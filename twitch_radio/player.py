from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from twitch_radio.models import Track
from twitch_radio.paths import hidden_subprocess_kwargs
from twitch_radio.store import JsonStore
from twitch_radio.telemetry import counters

log = logging.getLogger(__name__)

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
_CHUNK_DURATION = 0.1
_CHUNK_BYTES = int(AUDIO_RATE * AUDIO_CHANNELS * 2 * _CHUNK_DURATION)
_SILENCE_CHUNK = b"\x00" * _CHUNK_BYTES
_STREAM_CHUNK_BYTES = 8192
# ~5-10s of Opus at typical bitrates; a stalled listener gets dropped, not buffered forever.
_SUBSCRIBER_QUEUE_SIZE = 50
# How far behind real time a listener typically hears /stream.opus (client
# buffering) — the overlay's title change is held back by this much so it
# doesn't jump ahead of what's actually audible. Tune to taste.
_OVERLAY_SYNC_DELAY_SECONDS = 4.0
# Sanity ceiling on captured Ogg header bytes (see _pump_encoder_output).
_MAX_OGG_HEADER_BYTES = 65536


def _iter_ogg_pages(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split complete Ogg pages off the front of `buf`; return them plus
    whatever incomplete tail is left."""
    pages: list[bytes] = []
    pos = 0
    n = len(buf)
    while True:
        if n - pos < 27 or buf[pos : pos + 4] != b"OggS":
            break
        segment_count = buf[pos + 26]
        header_len = 27 + segment_count
        if n - pos < header_len:
            break
        body_len = sum(buf[pos + 27 : pos + header_len])
        page_len = header_len + body_len
        if n - pos < page_len:
            break
        pages.append(buf[pos : pos + page_len])
        pos += page_len
    return pages, buf[pos:]


def _ogg_page_granule(page: bytes) -> int:
    return int.from_bytes(page[6:14], "little", signed=True)


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
# How long before a track ends to resolve the next one (network-only —
# no decoder yet, see _DECODER_PREP_LEAD_SECONDS below) so the actual
# resolve latency (~8-18s) is paid ahead of time rather than as part of
# the transition.
_PREFETCH_LEAD_SECONDS = 20.0
# How long before a track ends to spawn the next decoder and read its
# first chunk — deliberately much shorter than _PREFETCH_LEAD_SECONDS.
# ffmpeg's -re paces its *own* clock from the moment it's spawned, not
# from whenever we first read its output: a decoder spawned a full 20s
# early would sit idle that whole time yet still believe, by -re's own
# clock, that up to 20s of the track had already gone by — on a 3-4
# minute track that's a small, unnoticeable trim off the very end; on a
# short one it's the difference between "plays" and "skips itself
# entirely" (caught by hand: a synthetic 3s test track was reduced to
# under half a second of actual audio). Small enough that the same drift
# here is negligible, comfortably above the sub-second ffmpeg spawn +
# first-chunk time it needs to cover.
_DECODER_PREP_LEAD_SECONDS = 3.0


def _ffmpeg() -> str:
    """ffmpeg's full path when it can be found (the packaged app puts its
    bundled copy on PATH), else the bare name so the error still names it."""
    return shutil.which("ffmpeg") or "ffmpeg"


async def _spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
    """create_subprocess_exec, minus the console window Windows would
    otherwise flash for every ffmpeg launched by a windowless parent."""
    # mypy can't rule out a `program=` key inside **kwargs; callers only pass subprocess options.
    return await asyncio.create_subprocess_exec(*args, **hidden_subprocess_kwargs(), **kwargs)  # type: ignore[misc]


def _decoder_cmd(stream_url: str) -> list[str]:
    return [
        # fatal, not error: a mid-pull TLS reset is exactly what -reconnect
        # below recovers from on its own — logging it at "error" was just
        # noise on every transient CDN hiccup.
        _ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "fatal",
        # Reconnect flags recover from a dropped/hiccuping CDN connection
        # instead of corrupting the stream — including one that's gone
        # idle while a prepared-ahead decoder sat waiting for its turn to
        # play. -probesize/-analyzeduration skip ffmpeg's default
        # multi-second format probe, cutting startup latency.
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-reconnect_on_network_error",
        "1",
        "-reconnect_on_http_error",
        "429,500,502,503,504",
        "-probesize",
        "128k",
        "-analyzeduration",
        "0",
        "-re",
        "-i",
        stream_url,
        # Single-pass (dynamic) EBU R128 loudness normalization — every
        # track lands around the same perceived volume regardless of how
        # loud the original upload was, instead of a jump on every track
        # change. -16 LUFS sits between Spotify/YouTube's ~-14 and
        # broadcast's -23; -1.5 dBTP true-peak ceiling leaves headroom so
        # this never clips. Two-pass would sound marginally better but
        # needs the whole file decoded first — not compatible with
        # starting playback immediately.
        "-af",
        "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-f",
        "s16le",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        str(AUDIO_CHANNELS),
        "-",
    ]


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
    # Uploader/channel as reported by the resolve — shown in chat
    # announcements and available for matching already-queued entries by
    # uploader without a re-resolve (e.g. a future block-list). Empty
    # string when unknown.
    uploader: str = ""
    cancelled: bool = False
    on_start: Callable[[], None] | None = field(default=None, repr=False)
    # Only ever True on the resumed-track entry pause() builds — lets
    # _feed_loop start it immediately even if _pause_when_no_listeners
    # would otherwise defer, so an explicit !resume unambiguously resumes
    # rather than silently staying paused because nobody's connected yet.
    bypass_listener_pause: bool = False


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


@dataclass(slots=True)
class _PreparedNext:
    """A track resolved, decoded, and already producing audio ahead of
    time — built by _prepare_ahead() while the current track is still
    playing, so the transition to it can hand off with no silence gap.
    Discarded (decoder killed) if it turns out not to be what plays next
    — see _feed_loop's use of it."""

    request: QueuedRequest
    track: Track
    decoder: asyncio.subprocess.Process
    first_chunk: bytes


