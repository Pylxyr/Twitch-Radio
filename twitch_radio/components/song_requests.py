from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from twitchio import Chatter
from twitchio.ext import commands

from twitch_radio.extraction import UnsupportedSourceError
from twitch_radio.player import SKIP_NOT_READY_MESSAGE, QueuedRequest, SkipResult
from twitch_radio.telemetry import counters
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

# Keyed by command name, for event_command_error's MissingRequiredArgument
# handler in chatbot.py.
USAGE = {
    "sr": "Usage: !sr <song name or URL>",
    "radio": "Usage: !radio [on|off]",
}

# How much of a chatter's query the "Looking up ..." acknowledgement repeats.
_MAX_ECHO_CHARS = 80


class SongRequestComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="sr")
    async def song_request(self, ctx: commands.Context, *, query: str) -> None:
        query = query.strip()
        if not query:
            await self.bot.safe_reply(ctx, USAGE["sr"])
            return

        chatter_key = str(ctx.chatter.id)
        normalized_query = query.lower()

        # An exact repeat of a query this chatter already has resolving —
        # usually the same command double-tapped before "Looking up..."
        # even lands. A distinct reply here avoids Twitch's duplicate-
        # message drop and skips a redundant second resolve.
        if self.bot.inflight_query_by_chatter.get(chatter_key) == normalized_query:
            await self.bot.safe_reply(ctx, "Still looking that up — hang tight!")
            return

        tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())
        now = time.monotonic()

        # No `await` between checking limits and reserving the slot below —
        # keeps check-and-reserve atomic so rapid-fire !sr can't race past
        # the cooldown/pending/queue caps before the resolver's network call.
        last = self.bot.last_request_at.get(chatter_key, 0.0)
        if tunables.request_cooldown_seconds > 0 and (now - last) < tunables.request_cooldown_seconds:
            remaining = tunables.request_cooldown_seconds - (now - last)
            await self.bot.safe_reply(ctx, f"Slow down — try again in {remaining:.0f}s.")
            return

        pending = self.bot.pending_by_chatter.get(chatter_key, 0)
        if pending >= tunables.max_pending_per_chatter:
            await self.bot.safe_reply(
                ctx, f"You already have {pending} request(s) queued — wait for one to play first."
            )
            return

        if self.bot.player.real_queue_size() >= tunables.queue_cap:
            await self.bot.safe_reply(ctx, "Queue's full right now — try again in a bit.")
            return

        try:
            requester_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            # Bail rather than fall back to a fixed sentinel — that would
            # let two different chatters collide under the same fake id.
            await self.bot.safe_reply(ctx, "Couldn't identify you — try again.")
            return

        self.bot.last_request_at[chatter_key] = now
        self.bot.pending_by_chatter[chatter_key] = pending + 1

        # Resolving is a real network round trip (under a second to ~15-20s
        # cold), so !sr replies right away rather than leaving chat
        # wondering. _resolve_and_queue (a background task) sends the real
        # "Queued: ..." or an error once resolution finishes, and owns
        # releasing the pending-count reservation on every exit path.
        #
        # safe_reply here specifically: this runs before create_task()
        # below, so an uncaught delivery failure would abort song_request()
        # before the task exists — leaking the reservation and losing the
        # request entirely.
        # Echo at most a short prefix — repeating the full message back
        # would let anyone make the bot post up to 500 chars of their choosing.
        shown = query if len(query) <= _MAX_ECHO_CHARS else query[: _MAX_ECHO_CHARS - 1] + "\u2026"
        await self.bot.safe_reply(ctx, f"Looking up {shown!r}\u2026")
        self.bot.inflight_query_by_chatter[chatter_key] = normalized_query

        requester_name = ctx.chatter.display_name or ctx.chatter.name or "a viewer"
        task = asyncio.create_task(
            self._resolve_and_queue(ctx, query, chatter_key, requester_id, requester_name),
            name=f"song-request-{chatter_key}",
        )
        self.bot.background_tasks.add(task)
        task.add_done_callback(self.bot.background_tasks.discard)

    async def _resolve_and_queue(
        self, ctx: commands.Context, query: str, chatter_key: str, requester_id: int, requester_name: str
    ) -> None:
        """The slow half of !sr, split out so a slow resolve can't delay
        song_request()'s own reply. ctx.reply() doesn't depend on the
        originating coroutine still being alive — it's a plain API call
        keyed off already-captured attributes — so replying here, well
        after song_request() returned, is safe."""
        reserved = True
        try:
            try:
                track = await self.bot.resolver(query, requester_id)
            except UnsupportedSourceError as exc:
                await self.bot.safe_reply(ctx, str(exc))
                return
            except Exception:
                log.exception("Failed to resolve Twitch song request: %s", query)
                await self.bot.safe_reply(ctx, "Couldn't fetch that — try a different search or link.")
                return

            if track is None:
                await self.bot.safe_reply(ctx, "No results for that.")
                return

            if track.is_live:
                await self.bot.safe_reply(ctx, "Can't queue a livestream — sorry!")
                return

            # Re-read rather than reuse whatever song_request() read before
            # resolving — a mod could easily adjust /settings during a
            # multi-second resolve.
            tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())

            if 0 < tunables.max_request_duration_seconds < track.duration:
                minutes = tunables.max_request_duration_seconds // 60
                await self.bot.safe_reply(ctx, f"That's too long to queue — max is {minutes} minute(s).")
                return

            if self.bot.player.is_already_requested(track.webpage_url):
                await self.bot.safe_reply(ctx, f"{track.title} is already queued.")
                return

            # Re-check the cap right before enqueuing (no await between this
            # check and enqueue() below) — the resolve above may have taken
            # long enough for the queue to have filled up meanwhile.
            if self.bot.player.real_queue_size() >= tunables.queue_cap:
                await self.bot.safe_reply(ctx, "Queue's full right now — try again in a bit.")
                return

            def _on_start(key: str = chatter_key) -> None:
                remaining_pending = self.bot.pending_by_chatter.get(key, 1) - 1
                if remaining_pending <= 0:
                    self.bot.pending_by_chatter.pop(key, None)
                else:
                    self.bot.pending_by_chatter[key] = remaining_pending

            self.bot.player.enqueue(
                QueuedRequest(
                    webpage_url=track.webpage_url,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    title=track.title,
                    uploader=track.uploader,
                    thumbnail_url=track.thumbnail_url,
                    on_start=_on_start,
                )
            )
            reserved = False  # ownership of the reservation now belongs to on_start's eventual decrement
            counters.record("requests_queued")
            await self.bot.safe_reply(
                ctx, f"Queued: {track.title} (#{self.bot.player.real_queue_size()} in queue)"
            )
        except Exception:
            # Catch-all so a bug here can't silently eat the chatter's
            # pending-count reservation forever, or fail with no reply at
            # all — create_task() has no caller left to propagate to.
            log.exception("Unhandled error resolving/queuing song request: %s", query)
            await self.bot.safe_reply(ctx, "Something went wrong queuing that — try again.")
        finally:
            if reserved:
                remaining_pending = self.bot.pending_by_chatter.get(chatter_key, 1) - 1
                if remaining_pending <= 0:
                    self.bot.pending_by_chatter.pop(chatter_key, None)
                else:
                    self.bot.pending_by_chatter[chatter_key] = remaining_pending
            # Only clear if it's still *our* query — a newer !sr from this
            # same chatter could already have overwritten the marker with a
            # different query by the time this one finishes.
            if self.bot.inflight_query_by_chatter.get(chatter_key) == query.lower():
                self.bot.inflight_query_by_chatter.pop(chatter_key, None)

    @commands.command(name="skip")
    # No @commands.is_moderator() guard: mods and the broadcaster can always
    # skip (checked manually below), and anyone can skip the request of
    # theirs that is currently playing or loading, without mod status.
    async def skip(self, ctx: commands.Context) -> None:
        chatter = ctx.chatter
        # ctx.chatter is Chatter | PartialUser; only Chatter has the role flags.
        is_mod = isinstance(chatter, Chatter) and (chatter.moderator or chatter.broadcaster)
        if not is_mod:
            # active_requester_id (not now_playing) so a chatter can skip
            # their own song during the resolve/load window too — that can
            # take 15-20s+, and now_playing stays None the whole time.
            active_id = self.bot.player.active_requester_id
            if active_id is None:
                await self.bot.safe_reply(ctx, "Nothing's playing right now.")
                return
            try:
                requester_id = int(ctx.chatter.id)
            except (TypeError, ValueError):
                requester_id = -1
            if requester_id != active_id:
                await self.bot.safe_reply(ctx, "You can only skip your own song — mods can skip anything.")
                return
        result = await self.bot.player.skip()
        if result is SkipResult.SKIPPED:
            await self.bot.safe_reply(ctx, "Skipped.")
        elif result is SkipResult.NOT_READY:
            await self.bot.safe_reply(ctx, SKIP_NOT_READY_MESSAGE)
        else:
            await self.bot.safe_reply(ctx, "Nothing's playing right now.")

    # Deliberately not "queue": another bot in the channel already answers !queue.
    @commands.command(name="sq", aliases=["songqueue"])
    async def queue_cmd(self, ctx: commands.Context) -> None:
        items = self.bot.player.queued_items()
        if not items:
            await self.bot.safe_reply(ctx, "Queue is empty.")
            return
        upcoming = ", ".join(item.title or "an unnamed track" for item in items[:3])
        more = f" (+{len(items) - 3} more)" if len(items) > 3 else ""
        await self.bot.safe_reply(ctx, f"{len(items)} queued: {upcoming}{more}")

    @commands.command(name="radio")
    async def radio_toggle(self, ctx: commands.Context, *, arg: str = "") -> None:
        """!radio alone reports whether auto-radio is on (anyone can ask);
        !radio on / !radio off is the broadcaster's alone. When on, an empty
        queue auto-fills with a track related to whatever just finished
        (YouTube's own "Mix" playlist) instead of going quiet."""
        arg = arg.strip().lower()
        if not arg:
            toggles = FeatureToggles.from_dict(await self.bot.toggles_store.read())
            state = "on" if toggles.radio_autoplay_enabled else "off"
            await self.bot.safe_reply(ctx, f"Radio autoplay is {state}.")
            return
        chatter = ctx.chatter
        if not (isinstance(chatter, Chatter) and chatter.broadcaster):
            await self.bot.safe_reply(
                ctx, "Only the broadcaster can change that - try !radio with no argument to check status."
            )
            return
        if arg not in ("on", "off"):
            await self.bot.safe_reply(ctx, USAGE["radio"])
            return

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            toggles = FeatureToggles.from_dict(current)
            toggles.radio_autoplay_enabled = arg == "on"
            return toggles.to_dict()

        await self.bot.toggles_store.update(_mutate)
        # Turning it off also clears any radio-mix songs already lined up; turning it on refills.
        await self.bot.player.apply_radio_lookahead()
        await self.bot.safe_reply(ctx, f"Radio autoplay is now {arg}.")
