from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from twitch_radio.models import Track

log = logging.getLogger(__name__)

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
_CHUNK_DURATION = 0.1
_CHUNK_BYTES = int(AUDIO_RATE * AUDIO_CHANNELS * 2 * _CHUNK_DURATION)
_SILENCE_CHUNK = b"\x00" * _CHUNK_BYTES
_STREAM_CHUNK_BYTES = 8192
_SUBSCRIBER_QUEUE_SIZE = 50  # ~5-10s of MP3 at typical bitrates; a stalled listener gets dropped, not buffered forever

_MIN_BACKOFF = 5.0
_MAX_BACKOFF = 300.0
_STABLE_UPTIME_SECONDS = 30.0
_DECODER_START_TIMEOUT = 20.0
# Guards every stdout read for the rest of a track (separate from
# _DECODER_START_TIMEOUT, which only covers the first chunk) — without it,
# a decoder that stops producing output mid-track hangs forever in
# `stdout.read()` with nothing to recover on its own. 20s is generous:
# chunks normally arrive every _CHUNK_DURATION via ffmpeg's -re pacing, so
# a real gap that long means the source is actually stuck.
_STALL_TIMEOUT_SECONDS = 20.0
# How long before a track ends to pre-resolve what's next (see
# _prefetch_next_when_close) — comfortably above the ~8-18s a warm resolve
# takes, so it lands inside Resolver's TTL cache before it's needed for
# real. Without this, a queue backlog outliving YTDLP_CACHE_TTL_SECONDS
# turns every transition into a full cold resolve — audible silence
# mid-stream, not just at startup.
_PREFETCH_LEAD_SECONDS = 20.0


class TrackResolver(Protocol):
    async def __call__(self, query: str, requester_id: int) -> Track | None: ...


class RadioSuggestFn(Protocol):
    async def __call__(self, seed_webpage_url: str) -> "QueuedRequest | None": ...


# "Don't hammer a broken/empty radio mix" guard — a fast-failing
# suggestion could otherwise refire every ~0.1s idle tick.
_RADIO_RETRY_BACKOFF_SECONDS = 30.0


@dataclass(slots=True)
class QueuedRequest:
    webpage_url: str
    requester_id: int
    requester_name: str
    title: str = ""
    # Uploader/channel as reported by the resolve. Carried here so !block
    # <uploader> can purge already-queued entries like !block <url> does,
    # without re-resolving every pending request just to find out who
    # uploaded it. Empty string when unknown.
    uploader: str = ""
    cancelled: bool = False
    on_start: Callable[[], None] | None = field(default=None, repr=False)


class PlayerState(str, Enum):
    """Derived, read-only view over the _resolving/_now_playing flags below
    — for external observability (/healthz, etc.) without changing how
    those flags are maintained. Not a real state machine, just a label for
    whatever the flags currently say."""

    IDLE = "idle"
    RESOLVING = "resolving"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass(slots=True)
class NowPlaying:
    title: str
    uploader: str
    thumbnail_url: str | None
    requester_name: str
    requester_id: int
    webpage_url: str
    started_at: float
    duration: int


