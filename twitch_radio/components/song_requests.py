from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from twitchio import Chatter
from twitchio.ext import commands

from twitch_radio.extraction import UnsupportedSourceError
from twitch_radio.player import QueuedRequest
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

if TYPE_CHECKING:
    from twitch_radio.chatbot import TwitchChatBot

log = logging.getLogger(__name__)

# Keyed by canonical command name (ctx.command.name resolves aliases to
# this), for event_command_error's MissingRequiredArgument handler in
# chatbot.py.
USAGE = {"sr": "Usage: !sr <song name or URL>"}

# How much of a chatter's query the "Looking up ..." acknowledgement repeats.
_MAX_ECHO_CHARS = 80


class SongRequestComponent(commands.Component):
    def __init__(self, bot: TwitchChatBot) -> None:
        self.bot = bot

    @commands.command(name="sr", aliases=["songrequest"])
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
            await self.bot.safe_reply(ctx, f"You already have {pending} request(s) queued — wait for one to play first.")
            return

        if self.bot.player.queue_size() >= tunables.queue_cap:
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

            already_queued = track.webpage_url == self.bot.player.active_webpage_url or any(
                item.webpage_url == track.webpage_url for item in self.bot.player.queued_items()
            )
            if already_queued:
                await self.bot.safe_reply(ctx, f"{track.title} is already queued.")
                return

            # Re-check the cap right before enqueuing (no await between this
            # check and enqueue() below) — the resolve above may have taken
            # long enough for the queue to have filled up meanwhile.
            if self.bot.player.queue_size() >= tunables.queue_cap:
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
                    on_start=_on_start,
                )
            )
            reserved = False  # ownership of the reservation now belongs to on_start's eventual decrement
            await self.bot.safe_reply(ctx, f"Queued: {track.title} (#{self.bot.player.queue_size()} in queue)")
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
    # No @commands.is_moderator() guard — mods/broadcaster can always skip
    # (checked manually below), but a chatter can also skip their own
    # currently-playing request without mod status.
    async def skip(self, ctx: commands.Context) -> None:
        chatter = ctx.chatter
        # ctx.chatter is Chatter | PartialUser; only Chatter has .moderator.
        is_mod = isinstance(chatter, Chatter) and chatter.moderator
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
        if self.bot.player.skip_current():
            await self.bot.safe_reply(ctx, "Skipped.")
        else:
            await self.bot.safe_reply(ctx, "Nothing's playing right now.")

    @commands.command(name="pause")
    @commands.is_moderator()
    async def pause(self, ctx: commands.Context) -> None:
        """Stops whatever's playing (or resolving) and holds the queue at
        silence — for an ad break or announcement where the mod wants the
        music gone immediately, not once the current track ends. Replays
        from the top on !resume; no seek support anywhere in this pipeline."""
        if self.bot.player.pause():
            await self.bot.safe_reply(ctx, "Paused. !resume to pick it back up.")
        else:
            await self.bot.safe_reply(ctx, "Already paused.")

    @commands.command(name="resume", aliases=["unpause"])
    @commands.is_moderator()
    async def resume(self, ctx: commands.Context) -> None:
        if self.bot.player.resume():
            await self.bot.safe_reply(ctx, "Resumed.")
        else:
            await self.bot.safe_reply(ctx, "Not paused right now.")

    @commands.command(name="voteskip", aliases=["vs"])
    async def vote_skip(self, ctx: commands.Context) -> None:
        """Anyone can vote to skip what's playing (or loading) — once
        enough unique chatters vote (vote_skip_threshold, adjustable from
        /settings), it's skipped automatically. Votes don't carry over
        between tracks."""
        if self.bot.player.active_requester_id is None:
            await self.bot.safe_reply(ctx, "Nothing's playing right now.")
            return
        try:
            voter_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            await self.bot.safe_reply(ctx, "Couldn't identify you — try again.")
            return
        tunables = TwitchTunables.from_dict(await self.bot.tunables_store.read())
        result = self.bot.player.register_skip_vote(voter_id, tunables.vote_skip_threshold)
        if result is None:
            await self.bot.safe_reply(ctx, "Nothing's playing right now.")
            return
        skipped, count, is_new = result
        if skipped:
            await self.bot.safe_reply(ctx, "Vote-skipped!")
        elif not is_new:
            await self.bot.safe_reply(ctx, f"You've already voted to skip this one ({count}/{tunables.vote_skip_threshold}).")
        else:
            needed = tunables.vote_skip_threshold - count
            await self.bot.safe_reply(ctx, f"Skip vote registered ({count}/{tunables.vote_skip_threshold}) — {needed} more needed.")

    @commands.command(name="remove", aliases=["cancel", "unqueue"])
    async def remove(self, ctx: commands.Context) -> None:
        """Lets a chatter pull their own most-recently-queued request back
        out of the queue — not the one currently playing (!skip is for that)."""
        try:
            requester_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            await self.bot.safe_reply(ctx, "Couldn't identify you — try again.")
            return
        removed = self.bot.player.cancel_pending_for(requester_id)
        if removed is None:
            await self.bot.safe_reply(ctx, "You don't have anything waiting in the queue.")
            return
        title = removed.title or "your request"
        await self.bot.safe_reply(ctx, f"Removed: {title}")

    @commands.command(name="position", aliases=["pos"])
    async def position(self, ctx: commands.Context) -> None:
        """Shows where the chatter's own request(s) sit in the queue."""
        try:
            requester_id = int(ctx.chatter.id)
        except (TypeError, ValueError):
            await self.bot.safe_reply(ctx, "Couldn't identify you — try again.")
            return
        positions = self.bot.player.positions_for(requester_id)
        if not positions:
            if self.bot.player.active_requester_id == requester_id:
                await self.bot.safe_reply(ctx, "Your song is up now!")
            else:
                await self.bot.safe_reply(ctx, "You don't have anything queued.")
            return
        if len(positions) == 1:
            await self.bot.safe_reply(ctx, f"You're #{positions[0]} in the queue.")
        else:
            spots = ", ".join(f"#{p}" for p in positions)
            await self.bot.safe_reply(ctx, f"You're at {spots} in the queue.")

    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context) -> None:
        items = self.bot.player.queued_items()
        if not items:
            await self.bot.safe_reply(ctx, "Queue is empty.")
            return
        upcoming = ", ".join(item.title or "an unnamed track" for item in items[:3])
        more = f" (+{len(items) - 3} more)" if len(items) > 3 else ""
        await self.bot.safe_reply(ctx, f"{len(items)} queued: {upcoming}{more}")

    @commands.command(name="nowplaying", aliases=["np"])
    async def now_playing(self, ctx: commands.Context) -> None:
        np = self.bot.player.now_playing
        if np is None:
            await self.bot.safe_reply(ctx, "Nothing's playing right now.")
            return
        elapsed = max(0, int(time.monotonic() - np.started_at))
        await self.bot.safe_reply(ctx, f"Now playing: {np.title} — requested by {np.requester_name} ({elapsed}s in)")

    @commands.command(name="radio")
    async def radio_toggle(self, ctx: commands.Context, *, arg: str = "") -> None:
        """!radio alone reports status; !radio on/off (mod-only) flips it.
        When on, an empty queue auto-fills with a track related to whatever
        just finished (YouTube's own "Mix" playlist) instead of going quiet."""
        arg = arg.strip().lower()
        if not arg:
            toggles = FeatureToggles.from_dict(await self.bot.toggles_store.read())
            state = "on" if toggles.radio_autoplay_enabled else "off"
            await self.bot.safe_reply(ctx, f"Radio autoplay is {state}.")
            return
        chatter = ctx.chatter
        if not (isinstance(chatter, Chatter) and chatter.moderator):
            await self.bot.safe_reply(ctx, "Only mods can change that — try !radio with no argument to check status.")
            return
        if arg not in ("on", "off"):
            await self.bot.safe_reply(ctx, "Usage: !radio [on|off]")
            return

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            toggles = FeatureToggles.from_dict(current)
            toggles.radio_autoplay_enabled = arg == "on"
            return toggles.to_dict()

        await self.bot.toggles_store.update(_mutate)
        await self.bot.safe_reply(ctx, f"Radio autoplay is now {arg}.")