class RadioPlayer:
    """Owns one persistent ffmpeg encoder producing a continuous Opus stream
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

        # The one structure representing the queue's play order — also
        # what !queue, !position and the overlay read directly. (Used to
        # be split across this list plus a separate asyncio.Queue kept in
        # sync by convention; collapsing them removes a real class of bugs
        # from the two staying out of sync, and lets enqueue() reorder a
        # real request ahead of trailing radio-mix filler, which a FIFO
        # asyncio.Queue can't do.)
        self._pending: list[QueuedRequest] = []
        self._queue_store: JsonStore | None = None
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
        self._encoder_starts = 0
        self._resolving = False
        self._skip_pending = False
        self._notify_failure: Callable[[str], Awaitable[None]] | None = None
        self._duration_limit_getter: Callable[[], Awaitable[int]] | None = None
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        # Ogg ID/comment header pages for the current encoder session —
        # replayed to new subscribers in handle_stream(). See ogg_header_snapshot().
        self._ogg_header_bytes: bytes = b""
        self._ogg_header_ready: bool = False
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
        self._radio_played_notifier: Callable[[str], None] | None = None
        self._last_played_webpage_url: str | None = None
        self._radio_fill_task: asyncio.Task[None] | None = None
        # -inf, not 0.0: "no failure yet" must never look like "failed
        # right at boot" — time.monotonic() is seconds-since-boot on
        # Linux, so a literal 0.0 here would wrongly block the very first
        # radio-mix attempt on any system still under
        # _RADIO_RETRY_BACKOFF_SECONDS of uptime when the bot starts.
        self._radio_fill_failed_at: float = float("-inf")

        # Wall-clock deadline for the next silence chunk — see
        # _write_paced_silence(). Zero means "not pacing yet"; the first
        # call snaps it to now.
        self._silence_deadline: float = 0.0

        # Manual mod pause (!pause/!resume) — separate from
        # _pause_when_no_listeners above, which is automatic and driven by
        # subscriber count. Only ever set by an explicit chat command.
        self._paused = False

        # A track resolved, decoded and already producing audio ahead of
        # the current one ending, set by _prepare_ahead() — see
        # _PreparedNext. None means nothing's ready; _feed_loop falls back
        # to resolving fresh (a normal, silence-covered transition) in
        # that case, same as before this existed.
        self._prepared: _PreparedNext | None = None
        # Short-lived fire-and-forget tasks (currently just the now-playing
        # chat announcement) — held here only so asyncio can't garbage-
        # collect one mid-flight; see _fire_and_forget().
        self._background_tasks: set[asyncio.Task[None]] = set()

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
        """Requests still waiting to play — what !queue, !position and the
        overlay already read directly from the same list."""
        return len(self._pending)

    def queued_items(self) -> list[QueuedRequest]:
        return list(self._pending)

    @property
    def listener_count(self) -> int:
        """Open /stream.opus connections (OBS browser/media sources)."""
        return len(self._subscribers)

    @property
    def encoder_running(self) -> bool:
        encoder = self._encoder
        return encoder is not None and encoder.returncode is None

    @property
    def encoder_starts(self) -> int:
        """How many times ffmpeg's encoder has been launched this run; more
        than one means it died and was restarted."""
        return self._encoder_starts

    def positions_for(self, requester_id: int) -> list[int]:
        """1-indexed queue positions, in play order, for every one of
        requester_id's requests still waiting — empty if they have none
        waiting (check active_requester_id for the "up now" case)."""
        return [i + 1 for i, r in enumerate(self._pending) if r.requester_id == requester_id]

    def set_queue_store(self, store: JsonStore | None) -> None:
        """Persists the real (non-radio-filler) queue to disk on every
        change, restored by restore_queue() at startup — so a restart for
        an update doesn't wipe out everyone's queued requests. Radio-mix
        filler (requester_id == 0) is never persisted; it's regenerated
        on demand, not a real request worth keeping."""
        self._queue_store = store

    async def _persist_queue(self, front: QueuedRequest | None = None) -> None:
        if self._queue_store is None:
            return
        pending = ([front] if front is not None else []) + list(self._pending)
        items = [
            {
                "webpage_url": r.webpage_url,
                "requester_id": r.requester_id,
                "requester_name": r.requester_name,
                "title": r.title,
                "uploader": r.uploader,
            }
            for r in pending
            if r.requester_id != 0
        ]
        await self._queue_store.write({"items": items})

    async def restore_queue(self) -> int:
        """Reloads whatever set_queue_store() last persisted — call once at
        startup, before start(), so the queue's already populated by the
        time the feed loop takes its first tick. Returns how many were
        restored. stream_url is never persisted (see QueuedRequest) since
        it's re-resolved fresh from webpage_url at play time regardless —
        a restored entry is exactly as good as one just requested."""
        if self._queue_store is None:
            return 0
        data = await self._queue_store.read()
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            return 0
        restored = 0
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            try:
                webpage_url = str(item["webpage_url"])
                requester_id = int(item["requester_id"])
            except (KeyError, TypeError, ValueError):
                continue
            self.enqueue(
                QueuedRequest(
                    webpage_url=webpage_url,
                    requester_id=requester_id,
                    requester_name=str(item.get("requester_name") or "a viewer"),
                    title=str(item.get("title") or ""),
                    uploader=str(item.get("uploader") or ""),
                )
            )
            restored += 1
        return restored

    def enqueue(self, request: QueuedRequest) -> None:
        # A real request is inserted ahead of any trailing radio-mix
        # filler (requester_id == 0 — see radio.py's RadioSuggester,
        # never a real Twitch user ID) already queued, but still after
        # every other real request — a listener's own pick shouldn't have
        # to wait behind autoplay filler nobody actually asked for, but
        # two listeners' requests still play in the order they arrived.
        if request.requester_id != 0:
            index = next((i for i, r in enumerate(self._pending) if r.requester_id == 0), len(self._pending))
            self._pending.insert(index, request)
        else:
            self._pending.append(request)
        self._notify_state_changed()
        self._fire_and_forget(self._persist_queue(), name="persist-queue")

    def set_track_failure_notifier(self, notifier: Callable[[str], Awaitable[None]] | None) -> None:
        # Also used for the "Now Playing: ..." announcement on a
        # successful track start, not just failures — see _notify() and
        # _announce_now_playing(). One "post this plain string to chat,
        # best-effort" callback either way.
        self._notify_failure = notifier

    def set_duration_limit_getter(self, getter: Callable[[], Awaitable[int]] | None) -> None:
        self._duration_limit_getter = getter

    def set_radio_suggester(self, suggester: RadioSuggestFn | None) -> None:
        self._radio_suggest = suggester

    def set_radio_played_notifier(self, notifier: Callable[[str], None] | None) -> None:
        self._radio_played_notifier = notifier

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

    def ogg_header_snapshot(self) -> bytes:
        """Current session's Ogg header pages, or empty if not captured yet."""
        return self._ogg_header_bytes if self._ogg_header_ready else b""

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

    async def _notify_state_changed_delayed(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._notify_state_changed()

    def cancel_pending_for(self, requester_id: int) -> QueuedRequest | None:
        for request in reversed(self._pending):
            if request.requester_id == requester_id and not request.cancelled:
                request.cancelled = True
                with contextlib.suppress(ValueError):
                    self._pending.remove(request)
                if self._prepared is not None and self._prepared.request is request:
                    self._discard_prepared()
                if request.on_start is not None:
                    with contextlib.suppress(Exception):
                        request.on_start()
                    request.on_start = None
                self._notify_state_changed()
                self._fire_and_forget(self._persist_queue(), name="persist-queue")
                return request
        return None

    def purge_pending(self, predicate: Callable[[QueuedRequest], bool]) -> list[QueuedRequest]:
        """Removes every not-yet-playing request matching predicate.
        Doesn't touch whatever's currently playing/resolving. Used by
        !block (see components/song_requests.py) to strip a newly-blocked
        video out of the queue immediately."""
        removed = []
        for request in list(self._pending):
            if not predicate(request):
                continue
            request.cancelled = True
            with contextlib.suppress(ValueError):
                self._pending.remove(request)
            if self._prepared is not None and self._prepared.request is request:
                self._discard_prepared()
            if request.on_start is not None:
                with contextlib.suppress(Exception):
                    request.on_start()
                request.on_start = None
            removed.append(request)
        if removed:
            self._notify_state_changed()
            self._fire_and_forget(self._persist_queue(), name="persist-queue")
        return removed

    def skip_current(self) -> bool:
        if self._current_decoder is not None:
            with contextlib.suppress(ProcessLookupError):
                self._current_decoder.kill()
            counters.record("skips")
            return True
        if self._resolving:
            self._skip_pending = True
            counters.record("skips")
            return True
        return False

    def pause(self) -> bool:
        """Mod-only manual pause. Returns False if already paused.

        Interrupts playback/resolving immediately — deliberately doesn't
        wait for a track boundary like _pause_when_no_listeners does, since
        a mod reaching for !pause usually means "stop it right now", not
        "in four minutes when this song ends". The interrupted request (if
        any) is preserved and replayed from the top on resume(): inserted
        straight into _pending[0], the same place any other "play this
        next" request lives, rather than a separate field — see
        _feed_loop. No seek support anywhere in this pipeline (ffmpeg runs
        with -re, no -ss), so "resume" always means from 0:00."""
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
                bypass_listener_pause=True,
            )
            self._pending.insert(0, resumed)
            self._fire_and_forget(self._persist_queue(), name="persist-queue")
        self.skip_current()
        # Whatever was being prepared for after the interrupted track is no
        # longer next — the interrupted track is. Free its decoder now
        # rather than letting it idle for however long the pause lasts.
        self._discard_prepared()
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

    async def _notify_failed(self, message: str) -> None:
        counters.record("tracks_failed")
        await self._notify(message)

    def _discard_prepared(self) -> None:
        """Kills and drops whatever's in self._prepared, if anything —
        called whenever it turns out not to be what plays next (preempted
        by a real request, cancelled, or we're pausing/stopping)."""
        prepared, self._prepared = self._prepared, None
        if prepared is not None:
            with contextlib.suppress(ProcessLookupError):
                prepared.decoder.kill()

    def _fire_and_forget(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH — required to run the radio player.")
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever(), name="radio-player")

    def close_subscribers(self) -> None:
        """Ends every open /stream.opus connection by pushing the empty-bytes
        sentinel handle_stream() already understands. Without it a connected
        OBS source keeps its handler alive forever and the web server's
        shutdown waits out its whole grace period for nothing."""
        for q in list(self._subscribers):
            self._subscribers.discard(q)
            while True:
                try:
                    q.put_nowait(b"")
                    break
                except asyncio.QueueFull:
                    with contextlib.suppress(asyncio.QueueEmpty):
                        q.get_nowait()

    async def stop(self) -> None:
        self._stopping = True
        # A song that is playing (or loading) right now is not in _pending, so
        # a plain restart used to lose it. Put a real viewer request back at
        # the front of the saved queue unless it was about to end anyway.
        carry: QueuedRequest | None = self._active_request
        if carry is not None:
            np = self._now_playing
            nearly_done = (
                np is not None and np.duration > 0 and time.monotonic() - np.started_at >= np.duration - 15
            )
            if carry.requester_id == 0 or nearly_done or carry in self._pending:
                carry = None
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
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._discard_prepared()
        await self._kill_encoder()
        self.close_subscribers()
        if carry is not None:
            with contextlib.suppress(Exception):
                await self._persist_queue(front=carry)

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
            _ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(AUDIO_RATE),
            "-ac",
            str(AUDIO_CHANNELS),
            "-i",
            "-",
            # Opus over Ogg: Opus beats MP3 at the same bitrate (transparent
            # well under half MP3's bitrate — see AUDIO_BITRATE_KBPS in
            # .env), and Ogg is natively built for exactly this — an
            # unbounded live stream muxed page-by-page — where MP3 only
            # ever worked by omitting the tags/duration header it expects.
            "-c:a",
            "libopus",
            "-b:a",
            f"{self._audio_bitrate_kbps}k",
            "-vbr",
            "on",
            "-f",
            "ogg",
            "-",
        ]
        self._encoder = await _spawn(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        self._encoder_starts += 1
        log.info("Audio encoder started (%dkbps Opus).", self._audio_bitrate_kbps)

    async def _pump_encoder_output(self) -> None:
        assert self._encoder is not None and self._encoder.stdout is not None
        stdout = self._encoder.stdout
        # Reset per session — a restarted ffmpeg is a new logical bitstream.
        self._ogg_header_bytes = b""
        self._ogg_header_ready = False
        header_parse_buf = b""
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
            if not self._ogg_header_ready:
                header_parse_buf += chunk
                pages, header_parse_buf = _iter_ogg_pages(header_parse_buf)
                for page in pages:
                    if _ogg_page_granule(page) == 0:
                        self._ogg_header_bytes += page
                    else:
                        self._ogg_header_ready = True
                        break
                if len(self._ogg_header_bytes) > _MAX_OGG_HEADER_BYTES:
                    self._ogg_header_ready = True
                if self._ogg_header_ready:
                    header_parse_buf = b""
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
            if (
                not self._backoff_reset_done
                and time.monotonic() - self._encoder_spawned_at >= _STABLE_UPTIME_SECONDS
            ):
                self._backoff = _MIN_BACKOFF
                self._backoff_reset_done = True
            if self._paused:
                # Checked first, every tick — unlike _pause_when_no_listeners
                # below, this never falls through to a dequeue or reaches
                # _maybe_start_radio_fill(), so autoplay can't sneak a track
                # in while a mod has explicitly paused things.
                await self._write_paced_silence(encoder_stdin)
                continue
            about_to_resume = bool(self._pending) and self._pending[0].bypass_listener_pause
            if self._pending and not (
                self._pause_when_no_listeners and not self._subscribers and not about_to_resume
            ):
                request = self._pending.pop(0)
                self._fire_and_forget(self._persist_queue(), name="persist-queue")
            else:
                if not self._pending:
                    # Catches what the prefetch-timer hook doesn't: a skip
                    # or early end that empties the queue first. Guarded/
                    # deduped inside the method, so calling it every idle
                    # tick is cheap.
                    self._maybe_start_radio_fill()
                # Else: track-boundary pause only (not mid-track) — checked
                # fresh every loop tick, so playback resumes on its own the
                # instant a subscriber (re)connects.
                await self._write_paced_silence(encoder_stdin)
                continue
            if request.cancelled:
                if self._prepared is not None and self._prepared.request is request:
                    self._discard_prepared()
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

    async def _prepare_ahead(self, current_duration: int) -> None:
        """Best-effort: once playback is close to ending, gets whatever
        should play next fully ready — resolved, decoded, and already
        producing audio — so _feed_loop's handoff to it has no silence
        gap. Includes asking for a radio-mix pick if the queue is empty,
        so "gapless" applies to autoplay too, not just a queued request.

        Two separate waits, not one: the resolve (network-bound, wants a
        big lead — _PREFETCH_LEAD_SECONDS) and the decoder spawn (wants a
        small one — _DECODER_PREP_LEAD_SECONDS, see its comment for why).

        Stashed in self._prepared; only used if it's still what's
        actually next once this track ends (see _play_one_inner) — a real
        request can preempt a radio-mix pick at any point up to that
        moment (see enqueue()), and this notices rather than playing the
        stale one. Never raises; on any failure, or if nothing's ready in
        time, that transition just falls back to resolving fresh, exactly
        as if this didn't run at all.
        """
        started_at = time.monotonic()
        resolve_delay = max(0.0, current_duration - _PREFETCH_LEAD_SECONDS)
        # Cancelled cleanly by _play_one's finally when the track ends first.
        await asyncio.sleep(resolve_delay)
        if not self._pending:
            # Ask for a radio-mix pick ourselves — awaited, not the fire-
            # and-forget _maybe_start_radio_fill(), so there's actually
            # something here to prepare rather than hoping one turns up
            # in time. Same backoff so a broken radio source isn't retried
            # on every single track transition.
            if time.monotonic() - self._radio_fill_failed_at >= _RADIO_RETRY_BACKOFF_SECONDS:
                picked = await self._get_radio_pick()
                if picked is not None:
                    self.enqueue(picked)
                    log.info("Radio autoplay queued: %s", picked.title)
            if not self._pending:
                return
        candidate = self._pending[0]
        if candidate.cancelled:
            return
        track = await self._resolve_ahead(candidate)
        if track is None:
            return

        decoder_at = started_at + max(0.0, current_duration - _DECODER_PREP_LEAD_SECONDS)
        remaining = decoder_at - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        # Re-check before spawning: a real request can jump ahead of
        # radio-mix filler (enqueue()) at any point while the above was
        # sleeping/awaiting, and !remove can cancel `candidate` outright.
        if not self._pending or self._pending[0] is not candidate or candidate.cancelled:
            return
        prepared = await self._spawn_ahead(candidate, track)
        if prepared is None:
            return
        # One more re-check: the spawn itself just awaited too.
        if self._pending and self._pending[0] is candidate and not candidate.cancelled:
            self._prepared = prepared
        else:
            with contextlib.suppress(ProcessLookupError):
                prepared.decoder.kill()

    async def _resolve_ahead(self, request: QueuedRequest) -> Track | None:
        """The resolve+validate half of what _play_one_inner always ran
        inline, extracted so _prepare_ahead can run it early — network-
        only, no decoder yet (see _DECODER_PREP_LEAD_SECONDS for why
        that's spawned separately, later). Silent on failure (just a
        debug log) and touches none of the active-track state
        (_resolving, _current_decoder, _skip_pending) — this request
        isn't active yet, so a failure here isn't a user-facing event;
        _play_one_inner will notice nothing was prepared and resolve it
        fresh, with its usual chat notification if that fails too."""
        try:
            track = await self._resolver(request.webpage_url, request.requester_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Prepare-ahead resolve failed for %s (non-fatal).", request.webpage_url, exc_info=True)
            return None
        if track is None or track.is_live or request.cancelled:
            return None
        duration_limit = await self._current_duration_limit()
        if 0 < duration_limit < track.duration:
            return None
        return track

    async def _spawn_ahead(self, request: QueuedRequest, track: Track) -> _PreparedNext | None:
        """The decoder-spawn+first-chunk half — see _resolve_ahead."""
        decoder: asyncio.subprocess.Process | None = None
        try:
            try:
                decoder = await _spawn(*_decoder_cmd(track.stream_url), stdout=asyncio.subprocess.PIPE)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug(
                    "Prepare-ahead decoder spawn failed for %s (non-fatal).",
                    request.webpage_url,
                    exc_info=True,
                )
                return None
            assert decoder.stdout is not None
            try:
                first_chunk = await asyncio.wait_for(
                    decoder.stdout.read(_CHUNK_BYTES), timeout=_DECODER_START_TIMEOUT
                )
            except TimeoutError:
                return None
            if not first_chunk or request.cancelled:
                return None
            result = _PreparedNext(request=request, track=track, decoder=decoder, first_chunk=first_chunk)
            decoder = None  # ownership transferred to `result` — the finally below shouldn't kill it
            return result
        finally:
            if decoder is not None:
                with contextlib.suppress(ProcessLookupError):
                    decoder.kill()
                with contextlib.suppress(Exception):
                    await decoder.wait()

    def _maybe_start_radio_fill(self) -> None:
        """Kicks off a background radio-suggestion lookup when the queue is
        empty and nothing's already in flight — _feed_loop's idle-branch
        backstop for whenever _prepare_ahead's own attempt (above) didn't
        run or didn't catch it in time (e.g. a track too short for
        _PREFETCH_LEAD_SECONDS to matter, or radio toggled on mid-track).
        Guarded so the two can't double-queue a pick.

        _feed_loop can never reach this while paused, but _prepare_ahead
        is a background task scheduled minutes earlier and could still
        fire mid-pause, so the guard is repeated here too."""
        if self._paused or self._radio_fill_task is not None or self._pending:
            return
        if self._radio_suggest is None or self._last_played_webpage_url is None:
            return
        if time.monotonic() - self._radio_fill_failed_at < _RADIO_RETRY_BACKOFF_SECONDS:
            return
        self._radio_fill_task = asyncio.create_task(self._run_radio_fill(), name="radio-autoplay-fill")

    async def _run_radio_fill(self) -> None:
        try:
            picked = await self._get_radio_pick()
            if picked is not None:
                self.enqueue(picked)
                log.info("Radio autoplay queued: %s", picked.title)
        finally:
            self._radio_fill_task = None

    async def _get_radio_pick(self) -> QueuedRequest | None:
        """The actual "ask for one radio-autoplay suggestion" step, shared
        by _run_radio_fill's fire-and-forget backstop and _prepare_ahead's
        own direct attempt at a gapless autoplay transition. None (and the
        retry backoff armed) covers everything from "radio's off" to a
        failed lookup."""
        if self._radio_enabled_getter is not None:
            with contextlib.suppress(Exception):
                if not await self._radio_enabled_getter():
                    # Arm the backoff here too — otherwise the common
                    # "radio autoplay is off" state means both callers
                    # above retry every time they fire, for nothing.
                    self._radio_fill_failed_at = time.monotonic()
                    return None
        seed = self._last_played_webpage_url
        if seed is None or self._radio_suggest is None:
            return None
        try:
            picked = await self._radio_suggest(seed)
        except Exception:
            log.debug("Radio autoplay suggestion failed for %s (non-fatal).", seed, exc_info=True)
            picked = None
        if picked is None:
            self._radio_fill_failed_at = time.monotonic()
        return picked

    async def _current_duration_limit(self) -> int:
        if self._duration_limit_getter is None:
            return 0
        with contextlib.suppress(Exception):
            return await self._duration_limit_getter()
        return 0

    async def _play_one_inner(self, request: QueuedRequest, encoder_stdin: asyncio.StreamWriter) -> None:
        prepared, self._prepared = self._prepared, None
        if prepared is not None and (prepared.request is not request or request.cancelled):
            # Belonged to a different track than the one actually about to
            # play — a real request preempted it, or it was cancelled
            # while sitting ready. Not usable; free it and fall through to
            # resolving `request` fresh below, same as if nothing had
            # been prepared at all.
            with contextlib.suppress(ProcessLookupError):
                prepared.decoder.kill()
            prepared = None

        if prepared is not None:
            await self._start_and_stream(
                prepared.track, prepared.decoder, prepared.first_chunk, request, encoder_stdin
            )
            return

        self._skip_pending = False
        silence_task = asyncio.create_task(
            self._trickle_silence_until_cancelled(encoder_stdin), name="radio-player-prefeed-silence"
        )
        decoder: asyncio.subprocess.Process | None = None
        track: Track | None = None
        first_chunk = b""
        self._resolving = True
        try:
            try:
                track = await self._resolver(request.webpage_url, request.requester_id)
            except Exception:
                log.exception("Failed to re-resolve queued request: %s", request.webpage_url)
                await self._notify_failed(f"Couldn't load {request.requester_name}'s song — skipping it.")
                return
            if track is None:
                log.warning("Re-resolve returned nothing for %s — skipping", request.webpage_url)
                await self._notify_failed(f"Couldn't load {request.requester_name}'s song — skipping it.")
                return
            if track.is_live:
                log.warning("Re-resolve found %s is now live — skipping", request.webpage_url)
                await self._notify_failed(f"Skipped {request.requester_name}'s song — it's a livestream now.")
                return
            duration_limit = await self._current_duration_limit()
            if 0 < duration_limit < track.duration:
                log.warning(
                    "Re-resolve found %s now exceeds the duration cap — skipping", request.webpage_url
                )
                await self._notify_failed(
                    f"Skipped {request.requester_name}'s song — it's too long to play now."
                )
                return
            if self._skip_pending:
                self._skip_pending = False
                log.info("Skipped %s before it started playing (mid-resolve skip).", track.title)
                return

            decoder = await _spawn(*_decoder_cmd(track.stream_url), stdout=asyncio.subprocess.PIPE)
            self._current_decoder = decoder
            if self._skip_pending:
                self._skip_pending = False
                log.info("Skipped %s right after its decoder started (mid-spawn skip).", track.title)
                with contextlib.suppress(ProcessLookupError):
                    decoder.kill()
                await decoder.wait()
                self._current_decoder = None
                decoder = None
                return
            assert decoder.stdout is not None
            try:
                first_chunk = await asyncio.wait_for(
                    decoder.stdout.read(_CHUNK_BYTES), timeout=_DECODER_START_TIMEOUT
                )
            except TimeoutError:
                log.warning("Timed out waiting for decoder output for %s — skipping.", request.webpage_url)
                await self._notify_failed(
                    f"Skipped {request.requester_name}'s song — it took too long to start."
                )
                with contextlib.suppress(ProcessLookupError):
                    decoder.kill()
                await decoder.wait()
                self._current_decoder = None
                decoder = None
                return
        finally:
            self._resolving = False
            silence_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await silence_task

        if decoder is None or track is None:
            return
        await self._start_and_stream(track, decoder, first_chunk, request, encoder_stdin)

    async def _start_and_stream(
        self,
        track: Track,
        decoder: asyncio.subprocess.Process,
        first_chunk: bytes,
        request: QueuedRequest,
        encoder_stdin: asyncio.StreamWriter,
    ) -> None:
        """Common tail for both playback paths — a freshly resolved-and-
        spawned track (the slow path above) and one handed off from
        self._prepared (the gapless path) arrive here identically: a
        track, a live decoder, and its first chunk already in hand."""
        assert decoder.stdout is not None
        stdout = decoder.stdout
        self._current_decoder = decoder
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
        # Seed for the next radio-autoplay pick — set only once playback
        # is actually going ahead, so a track that fails to resolve/decode
        # never becomes a seed.
        self._last_played_webpage_url = track.webpage_url
        if self._radio_played_notifier is not None:
            self._radio_played_notifier(track.webpage_url)
        self._fire_and_forget(
            self._notify_state_changed_delayed(_OVERLAY_SYNC_DELAY_SECONDS), name="radio-player-overlay-sync"
        )
        log.info("Now playing: %s (requested by %s)", track.title, request.requester_name)
        self._fire_and_forget(
            self._announce_now_playing(request, track), name="radio-player-announce-now-playing"
        )
        if self._prefetch_enabled:
            self._prefetch_task = asyncio.create_task(
                self._prepare_ahead(track.duration), name="radio-player-prepare-ahead"
            )

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
                        _STALL_TIMEOUT_SECONDS,
                        request.webpage_url,
                    )
                    await self._notify_failed(f"Skipped {request.requester_name}'s song — playback stalled.")
                    break
        except asyncio.CancelledError:
            # now_playing is still set here; the finally below clears it.
            # Not on shutdown: the stream isn't reconnecting, it's ending.
            if not self._stopping:
                await self._notify(f"{request.requester_name}'s song was cut off — reconnecting the stream.")
            raise
        finally:
            with contextlib.suppress(ProcessLookupError):
                decoder.kill()
            await decoder.wait()
            self._current_decoder = None
            self._now_playing = None

    async def _announce_now_playing(self, request: QueuedRequest, track: Track) -> None:
        counters.record("tracks_played")
        if request.requester_id == 0:
            message = f"Now Playing: {track.title}"
        else:
            message = f"{request.requester_name}'s song request is Now Playing: {track.title}"
        await self._notify(message)