class RadioPlayer:
    """Owns one persistent ffmpeg encoder producing a continuous MP3 stream
    from resolved tracks + silence between them, fanned out to any number of
    HTTP subscribers (see subscribe()/unsubscribe()) — e.g. an OBS Media
    Source. Nothing is pushed anywhere on its own; playback only happens
    where something is actually listening.
    """

    def __init__(
        self,
        *,
        resolver: TrackResolver,
        audio_bitrate_kbps: int,
        pause_when_no_listeners: bool = False,
        prefetch_enabled: bool = True,
    ) -> None:
        self._resolver = resolver
        self._audio_bitrate_kbps = audio_bitrate_kbps
        self._pause_when_no_listeners = pause_when_no_listeners
        # Pointless when Resolver's cache is disabled
        # (YTDLP_CACHE_TTL_SECONDS=0) — the prefetch's result would just be
        # discarded instead of reused. See bot.py for how this is computed.
        self._prefetch_enabled = prefetch_enabled
        self._prefetch_task: asyncio.Task[None] | None = None

        self._queue: asyncio.Queue[QueuedRequest] = asyncio.Queue()
        self._pending: list[QueuedRequest] = []
        self._task: asyncio.Task[None] | None = None
        self._encoder: asyncio.subprocess.Process | None = None
        self._encoder_spawned_at: float = 0.0
        self._backoff = _MIN_BACKOFF
        self._backoff_reset_done = False
        self._current_decoder: asyncio.subprocess.Process | None = None
        self._now_playing: NowPlaying | None = None
        # The request occupying the player's one "slot" — set the instant
        # it's dequeued, cleared when done/failed. now_playing alone isn't
        # enough: it stays None through the resolve/decoder-startup window,
        # so this covers that gap too, letting a chatter !skip their own
        # song before it's technically "playing" yet.
        self._active_request: QueuedRequest | None = None
        self._stopping = False
        self._resolving = False
        self._skip_pending = False
        self._notify_failure: Callable[[str], Awaitable[None]] | None = None
        self._duration_limit_getter: Callable[[], Awaitable[int]] | None = None
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        # Cleared every time a new request becomes active (see _play_one),
        # so votes never carry over from one song to the next.
        self._skip_votes: set[int] = set()
        # Pub-sub for "something about now-playing/queue changed" — carries
        # no payload; consumers (the admin server's /ws/nowplaying) re-fetch
        # full current state themselves.
        self._state_subscribers: set[asyncio.Queue[None]] = set()

        # Radio autoplay — see set_radio_suggester()/_maybe_start_radio_fill().
        self._radio_suggest: RadioSuggestFn | None = None
        self._radio_enabled_getter: Callable[[], Awaitable[bool]] | None = None
        self._last_played_webpage_url: str | None = None
        self._radio_fill_task: asyncio.Task[None] | None = None
        self._radio_fill_failed_at: float = 0.0

        # Wall-clock deadline for the next silence chunk — see
        # _write_paced_silence(). Zero means "not pacing yet"; the first
        # call snaps it to now.
        self._silence_deadline: float = 0.0

        # Manual mod pause (!pause/!resume) — separate from
        # _pause_when_no_listeners above, which is automatic and driven by
        # subscriber count. Only ever set by an explicit chat command.
        self._paused = False
        # Holds the track pause() interrupted, so resume() replays it first
        # instead of skipping to _pending's next item. No seek support
        # anywhere in this pipeline (ffmpeg runs with -re, no -ss), so
        # "resume" always means from 0:00, never from the cut-off point.
        self._priority_request: QueuedRequest | None = None

    # -- public interface used by the chat bot / admin server ------------

    @property
    def now_playing(self) -> NowPlaying | None:
        return self._now_playing

    @property
    def active_requester_id(self) -> int | None:
        """Who the player's current "slot" belongs to — playing or still
        resolving/loading — or None if idle."""
        if self._now_playing is not None:
            return self._now_playing.requester_id
        if self._active_request is not None:
            return self._active_request.requester_id
        return None

    @property
    def active_webpage_url(self) -> str | None:
        """Same idea as active_requester_id, but the URL — for duplicate-
        request checks against whatever's currently playing or resolving."""
        if self._now_playing is not None:
            return self._now_playing.webpage_url
        if self._active_request is not None:
            return self._active_request.webpage_url
        return None

    def queue_size(self) -> int:
        """Requests still waiting to play. Deliberately len(self._pending),
        not self._queue.qsize(): purge_pending()/cancel_pending_for() only
        mark an entry cancelled and drop it from _pending — the
        asyncio.Queue keeps the tombstone until _feed_loop next dequeues
        it, which never happens while paused for no listeners, so qsize()
        could stay at its pre-!clearqueue value indefinitely. _pending is
        the authoritative list and what !queue/the overlay already render,
        so this keeps all three in agreement."""
        return len(self._pending)

    def queued_items(self) -> list[QueuedRequest]:
        return list(self._pending)

    def positions_for(self, requester_id: int) -> list[int]:
        """1-indexed queue positions, in play order, for every one of
        requester_id's requests still waiting — empty if they have none
        waiting (check active_requester_id for the "up now" case)."""
        return [i + 1 for i, r in enumerate(self._pending) if r.requester_id == requester_id]

    def enqueue(self, request: QueuedRequest) -> None:
        self._queue.put_nowait(request)
        self._pending.append(request)
        self._notify_state_changed()

    def set_track_failure_notifier(self, notifier: Callable[[str], Awaitable[None]] | None) -> None:
        self._notify_failure = notifier

    def set_duration_limit_getter(self, getter: Callable[[], Awaitable[int]] | None) -> None:
        self._duration_limit_getter = getter

    def set_radio_suggester(self, suggester: RadioSuggestFn | None) -> None:
        self._radio_suggest = suggester

    def set_radio_enabled_getter(self, getter: Callable[[], Awaitable[bool]] | None) -> None:
        self._radio_enabled_getter = getter

    @property
    def state(self) -> PlayerState:
        # Checked first, ahead of now_playing/resolving: killing the
        # decoder in pause() is asynchronous, so there's a brief window
        # where _now_playing hasn't cleared yet even though a mod already
        # asked to pause. "Paused" is the answer that matches what the mod
        # just did, not an implementation detail of how fast ffmpeg exits.
        if self._paused:
            return PlayerState.PAUSED
        if self._now_playing is not None:
            return PlayerState.PLAYING
        if self._resolving or self._active_request is not None:
            return PlayerState.RESOLVING
        return PlayerState.IDLE

    @property
    def is_paused(self) -> bool:
        return self._paused

    def subscribe(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        self._subscribers.discard(q)

    def subscribe_state(self) -> asyncio.Queue[None]:
        """A queue that receives a wakeup (no payload) every time now-
        playing or the queue changes. Small maxsize is fine: consumers only
        care that *something* changed, and a full queue just means a
        wakeup is already pending."""
        q: asyncio.Queue[None] = asyncio.Queue(maxsize=4)
        self._state_subscribers.add(q)
        return q

    def unsubscribe_state(self, q: asyncio.Queue[None]) -> None:
        self._state_subscribers.discard(q)

    def _notify_state_changed(self) -> None:
        for q in list(self._state_subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)

    def cancel_pending_for(self, requester_id: int) -> QueuedRequest | None:
        for request in reversed(self._pending):
            if request.requester_id == requester_id and not request.cancelled:
                request.cancelled = True
                with contextlib.suppress(ValueError):
                    self._pending.remove(request)
                if request.on_start is not None:
                    with contextlib.suppress(Exception):
                        request.on_start()
                    request.on_start = None
                self._notify_state_changed()
                return request
        return None

    def purge_pending(self, predicate: Callable[[QueuedRequest], bool]) -> list[QueuedRequest]:
        """Removes every not-yet-playing request matching predicate — used
        for mod tooling (!clearqueue, and !block pulling out an already-
        queued copy of what it just blocked). Doesn't touch whatever's
        currently playing/resolving."""
        removed = []
        for request in list(self._pending):
            if not predicate(request):
                continue
            request.cancelled = True
            with contextlib.suppress(ValueError):
                self._pending.remove(request)
            if request.on_start is not None:
                with contextlib.suppress(Exception):
                    request.on_start()
                request.on_start = None
            removed.append(request)
        if removed:
            self._notify_state_changed()
        return removed

    def skip_current(self) -> bool:
        if self._current_decoder is not None:
            with contextlib.suppress(ProcessLookupError):
                self._current_decoder.kill()
            return True
        if self._resolving:
            self._skip_pending = True
            return True
        return False

    def pause(self) -> bool:
        """Mod-only manual pause. Returns False if already paused.

        Interrupts playback/resolving immediately — deliberately doesn't
        wait for a track boundary like _pause_when_no_listeners does, since
        a mod reaching for !pause usually means "stop it right now", not
        "in four minutes when this song ends". The interrupted request (if
        any) is preserved and replayed from the top on resume() — see
        _priority_request's docstring for why "from the top"."""
        if self._paused:
            return False
        self._paused = True
        active = self._active_request
        if active is not None and not active.cancelled:
            resumed = QueuedRequest(
                webpage_url=self._now_playing.webpage_url if self._now_playing else active.webpage_url,
                requester_id=active.requester_id,
                requester_name=active.requester_name,
                title=self._now_playing.title if self._now_playing else active.title,
                uploader=self._now_playing.uploader if self._now_playing else active.uploader,
            )
            self._priority_request = resumed
            self._pending.insert(0, resumed)
        self.skip_current()
        self._notify_state_changed()
        return True

    def resume(self) -> bool:
        """Returns False if not currently paused (nothing changed)."""
        if not self._paused:
            return False
        self._paused = False
        self._notify_state_changed()
        return True

    def register_skip_vote(self, voter_id: int, threshold: int) -> tuple[bool, int, bool] | None:
        """Registers one vote to skip whatever's currently active. Returns
        (skipped, vote_count, is_new_vote), or None if nothing's active to
        vote on. `skipped` is True if this vote reached `threshold` (votes
        are cleared immediately in that case). `is_new_vote` is False if
        this voter already voted for this same track."""
        if self.active_requester_id is None:
            return None
        is_new = voter_id not in self._skip_votes
        self._skip_votes.add(voter_id)
        count = len(self._skip_votes)
        if count >= threshold:
            self.skip_current()
            self._skip_votes.clear()
            return True, count, is_new
        return False, count, is_new

    async def _notify(self, message: str) -> None:
        if self._notify_failure is None:
            return
        with contextlib.suppress(Exception):
            await self._notify_failure(message)

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH — required to run the radio player.")
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever(), name="radio-player")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._radio_fill_task is not None:
            self._radio_fill_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._radio_fill_task
            self._radio_fill_task = None
        await self._kill_encoder()

    # -- internals ---------------------------------------------------------

    async def _kill_encoder(self) -> None:
        if self._current_decoder is not None:
            with contextlib.suppress(ProcessLookupError):
                self._current_decoder.kill()
            self._current_decoder = None
        if self._encoder is not None:
            with contextlib.suppress(ProcessLookupError):
                self._encoder.kill()
            with contextlib.suppress(Exception):
                await self._encoder.wait()
            self._encoder = None

    async def _run_forever(self) -> None:
        self._backoff = _MIN_BACKOFF
        while not self._stopping:
            try:
                await self._run_one_session()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Audio encoder session ended — restarting in %.0fs", self._backoff)
            finally:
                await self._kill_encoder()
            if self._stopping:
                return
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, _MAX_BACKOFF)

    async def _run_one_session(self) -> None:
        await self._spawn_encoder()
        self._encoder_spawned_at = time.monotonic()
        self._backoff_reset_done = False
        feed_task = asyncio.create_task(self._feed_loop())
        pump_task = asyncio.create_task(self._pump_encoder_output())
        try:
            done, pending = await asyncio.wait({feed_task, pump_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
        finally:
            feed_task.cancel()
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(feed_task, pump_task, return_exceptions=True)

    async def _spawn_encoder(self) -> None:
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS), "-i", "-",
            "-c:a", "libmp3lame", "-b:a", f"{self._audio_bitrate_kbps}k",
            "-id3v2_version", "0", "-write_xing", "0",  # no tags/duration header on an infinite live stream
            "-f", "mp3", "-",
        ]
        self._encoder = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
        )
        log.info("Audio encoder started (%dkbps MP3).", self._audio_bitrate_kbps)

    async def _pump_encoder_output(self) -> None:
        assert self._encoder is not None and self._encoder.stdout is not None
        stdout = self._encoder.stdout
        while True:
            chunk = await stdout.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                # EOF means ffmpeg itself exited (crashed, OOM-killed) —
                # stop() cancels this task directly before a read could
                # return empty during a normal shutdown. Raising routes
                # this through the same backoff/restart path as any other
                # encoder failure, with a log line explaining why.
                returncode = self._encoder.returncode if self._encoder is not None else None
                raise RuntimeError(f"Encoder stdout closed unexpectedly (exit code {returncode})")
            for q in list(self._subscribers):
                try:
                    q.put_nowait(chunk)
                except asyncio.QueueFull:
                    # A stalled subscriber's handle_stream() is still
                    # awaiting queue.get() — evict one old chunk to make
                    # room, then push an empty-bytes sentinel (never
                    # produced by a real read) that handle_stream() treats
                    # as "stop", so it actually closes instead of hanging.
                    self._subscribers.discard(q)
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        q.put_nowait(b"")

    async def _feed_loop(self) -> None:
        assert self._encoder is not None and self._encoder.stdin is not None
        encoder_stdin = self._encoder.stdin
        while not self._stopping:
            if self._encoder.returncode is not None:
                raise RuntimeError(f"Encoder exited with code {self._encoder.returncode}")
            if not self._backoff_reset_done and time.monotonic() - self._encoder_spawned_at >= _STABLE_UPTIME_SECONDS:
                self._backoff = _MIN_BACKOFF
                self._backoff_reset_done = True
            if self._paused:
                # Checked first, every tick — unlike _pause_when_no_listeners
                # below, this never falls through to a dequeue or reaches
                # _maybe_start_radio_fill(), so autoplay can't sneak a track
                # in while a mod has explicitly paused things.
                await self._write_paced_silence(encoder_stdin)
                continue
            try:
                if self._priority_request is not None:
                    # Set by pause() — takes priority over the normal queue
                    # exactly once, so resume() picks up the same track
                    # again rather than whatever's now at _pending's front.
                    request = self._priority_request
                    self._priority_request = None
                elif self._pause_when_no_listeners and not self._subscribers:
                    # Track-boundary pause only (not mid-track) — checked
                    # fresh every loop tick, so playback resumes on its own
                    # the instant a subscriber (re)connects.
                    await self._write_paced_silence(encoder_stdin)
                    continue
                else:
                    request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                # Catches what the prefetch-timer hook doesn't: a skip or
                # early end that empties the queue first. Guarded/deduped
                # inside the method, so calling it every idle tick is cheap.
                self._maybe_start_radio_fill()
                await self._write_paced_silence(encoder_stdin)
                continue
            with contextlib.suppress(ValueError):
                self._pending.remove(request)
            if request.cancelled:
                continue
            if request.on_start is not None:
                with contextlib.suppress(Exception):
                    request.on_start()
            await self._play_one(request, encoder_stdin)

    async def _write_silence_chunk(self, encoder_stdin: asyncio.StreamWriter) -> None:
        encoder_stdin.write(_SILENCE_CHUNK)
        await encoder_stdin.drain()

    async def _write_paced_silence(self, encoder_stdin: asyncio.StreamWriter) -> None:
        """Writes one silence chunk and sleeps until the *deadline* for the
        next one, rather than a flat _CHUNK_DURATION sleep.

        The flat-sleep version fed 0.1s of audio per (0.1s + write + event-
        loop latency) of wall clock, so the encoder ran slower than real
        time whenever silent — ~0.3% on an idle box (~11s of listener-buffer
        drain per hour), worse under load since yt-dlp's GIL-bound
        extraction runs on this same interpreter and every oversleep is
        kept, not amortised. OBS's Media Source pays for it as a
        progressive underrun.

        Accumulating against a monotonic deadline lets a late tick borrow
        from the next one instead of building permanent debt. The max()
        clamps the deadline forward after a long gap (a track just ended,
        the loop was paused) so it never "catches up" by dumping a burst
        of silence into the encoder.
        """
        await self._write_silence_chunk(encoder_stdin)
        self._silence_deadline = max(self._silence_deadline + _CHUNK_DURATION, time.monotonic())
        delay = self._silence_deadline - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _trickle_silence_until_cancelled(self, encoder_stdin: asyncio.StreamWriter) -> None:
        try:
            while True:
                await self._write_paced_silence(encoder_stdin)
        except asyncio.CancelledError:
            raise

    async def _play_one(self, request: QueuedRequest, encoder_stdin: asyncio.StreamWriter) -> None:
        self._active_request = request
        self._skip_votes.clear()
        self._notify_state_changed()
        try:
            await self._play_one_inner(request, encoder_stdin)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error playing queued request: %s", request.webpage_url)
        finally:
            self._active_request = None
            # However this track ended, its prefetch is no longer relevant
            # — the next _play_one_inner schedules its own once it knows
            # the new track's real duration.
            task = self._prefetch_task
            self._prefetch_task = None
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._notify_state_changed()

    async def _prefetch_next_when_close(self, current_duration: int) -> None:
        """Best-effort: once playback is close to ending, resolves what's
        now at the front of the queue so it's cache-warm by the time
        _play_one_inner re-resolves it for real (see _PREFETCH_LEAD_SECONDS).
        Never raises; a failure just means that transition pays the normal
        resolve cost. Re-checks the queue at fire time, not what was next
        when scheduled, since a request ahead of it could get skipped,
        blocked, or cleared while this was sleeping.
        """
        delay = max(0.0, current_duration - _PREFETCH_LEAD_SECONDS)
        await asyncio.sleep(delay)  # cancelled cleanly by _play_one's finally when the track ends first
        # Try radio auto-fill before checking what's next, so a freshly
        # queued pick gets the same pre-warming resolve below instead of a
        # cold one right when it's needed.
        self._maybe_start_radio_fill()
        upcoming = self._pending[0] if self._pending else None
        if upcoming is None or upcoming.cancelled:
            return
        try:
            await self._resolver(upcoming.webpage_url, upcoming.requester_id)
        except Exception:
            log.debug("Prefetch failed for %s (non-fatal).", upcoming.webpage_url, exc_info=True)

    def _maybe_start_radio_fill(self) -> None:
        """Kicks off a background radio-suggestion lookup when the queue is
        empty and nothing's already in flight. Called from both the
        prefetch timer (pre-warms the normal "track ends naturally" case)
        and _feed_loop's idle branch (catches a skip/early-end that beat
        the timer) — both funnel through this one guarded entry point so
        they can't double-queue a pick.

        _feed_loop can never reach this while paused, but the prefetch
        timer is a background task scheduled minutes earlier and could
        still fire mid-pause, so the guard is repeated here too."""
        if self._paused or self._radio_fill_task is not None or self._pending:
            return
        if self._radio_suggest is None or self._last_played_webpage_url is None:
            return
        if time.monotonic() - self._radio_fill_failed_at < _RADIO_RETRY_BACKOFF_SECONDS:
            return
        self._radio_fill_task = asyncio.create_task(self._run_radio_fill(), name="radio-autoplay-fill")

    async def _run_radio_fill(self) -> None:
        try:
            if self._radio_enabled_getter is not None:
                with contextlib.suppress(Exception):
                    if not await self._radio_enabled_getter():
                        # Arm the same backoff a failed suggestion uses.
                        # Without this, the common "radio autoplay is off"
                        # state meant _feed_loop's idle branch spawned a
                        # fresh task every ~100ms forever — cheap per-call,
                        # but ten pointless tasks a second isn't free on a
                        # small VPS.
                        self._radio_fill_failed_at = time.monotonic()
                        return
            seed = self._last_played_webpage_url
            if seed is None or self._radio_suggest is None:
                return
            try:
                picked = await self._radio_suggest(seed)
            except Exception:
                log.debug("Radio autoplay suggestion failed for %s (non-fatal).", seed, exc_info=True)
                picked = None
            if picked is None:
                self._radio_fill_failed_at = time.monotonic()
                return
            self.enqueue(picked)
            log.info("Radio autoplay queued: %s", picked.title)
        finally:
            self._radio_fill_task = None

    async def _current_duration_limit(self) -> int:
        if self._duration_limit_getter is None:
            return 0
        with contextlib.suppress(Exception):
            return await self._duration_limit_getter()
        return 0

    async def _play_one_inner(self, request: QueuedRequest, encoder_stdin: asyncio.StreamWriter) -> None:
        self._skip_pending = False
        silence_task = asyncio.create_task(
            self._trickle_silence_until_cancelled(encoder_stdin), name="radio-player-prefeed-silence"
        )
        decoder: asyncio.subprocess.Process | None = None
        first_chunk = b""
        self._resolving = True
        try:
            try:
                track = await self._resolver(request.webpage_url, request.requester_id)
            except Exception:
                log.exception("Failed to re-resolve queued request: %s", request.webpage_url)
                await self._notify(f"Couldn't load {request.requester_name}'s song — skipping it.")
                return
            if track is None:
                log.warning("Re-resolve returned nothing for %s — skipping", request.webpage_url)
                await self._notify(f"Couldn't load {request.requester_name}'s song — skipping it.")
                return
            if track.is_live:
                log.warning("Re-resolve found %s is now live — skipping", request.webpage_url)
                await self._notify(f"Skipped {request.requester_name}'s song — it's a livestream now.")
                return
            duration_limit = await self._current_duration_limit()
            if 0 < duration_limit < track.duration:
                log.warning("Re-resolve found %s now exceeds the duration cap — skipping", request.webpage_url)
                await self._notify(f"Skipped {request.requester_name}'s song — it's too long to play now.")
                return
            if self._skip_pending:
                self._skip_pending = False
                log.info("Skipped %s before it started playing (mid-resolve skip).", track.title)
                return

            self._now_playing = NowPlaying(
                title=track.title,
                uploader=track.uploader,
                thumbnail_url=track.thumbnail_url,
                requester_name=request.requester_name,
                requester_id=request.requester_id,
                webpage_url=track.webpage_url,
                started_at=time.monotonic(),
                duration=track.duration,
            )
            # Seed for the next radio-autoplay pick — set only once
            # playback is actually going ahead, so a track that fails to
            # resolve/decode never becomes a seed.
            self._last_played_webpage_url = track.webpage_url
            self._notify_state_changed()
            log.info("Now playing: %s (requested by %s)", track.title, request.requester_name)
            if self._prefetch_enabled:
                self._prefetch_task = asyncio.create_task(
                    self._prefetch_next_when_close(track.duration), name="radio-player-prefetch-next"
                )

            decoder = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                # Reconnect flags recover from a dropped/hiccuping CDN
                # connection instead of corrupting the stream. -probesize/
                # -analyzeduration skip ffmpeg's default multi-second format
                # probe, cutting startup latency.
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-reconnect_on_network_error", "1", "-reconnect_on_http_error", "429,500,502,503,504",
                "-probesize", "128k", "-analyzeduration", "0",
                "-re", "-i", track.stream_url,
                "-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS), "-",
                stdout=asyncio.subprocess.PIPE,
            )
            self._current_decoder = decoder
            if self._skip_pending:
                self._skip_pending = False
                log.info("Skipped %s right after its decoder started (mid-spawn skip).", track.title)
                with contextlib.suppress(ProcessLookupError):
                    decoder.kill()
                await decoder.wait()
                self._current_decoder = None
                self._now_playing = None
                decoder = None
                return
            assert decoder.stdout is not None
            stdout = decoder.stdout
            try:
                first_chunk = await asyncio.wait_for(stdout.read(_CHUNK_BYTES), timeout=_DECODER_START_TIMEOUT)
            except TimeoutError:
                log.warning("Timed out waiting for decoder output for %s — skipping.", request.webpage_url)
                await self._notify(f"Skipped {request.requester_name}'s song — it took too long to start.")
                with contextlib.suppress(ProcessLookupError):
                    decoder.kill()
                await decoder.wait()
                self._current_decoder = None
                self._now_playing = None
                decoder = None
                return
        finally:
            self._resolving = False
            silence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await silence_task

        if decoder is None:
            return
        try:
            chunk = first_chunk
            while chunk:
                encoder_stdin.write(chunk)
                await encoder_stdin.drain()
                try:
                    chunk = await asyncio.wait_for(stdout.read(_CHUNK_BYTES), timeout=_STALL_TIMEOUT_SECONDS)
                except TimeoutError:
                    log.warning(
                        "No audio from decoder for %.0fs (stalled source?) — skipping %s.",
                        _STALL_TIMEOUT_SECONDS, request.webpage_url,
                    )
                    await self._notify(f"Skipped {request.requester_name}'s song — playback stalled.")
                    break
        except asyncio.CancelledError:
            # now_playing is still set here; the finally below clears it.
            await self._notify(f"{request.requester_name}'s song was cut off — reconnecting the stream.")
            raise
        finally:
            with contextlib.suppress(ProcessLookupError):
                decoder.kill()
            await decoder.wait()
            self._current_decoder = None
            self._now_playing = None
