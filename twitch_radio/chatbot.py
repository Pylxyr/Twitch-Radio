"""Twitch chat bot — wires up the song-request commands
(twitch_radio/components/song_requests.py) and hands resolved requests to
the radio player.

Built against twitchio 3.x's EventSub-based Bot (not the old IRC-token
pattern from twitchio 2.x).

Auth model: Twitch's "Installed Chatbot" pattern — one bot account (made a
moderator in your channel) with a User Access Token carrying
`user:read:chat` + `user:write:chat`. Moderator status satisfies the
ChatMessageSubscription requirement without a separate broadcaster-side
`channel:bot` grant.

One-time OAuth setup is required before chat commands work — TwitchIO's
built-in web server listens on http://localhost:4343 and persists whatever
token you authorize (see load_tokens/save_tokens):

  1. Start the bot once with TWITCH_CLIENT_ID/SECRET/BOT_ID/OWNER_ID set.
  2. On a remote host, tunnel the port first:
     `ssh -L 4343:localhost:4343 <user>@<host>`
  3. In a browser, logged in as the BOT's own account:
     http://localhost:4343/oauth?scopes=user:read:chat+user:write:chat+user:bot&force_verify=true
  4. In a SEPARATE browser session, logged in as the BROADCASTER's account:
     http://localhost:4343/oauth?scopes=channel:bot&force_verify=true

  Reusing the same logged-in session for steps 3 and 4 is the most common
  way this goes wrong — Twitch authorizes whichever account is currently
  logged in, with no error either way. See `_log_token_diagnostics` below.

  Tokens save to TWITCH_TOKEN_FILE (default: data/twitch_tokens.json) and
  reload automatically on future starts.

event_message() (dispatching commands) is left to commands.Bot's own
default implementation — nothing in this bot needs to see every message,
only the prefixed ones a command already matches.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from twitchio import eventsub
from twitchio.exceptions import TwitchioException
from twitchio.ext import commands

from twitch_radio.components.song_requests import SongRequestComponent
from twitch_radio.components.song_requests import USAGE as _USAGE
from twitch_radio.player import RadioPlayer
from twitch_radio.store import JsonStore

if TYPE_CHECKING:
    from twitchio.authentication import ValidateTokenPayload
    from twitchio.payloads import TokenRefreshedPayload

    from twitch_radio.extraction import Resolver

log = logging.getLogger(__name__)

# Twitch silently drops a chat message byte-identical to one this account
# sent recently — a real server-side rolling window, not just "the last
# message" (seen dropping a repeat 17s later, and even across a process
# restart with no local memory of what it sent). Too unpredictable to
# track client-side, so every safe_reply instead gets a small rotating
# cosmetic suffix, guaranteeing it's never byte-identical to the last one.
_DEDUP_SUFFIXES = (" \U0001f3b5", " \U0001f3b6", " \U0001f3a7", " \U0001f50a")

# Twitch's hard limit on a single chat message. PartialUser.send_message
# raises a plain ValueError above it — not a TwitchioException, so it sails
# past safe_reply's delivery-failure handling unless caught explicitly.
# Reachable without trying: a long !queue reply with several long titles.
_MAX_CHAT_MESSAGE_LENGTH = 500


class TwitchChatBot(commands.Bot):
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        bot_id: str,
        owner_id: str,
        prefix: str,
        resolver: Resolver,
        player: RadioPlayer,
        tunables_store: JsonStore,
        toggles_store: JsonStore,
        token_storage_path: Path,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            bot_id=bot_id,
            owner_id=owner_id,
            prefix=prefix,
        )
        self.resolver = resolver.resolve
        self.player = player
        self.tunables_store = tunables_store
        self.toggles_store = toggles_store
        self.prefix = prefix
        self._owner_id = owner_id
        self._bot_id = bot_id
        self._token_storage_path = token_storage_path
        # Set once subscribe_websocket() succeeds — save_tokens() retries the
        # subscription on every call until this is True.
        self._chat_subscribed = False
        # Per-chatter state — deliberately in-memory only: losing cooldown/
        # pending tracking across a restart is harmless, and persisting it
        # would add complexity for no real benefit.
        self.last_request_at: dict[str, float] = {}
        self.pending_by_chatter: dict[str, int] = {}
        # Tracks each chatter's in-flight !sr query (case-folded) so a
        # repeat while it's still resolving gets a distinct reply instead of
        # a second full resolve — without this, Twitch's dedup rule would
        # silently drop the identical "Looking up '...'…" and a double-tap
        # would look ignored while quietly redoing the whole round trip.
        # Cleared in _resolve_and_queue's finally.
        self.inflight_query_by_chatter: dict[str, str] = {}
        # Strong references to in-flight _resolve_and_queue() tasks —
        # without this, asyncio can garbage-collect a fire-and-forget task
        # mid-flight. Entries remove themselves via add_done_callback.
        self.background_tasks: set[asyncio.Task[None]] = set()
        # Round-robins through _DEDUP_SUFFIXES on every safe_reply call —
        # shared across every component (not one counter each) so replies
        # from different commands still can't collide on Twitch's dedup
        # window back to back.
        self._reply_counter = 0

    @property
    def owner_id_required(self) -> str:
        # The base class's own `owner_id` property returns `str | None`;
        # this subclass always constructs with one, so it's never actually
        # None — narrowed once here instead of a repeated assert at every
        # call site. (`bot_id` needs no equivalent: the base class's own
        # `bot_id` property already asserts and returns `str`.)
        assert self.owner_id is not None
        return self.owner_id

    async def load_tokens(self, path: str | None = None, /) -> None:
        # Redirects TwitchIO's default token file into DATA_DIR instead.
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        await super().load_tokens(path or str(self._token_storage_path))

    async def save_tokens(self, path: str | None = None, /) -> None:
        """Writes tokens to disk, locks the file down, and retries the chat
        subscription (a no-op once already subscribed). twitchio's Client
        only calls this on a graceful close — add_token() and
        event_token_refreshed() below call it explicitly too, so completing
        OAuth or a routine refresh takes effect immediately instead of only
        persisting at the next restart."""
        self._token_storage_path.parent.mkdir(parents=True, exist_ok=True)
        target = path or str(self._token_storage_path)
        await super().save_tokens(target)
        # twitchio's save() writes with no explicit mode, so this (live
        # OAuth tokens) inherits the process umask — often world-readable.
        # Locked down the same way a typical .env is — a fresh write resets
        # permissions, so this reapplies every save.
        with contextlib.suppress(OSError):
            Path(target).chmod(0o600)
        await self._try_subscribe_chat()

    async def add_token(self, token: str, refresh: str) -> ValidateTokenPayload:
        """twitchio calls this the instant an OAuth authorization completes,
        well before setup_hook() or any later save_tokens() call — the base
        class otherwise only calls save_tokens() from Client.close()."""
        response = await super().add_token(token, refresh)
        await self.save_tokens()
        return response

    async def event_token_refreshed(self, payload: TokenRefreshedPayload) -> None:
        """twitchio dispatches this after silently refreshing a
        soon-to-expire token — without this, the refreshed pair only lives
        in memory until the next graceful close, so a crash in between
        loads a stale, already-rotated token and forces re-authorization."""
        await self.save_tokens()

    def _oauth_complete(self) -> bool:
        if not self._token_storage_path.exists():
            return False
        try:
            saved_ids = set(json.loads(self._token_storage_path.read_text(encoding="utf-8")))
        except Exception:
            return False
        return self._bot_id in saved_ids and self._owner_id in saved_ids

    async def _try_subscribe_chat(self) -> None:
        if self._chat_subscribed:
            return
        self._log_token_diagnostics()
        subscription = eventsub.ChatMessageSubscription(
            broadcaster_user_id=self._owner_id,
            user_id=self._bot_id,
        )
        try:
            await self.subscribe_websocket(payload=subscription)
            log.info("Subscribed to chat messages for broadcaster=%s bot=%s", self._owner_id, self._bot_id)
            self._chat_subscribed = True
        except Exception as e:
            if self._oauth_complete():
                log.exception(
                    "Chat subscription failed even though both accounts have saved tokens — "
                    "chat commands won't work until this is fixed. See the token diagnostics "
                    "logged above, or redo the OAuth steps in this module's docstring with "
                    "&force_verify=true if a token was revoked or scopes changed. Error: %s",
                    e,
                )
            else:
                log.warning(
                    "Skipping chat subscription for now — the one-time OAuth steps in this "
                    "module's docstring aren't done for both accounts yet at %s. Will retry "
                    "automatically as soon as a token is saved, no restart needed. Error: %s",
                    self._token_storage_path,
                    e,
                )

    async def setup_hook(self) -> None:
        await self.add_component(SongRequestComponent(self))
        await self._try_subscribe_chat()

    def _log_token_diagnostics(self) -> None:
        # Catches the single most common cause of "OAuth said success but
        # chat still doesn't work": the saved token belongs to a different
        # Twitch account than TWITCH_BOT_ID/TWITCH_OWNER_ID, from reusing an
        # already-logged-in browser session for both authorization steps.
        if not self._token_storage_path.exists():
            return
        try:
            saved_ids = set(json.loads(self._token_storage_path.read_text(encoding="utf-8")))
        except Exception as e:
            log.warning("Couldn't read %s to check saved tokens: %s", self._token_storage_path, e)
            return
        if self._bot_id not in saved_ids:
            log.error(
                "No saved token for TWITCH_BOT_ID=%s in %s (tokens on file: %s). Chat commands "
                "won't work. Redo the bot-account OAuth step — make sure the browser is actually "
                "logged into THAT account, not your broadcaster account (a private/incognito "
                "window avoids reusing whatever session is already active).",
                self._bot_id,
                self._token_storage_path,
                sorted(saved_ids) or "none",
            )
        if self._owner_id not in saved_ids:
            log.info(
                "No saved token for TWITCH_OWNER_ID=%s — fine if the bot account is already a "
                "moderator in your channel (that alone satisfies the chat subscription), "
                "otherwise redo the broadcaster-account OAuth step.",
                self._owner_id,
            )

    async def event_ready(self) -> None:
        log.info("Twitch chat bot ready (bot_id=%s).", self._bot_id)

    async def announce(self, message: str) -> None:
        """Sends a message to the broadcaster's channel — used by RadioPlayer
        to tell chat about a track it had to drop. Not tied to a Context, so
        this goes through PartialUser.send_message directly."""
        channel = self.create_partialuser(user_id=self.owner_id_required)
        text = self._decorate(message)
        try:
            await channel.send_message(sender=self.bot_id, message=text)
        except (TwitchioException, ValueError) as e:
            # Same rationale as safe_reply: a delivery failure is Twitch
            # declining to show a message, not a caller bug — every caller
            # already treats announcing as best-effort.
            log.info("Announcement not delivered (%s): %r", type(e).__name__, text)

    async def safe_reply(self, ctx: commands.Context, message: str) -> None:
        """ctx.reply() that swallows Twitch's delivery failures (rate limit,
        exact-duplicate-message rule — both TwitchioException) instead of
        letting them propagate.

        Matters most for !sr's first reply: it runs before
        _resolve_and_queue's own try/except exists, so an uncaught failure
        there silently kills the whole request — no queue, no error,
        nothing. Two chatters requesting the same playing song back to
        back hits this normally, not just rapid self-testing.

        Every message gets a rotating suffix (_DEDUP_SUFFIXES) unconditionally
        before delivery — Twitch's own dedup window isn't something this
        process can reliably reconstruct.
        """
        text = self._decorate(message)
        try:
            await ctx.reply(text)
        except (TwitchioException, ValueError) as e:
            log.info("Chat reply not delivered (%s): %r", type(e).__name__, text)

    def _decorate(self, message: str) -> str:
        """Adds the rotating anti-dedup suffix and enforces Twitch's
        500-char limit. Shared by safe_reply()/announce().

        Truncating beats the alternative: over the limit, send_message
        raises and the message never appears, which looks exactly like
        the bot ignoring the command.
        """
        suffix = _DEDUP_SUFFIXES[self._reply_counter % len(_DEDUP_SUFFIXES)]
        self._reply_counter += 1
        budget = _MAX_CHAT_MESSAGE_LENGTH - len(suffix)
        if len(message) > budget:
            message = message[: budget - 1] + "\u2026"
        return f"{message}{suffix}"

    async def event_command_error(self, payload: commands.CommandErrorPayload) -> None:
        exc = payload.exception
        ctx = payload.context
        if isinstance(exc, commands.CommandNotFound):
            # Fires for every prefixed message that isn't one of ours — with
            # another bot sharing "!" (Nightbot, StreamElements), that's most
            # of them, so this stays quiet rather than replying to every
            # !whatever the channel's other bots handle.
            return
        if isinstance(exc, commands.GuardFailure):
            await self.safe_reply(ctx, "You don't have permission to use that command.")
            return
        if isinstance(exc, commands.MissingRequiredArgument):
            # ctx.command.name is the canonical name even via an alias (e.g.
            # !songrequest resolves to "sr").
            name = ctx.command.name if ctx.command is not None else "sr"
            usage = _USAGE.get(name, _USAGE["sr"])
            await self.safe_reply(ctx, usage)
            return
        log.error("Command error in %r: %r", getattr(ctx, "content", "<unknown>"), exc, exc_info=exc)
