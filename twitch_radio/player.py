from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from twitch_radio.models import Track
from twitch_radio.paths import hidden_subprocess_kwargs
from twitch_radio.store import JsonStore
from twitch_radio.telemetry import counters
from twitch_radio.youtube import youtube_video_id

log = logging.getLogger(__name__)

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
_CHUNK_DURATION = 0.1
_BYTES_PER_SECOND = AUDIO_RATE * AUDIO_CHANNELS * 2  # s16le
_CHUNK_BYTES = int(_BYTES_PER_SECOND * _CHUNK_DURATION)
_SILENCE_CHUNK = b"\x00" * _CHUNK_BYTES
_STREAM_CHUNK_BYTES = 8192
# ~5-10s of Opus at typical bitrates; a stalled listener gets dropped, not buffered forever.
_SUBSCRIBER_QUEUE_SIZE = 50

# The Opus encoder only runs while somebody is connected to /stream.opus, plus
# this long after the last listener leaves (so an OBS reconnect doesn't churn
# a process). With nobody listening, decoded audio is simply dropped.
_ENCODER_IDLE_GRACE_SECONDS = 30.0
# Idle poll interval while no encoder runs. Anything that matters (a new
# request, pause/resume, a listener arriving) wakes the loop immediately; this
# only bounds how stale the radio-autoplay and toggle checks can get.
_IDLE_TICK_SECONDS = 2.0
# libopus complexity. 10 is the default; 5 costs about a third less CPU for a
# difference nobody can hear at 160 kbps.
_OPUS_COMPLEXITY = 5

# Default for how far behind real time a listener hears /stream.opus: OBS (or the app's audio
# element) buffers what it receives before playing it. What the overlay and the dashboard show
# lags the player by this much, so they change when the sound does and not before. The setting is
# OVERLAY_DELAY_SECONDS.
_DEFAULT_OVERLAY_DELAY_SECONDS = 4.0
# A song ending and the next one starting closer together than this is one continuous stretch of
# sound, so the overlay doesn't flash "nothing playing" in between.
_NO_SONG_DEBOUNCE_SECONDS = 1.5
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
# How long before the current song ends the radio mix is asked for a pick when nothing at all is
# queued (so autoplay hands over without a gap). Requested songs never wait for this: they are
# prepared the moment they are queued (see _prepare_pass).
_PREFETCH_LEAD_SECONDS = 60.0
# Songs waiting in the queue are prepared ahead of time, nearest first:
#   - the first _RESOLVE_AHEAD get their stream address looked up (network only), and
#   - the first _WARM_AHEAD of those also get a decoder started with its first audio already
#     read ("warm"). The decoders have no -re (the player paces everything itself, see _pace), so
#     a warm one simply waits with a full pipe for its turn.
# A song is "ready" when it is warm, and only a ready song may take over from one that is cut short
# (see skip()). Deeper than this is not worth the extraction work and the idle connections.
_RESOLVE_AHEAD = 4
_WARM_AHEAD = 2
# Stream addresses are short-lived; one looked up this long ago is looked up again before it is
# used, and a decoder that has waited this long is replaced by a fresh one.
_RESOLVED_MAX_AGE_SECONDS = 1200.0
_WARM_MAX_AGE_SECONDS = 600.0
# A song that cannot be prepared is tried this many times (waiting _PREP_RETRY_DELAYS between
# attempts) before it is taken out of the queue, so one broken entry can never hold skip back.
_PREP_ATTEMPTS = 3
_PREP_RETRY_DELAYS = (3.0, 8.0)
# How long skip() waits for the next song to become ready before it gives up and refuses.
_SKIP_READY_WAIT_SECONDS = 5.0
# How long to wait for a killed decoder to be gone (see _reap).
_REAP_TIMEOUT_SECONDS = 3.0


def _ffmpeg() -> str:
    """ffmpeg's full path when it can be found (the packaged app puts its
    bundled copy on PATH), else the bare name so the error still names it."""
    return shutil.which("ffmpeg") or "ffmpeg"


async def _spawn(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
    """create_subprocess_exec, minus the console window Windows would
    otherwise flash for every ffmpeg launched by a windowless parent."""
    # mypy can't rule out a `program=` key inside **kwargs; callers only pass subprocess options.
    return await asyncio.create_subprocess_exec(*args, **hidden_subprocess_kwargs(), **kwargs)  # type: ignore[misc]


async def _reap(decoder: asyncio.subprocess.Process) -> None:
    """Kills a decoder and waits until it is really gone.

    Its output has to be read to the end first. asyncio stops reading a pipe nobody is reading
    (a decoder that sat ready, or was cut mid-song, has a full one), and wait() only returns once
    every pipe has reported its end - so a plain kill() + wait() can hang for good.
    """
    with contextlib.suppress(ProcessLookupError):
        decoder.kill()

    async def finish() -> None:
        if decoder.stdout is not None:
            while await decoder.stdout.read(65536):
                pass
        await decoder.wait()

    with contextlib.suppress(Exception):
        await asyncio.wait_for(finish(), _REAP_TIMEOUT_SECONDS)


def _decoder_cmd(stream_url: str) -> list[str]:
    cmd = [
        # fatal, not error: a mid-pull TLS reset is exactly what -reconnect
        # below recovers from on its own — logging it at "error" was just
        # noise on every transient CDN hiccup.
        _ffmpeg(),
        "-hide_banner",
        # The core's own stdin is the desktop app's control pipe, and a child
        # inherits it. Without this ffmpeg would read that pipe for its
        # interactive keys (and answer a stray line with a help prompt).
        "-nostdin",
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
        # No -re: the player paces the audio itself (see RadioPlayer._pace), against one clock
        # shared by every song and the silence between them. With -re each decoder kept its own
        # clock from the moment it was started, which made a decoder prepared ahead of time
        # burst when it was finally read.
        "-i",
        stream_url,
    ]
    # Songs play at their own volume: no -af chain, just PCM at the stream's rate and layout.
    return cmd + [
        "-f",
        "s16le",
        "-ar",
        str(AUDIO_RATE),
        "-ac",
        str(AUDIO_CHANNELS),
        "-",
    ]


def _encoder_cmd(audio_bitrate_kbps: int) -> list[str]:
    """The Opus-in-Ogg encoder: raw PCM in on stdin, an Ogg stream out on stdout."""
    return [
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
        f"{audio_bitrate_kbps}k",
        "-vbr",
        "on",
        "-compression_level",
        str(_OPUS_COMPLEXITY),
        "-f",
        "ogg",
        "-",
    ]


def _same_video(a: str | None, b: str | None) -> bool:
    """Same song, whichever URL shape each was written in (watch?v=, youtu.be/, music.youtube.com)."""
    if not a or not b:
        return False
    if a == b:
        return True
    first, second = youtube_video_id(a), youtube_video_id(b)
    return first is not None and first == second


class TrackResolver(Protocol):
    async def __call__(self, query: str, requester_id: int) -> Track | None: ...


class RadioSuggestFn(Protocol):
    async def __call__(self, seed_webpage_url: str) -> "QueuedRequest | None": ...


class RadioSuggestManyFn(Protocol):
    """Up to `count` radio-mix tracks that follow the seed, skipping `exclude_ids`."""

    async def __call__(
        self, seed_webpage_url: str, count: int, exclude_ids: Collection[str]
    ) -> "list[QueuedRequest]": ...


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
    # would otherwise defer, so an explicit resume unambiguously resumes
    # rather than silently staying paused because nobody's connected yet.
    bypass_listener_pause: bool = False
    # A picture for the dashboard's queue; None when unknown.
    thumbnail_url: str | None = None


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
class AudibleView:
    """What a listener hears right now, as opposed to what the player is doing right now: the
    overlay and the dashboard show this. `elapsed` counts from when `now` became audible, and
    `queue` includes a song that has already started in the player but is not yet audible."""

    now: NowPlaying | None
    elapsed: float
    queue: list[QueuedRequest]


# What a viewer or the dashboard is told when a skip is refused for want of a ready successor.
SKIP_NOT_READY_MESSAGE = (
    "The next song is still loading, so the current one keeps playing. Try again in a moment."
)


class SkipResult(str, Enum):
    """How a skip request ended (see RadioPlayer.skip)."""

    SKIPPED = "skipped"
    NOTHING_PLAYING = "nothing_playing"
    # The song that would play next is not ready yet, so the current one was left playing.
    NOT_READY = "not_ready"


@dataclass(slots=True)
class _ResolvedAhead:
    """A queued song whose stream address has been looked up ahead of its turn."""

    request: QueuedRequest
    track: Track
    at: float


@dataclass(slots=True)
class _PreparedNext:
    """A queued song that is ready to play: resolved, with its decoder running and its first chunk
    of audio already read. Built by _prepare_pass() while earlier songs are still playing, so the
    hand-off to it has no silence gap, and so a skip can go straight to it. Discarded (decoder
    killed) when it stops being one of the first songs in line, or gets too old."""

    request: QueuedRequest
    track: Track
    decoder: asyncio.subprocess.Process
    first_chunk: bytes
    ready_at: float = 0.0


class RadioPlayer:
    """Plays the queue and, while anyone is listening, runs one ffmpeg encoder
    producing a continuous Opus stream from resolved tracks + silence between
    them, fanned out to any number of HTTP subscribers (see
    subscribe()/unsubscribe()) — e.g. an OBS Media Source. The encoder starts
    with the first listener and stops shortly after the last one leaves;
    meanwhile the queue still advances in real time.
    """

    def __init__(
        self,
        *,
        resolver: TrackResolver,
        audio_bitrate_kbps: int,
        pause_when_no_listeners: bool = False,
        prefetch_enabled: bool = True,
        overlay_delay_seconds: float = _DEFAULT_OVERLAY_DELAY_SECONDS,
    ) -> None:
        self._resolver = resolver
        self._overlay_delay = max(0.0, float(overlay_delay_seconds))
        self._audio_bitrate_kbps = audio_bitrate_kbps
        self._pause_when_no_listeners = pause_when_no_listeners
        # Off only in tests that want the plain "resolve when its turn comes" path. With it off
        # nothing is ever ready ahead of time, so skip() cannot wait for readiness either.
        self._prefetch_enabled = prefetch_enabled
        self._prep_task: asyncio.Task[None] | None = None

        # The one structure representing the queue's play order — also
        # what !sq and the overlay read directly. (Used to
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
        # The decoder of a song that has been cut short (skip, pause) and is still being wound down.
        # What it had already produced sits in the pipe; the stream loop stops reading it at once
        # rather than playing out that tail, and a second skip finds the song already gone.
        self._cut: asyncio.subprocess.Process | None = None
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
        # Every byte the encoder has produced since it started, kept only
        # until the header is complete. A listener who connects in that
        # window is handed these (see subscribe()) so it still sees the
        # stream from its very first byte.
        self._ogg_prehistory: list[bytes] = []
        # Set when a listener arrives or leaves: wakes the encoder manager.
        self._subs_changed = asyncio.Event()
        # Set by anything the idle feed loop should react to at once.
        self._wake = asyncio.Event()
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
        # Radio-mix lookahead (see request_radio_lookahead()): keeps the next few radio-mix
        # songs queued behind whatever was requested. Only the queue entries exist ahead of
        # time; the resolve and decode prefetch still covers just the one song that is next.
        self._radio_suggest_many: RadioSuggestManyFn | None = None
        self._lookahead_getter: Callable[[], Awaitable[tuple[bool, int]]] | None = None
        self._lookahead_task: asyncio.Task[None] | None = None
        self._lookahead_again = False
        self._lookahead_failed_at: float = float("-inf")
        # -inf, not 0.0: "no failure yet" must never look like "failed
        # right at boot" — time.monotonic() is seconds-since-boot on
        # Linux, so a literal 0.0 here would wrongly block the very first
        # radio-mix attempt on any system still under
        # _RADIO_RETRY_BACKOFF_SECONDS of uptime when the bot starts.
        self._radio_fill_failed_at: float = float("-inf")

        # The one real-time clock the audio is paced against: the deadline for the next chunk,
        # whether that chunk is a piece of a song or silence. See _pace(). Zero means "not pacing
        # yet"; the first call snaps it to now.
        self._pace_deadline: float = 0.0
        # Bytes of the current song handed to the encoder so far; with the song's duration this
        # says how much of it is left, which is what decides when the next song is prepared.
        self._played_bytes = 0
        # Set whenever the queue or the player's state changes: wakes the preparation loop.
        self._prep_event = asyncio.Event()
        # Set whenever preparation made progress: wakes a skip() that is waiting for readiness.
        self._prep_progress = asyncio.Event()

        # What listeners hear, which trails what the player is doing by _overlay_delay. Each change
        # of the current song is recorded with the moment it becomes audible; see audible_view().
        self._audible: NowPlaying | None = None
        self._audible_since: float = 0.0
        self._audible_events: deque[tuple[float, NowPlaying | None]] = deque()

        # Manual pause (the desktop app's Pause/Resume) — separate from
        # _pause_when_no_listeners above, which is automatic and driven by
        # subscriber count. Only ever set by an explicit chat command.
        self._paused = False

        # Queued songs prepared ahead of time, keyed by id() of their QueuedRequest (which is
        # unhashable); see _prepare_pass(). _warm holds the ready ones. A song that is not warm when
        # its turn comes is resolved fresh (a normal, silence-covered transition), as it always was.
        self._resolved: dict[int, _ResolvedAhead] = {}
        self._warm: dict[int, _PreparedNext] = {}
        # Failed preparation attempts per queued song: (attempts so far, earliest next attempt).
        self._prep_attempts: dict[int, tuple[int, float]] = {}
        # A skip() waiting for its successor, shared by every skip asked for meanwhile.
        self._skip_waiter: asyncio.Task[SkipResult] | None = None
        # Set by skip() when the queue is empty and the radio mix would supply the next song.
        self._radio_pick_wanted = False
        # Short-lived fire-and-forget tasks (currently just the now-playing
        # chat announcement) — held here only so asyncio can't garbage-
        # collect one mid-flight; see _fire_and_forget().
        self._background_tasks: set[asyncio.Task[None]] = set()

    # -- public interface used by the chat bot / admin server ------------

    @property
    def now_playing(self) -> NowPlaying | None:
        return self._now_playing

    @property
    def overlay_delay_seconds(self) -> float:
        return self._overlay_delay

    def _set_now_playing(self, now: NowPlaying | None) -> None:
        """Every change of the current song goes through here: the player's own state changes at
        once, what listeners hear (see audible_view) _overlay_delay later."""
        self._now_playing = now
        self._audible_events.append((time.monotonic() + self._overlay_delay, now))
        if self._overlay_delay > 0:
            self._fire_and_forget(
                self._notify_state_changed_delayed(self._overlay_delay + 0.05),
                name="radio-player-overlay-sync",
            )
        self._notify_state_changed()

    def _audible_now(self) -> NowPlaying | None:
        now = time.monotonic()
        events = self._audible_events
        while events and events[0][0] <= now:
            due, song = events.popleft()
            if (
                song is None
                and events
                and events[0][1] is not None
                and events[0][0] - due < _NO_SONG_DEBOUNCE_SECONDS
            ):
                continue  # the next song follows right behind: no flash of "nothing playing"
            self._audible = song
            self._audible_since = due
        return self._audible

    def audible_view(self) -> AudibleView:
        """The song and queue as a listener of the stream experiences them. A request that arrives
        shows up in the queue at once; a song leaves the queue and becomes "now playing" when its
        first sound reaches the listener, and the song that just ended stays until its last does."""
        audible = self._audible_now()
        queue = list(self._pending)
        current = self._now_playing
        if current is not None and current is not audible:
            queue.insert(
                0,
                QueuedRequest(
                    webpage_url=current.webpage_url,
                    requester_id=current.requester_id,
                    requester_name=current.requester_name,
                    title=current.title,
                    uploader=current.uploader,
                    thumbnail_url=current.thumbnail_url,
                ),
            )
        elif current is None and self._active_request is not None:
            # Taken off the queue but still loading: it makes no sound yet, so for a listener it
            # is still the next song, not something that has already gone by.
            loading = self._active_request
            queue.insert(
                0,
                QueuedRequest(
                    webpage_url=loading.webpage_url,
                    requester_id=loading.requester_id,
                    requester_name=loading.requester_name,
                    title=loading.title,
                    uploader=loading.uploader,
                    thumbnail_url=loading.thumbnail_url,
                ),
            )
        elapsed = max(0.0, time.monotonic() - self._audible_since) if audible is not None else 0.0
        return AudibleView(now=audible, elapsed=elapsed, queue=queue)

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
        """Requests still waiting to play — what !sq and the
        overlay already read directly from the same list."""
        return len(self._pending)

    def is_already_requested(self, webpage_url: str) -> bool:
        """True when this song is playing (or loading) right now, or is waiting in the queue as
        someone's request. A radio-mix song with the same URL does not count: asking for it
        simply takes its place (see enqueue())."""
        if _same_video(self.active_webpage_url, webpage_url):
            return True
        return any(r.requester_id != 0 and _same_video(r.webpage_url, webpage_url) for r in self._pending)

    def real_queue_size(self) -> int:
        """Requests someone actually made, without the radio-mix filler - what the queue cap and
        the "N in queue" a viewer is told count, so autoplay can never crowd out a request."""
        return sum(1 for request in self._pending if request.requester_id != 0)

    def queued_items(self) -> list[QueuedRequest]:
        return list(self._pending)

    # -- readiness of queued songs -------------------------------------------

    @staticmethod
    def _decoder_usable(decoder: asyncio.subprocess.Process) -> bool:
        # Still running (waiting with a full pipe), or finished cleanly with everything it
        # produced still sitting in the pipe. Anything else died on the way.
        return decoder.returncode is None or decoder.returncode == 0

    def is_ready(self, request: QueuedRequest) -> bool:
        """True when this queued song can start playing right now: resolved, decoder running,
        first audio in hand."""
        entry = self._warm.get(id(request))
        return entry is not None and entry.request is request and self._decoder_usable(entry.decoder)

    def _live_pending(self) -> list[QueuedRequest]:
        return [r for r in self._pending if not r.cancelled]

    def _is_queued(self, request: QueuedRequest) -> bool:
        return any(r is request for r in self._pending)

    @property
    def next_ready(self) -> bool:
        """False while the song that plays after the current one is still being prepared (a skip
        would be refused). True when it is ready or when nothing is waiting."""
        if not self._prefetch_enabled:
            return True
        live = self._live_pending()
        return not live or self.is_ready(live[0])

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
                "thumbnail_url": r.thumbnail_url,
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
                    thumbnail_url=str(item.get("thumbnail_url") or "") or None,
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
            # If the radio mix had already lined this song up, the request replaces that entry.
            for filler in [
                r
                for r in self._pending
                if r.requester_id == 0 and _same_video(r.webpage_url, request.webpage_url)
            ]:
                filler.cancelled = True
                self._pending.remove(filler)
                self._forget(filler)
            index = next((i for i, r in enumerate(self._pending) if r.requester_id == 0), len(self._pending))
            self._pending.insert(index, request)
        else:
            self._pending.append(request)
        self._notify_state_changed()
        self._fire_and_forget(self._persist_queue(), name="persist-queue")
        if request.requester_id != 0:
            # Someone asked for a song: line up what the radio would play after it.
            self.request_radio_lookahead()

    # -- radio-mix lookahead ------------------------------------------------

    def request_radio_lookahead(self) -> None:
        """Asks for the radio-mix queue to be topped up, soon and in the background. Safe to call
        from anywhere on the event loop and as often as you like: calls made while a top-up is
        running collapse into one more pass afterwards."""
        if self._radio_suggest_many is None or self._lookahead_getter is None or self._stopping:
            return
        if self._lookahead_task is not None and not self._lookahead_task.done():
            self._lookahead_again = True
            return
        self._lookahead_task = asyncio.create_task(self._run_radio_lookahead(), name="radio-lookahead")

    async def apply_radio_lookahead(self) -> None:
        """The lookahead setting or the auto-radio switch changed: drop radio filler that is no
        longer wanted (all of it when either was turned off, the far end when the count went
        down), then top up."""
        if self._lookahead_getter is None:
            return
        try:
            enabled, count = await self._lookahead_getter()
            if enabled and self._radio_enabled_getter is not None:
                enabled = await self._radio_enabled_getter()
        except Exception:
            log.debug("Reading the radio lookahead setting failed.", exc_info=True)
            return
        self._lookahead_failed_at = float("-inf")
        radio_items = [r for r in self._pending if r.requester_id == 0]
        keep = count if enabled else 0
        surplus = radio_items[keep:]
        if surplus:
            self.purge_pending(lambda request: any(request is item for item in surplus))
            log.info("Radio lookahead: removed %d queued radio-mix song(s).", len(surplus))
        if enabled:
            self.request_radio_lookahead()

    async def _run_radio_lookahead(self) -> None:
        try:
            while True:
                self._lookahead_again = False
                await self._top_up_radio_lookahead()
                if not self._lookahead_again:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Radio lookahead failed (non-fatal).", exc_info=True)

    async def _top_up_radio_lookahead(self) -> None:
        assert self._lookahead_getter is not None and self._radio_suggest_many is not None
        if self._paused:
            return
        if time.monotonic() - self._lookahead_failed_at < _RADIO_RETRY_BACKOFF_SECONDS:
            return
        enabled, count = await self._lookahead_getter()
        if not enabled:
            return
        # Lookahead builds on auto-radio: with that switched off, no radio song is queued at all.
        if self._radio_enabled_getter is not None and not await self._radio_enabled_getter():
            return
        need = count - sum(1 for r in self._pending if r.requester_id == 0)
        if need <= 0:
            return
        # Follow the end of the queue; with an empty queue, follow what is playing or just played.
        seed = (
            self._pending[-1].webpage_url
            if self._pending
            else self.active_webpage_url or self._last_played_webpage_url
        )
        if seed is None:
            return
        taken = {youtube_video_id(r.webpage_url) for r in self._pending}
        taken.add(youtube_video_id(self.active_webpage_url or ""))
        picks = await self._radio_suggest_many(seed, need, {v for v in taken if v})
        if not picks:
            self._lookahead_failed_at = time.monotonic()
            return
        # The queue may have changed during the lookup (a request arrived, a song started):
        # work out again how many are still wanted before adding anything.
        need = count - sum(1 for r in self._pending if r.requester_id == 0)
        added = 0
        for pick in picks[: max(0, need)]:
            if self._paused or self._stopping:
                break
            self.enqueue(pick)
            added += 1
        if added:
            log.info("Radio lookahead: queued %d song(s), next: %s", added, picks[0].title)

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

    def set_radio_lookahead(
        self,
        suggest_many: RadioSuggestManyFn | None,
        getter: Callable[[], Awaitable[tuple[bool, int]]] | None,
    ) -> None:
        """`getter` returns (enabled, count) and is read fresh every time, so the dashboard's
        setting takes effect without a restart."""
        self._radio_suggest_many = suggest_many
        self._lookahead_getter = getter

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
        """A queue of encoder output. The first listener starts the encoder.

        Call ogg_header_snapshot() straight after (no await in between): it is
        empty while the header is still being captured, in which case this
        queue has already been seeded with everything produced so far."""
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        if not self._ogg_header_ready:
            for chunk in self._ogg_prehistory:
                q.put_nowait(chunk)
        self._subscribers.add(q)
        self._subs_changed.set()
        self._wake.set()
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        self._subscribers.discard(q)
        self._subs_changed.set()

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
        # Everything that changes what the feed loop should do (a request
        # arriving, pause/resume, a cancel) also lands here.
        self._wake.set()
        self._prep_event.set()
        for q in list(self._state_subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)

    async def _notify_state_changed_delayed(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._notify_state_changed()

    def purge_pending(self, predicate: Callable[[QueuedRequest], bool]) -> list[QueuedRequest]:
        """Removes every not-yet-playing request matching predicate.
        Doesn't touch whatever's currently playing/resolving. Used by
        the desktop app's "clear queue" command (see service.py)."""
        removed = []
        for request in list(self._pending):
            if not predicate(request):
                continue
            request.cancelled = True
            with contextlib.suppress(ValueError):
                self._pending.remove(request)
            self._forget(request)
            if request.on_start is not None:
                with contextlib.suppress(Exception):
                    request.on_start()
                request.on_start = None
            removed.append(request)
        if removed:
            self._notify_state_changed()
            self._fire_and_forget(self._persist_queue(), name="persist-queue")
        return removed

    def _interrupt_current(self) -> bool:
        """Cuts the current song (or the load of it) short, right now and without asking whether
        anything is ready to follow. skip() is the gated way in; pause() uses this directly."""
        decoder = self._current_decoder
        if decoder is not None:
            if decoder is self._cut:
                return True  # already being cut: that is one skip, not two
            self._cut = decoder
            with contextlib.suppress(ProcessLookupError):
                decoder.kill()
            counters.record("skips")
            return True
        if self._resolving:
            if not self._skip_pending:
                self._skip_pending = True
                counters.record("skips")
            return True
        return False

    async def skip(self, wait: float = _SKIP_READY_WAIT_SECONDS) -> SkipResult:
        """Skips the current song, but only once the song that follows it is ready to play.

        Cutting a song short while its successor is still being looked up or decoded would leave a
        silent gap, and everything that follows the player (the overlay, the dashboard, the queue)
        would be showing a song listeners cannot hear yet - and fast repeated skips would leave
        them further and further ahead of the sound. So when the successor is not ready, this
        waits up to `wait` seconds for it (nudging its preparation to the front) and, if it still is
        not, leaves the current song playing and returns NOT_READY. With nothing queued there is no
        successor to wait for, unless the radio mix is about to supply one.

        Skips asked for while one is waiting join it instead of each cutting another song.
        """
        if self._skip_waiter is not None:
            return await asyncio.shield(self._skip_waiter)
        if self._current_decoder is None and not self._resolving:
            return SkipResult.NOTHING_PLAYING
        if not self._prefetch_enabled or self._prep_task is None:
            return SkipResult.SKIPPED if self._interrupt_current() else SkipResult.NOTHING_PLAYING
        waiter = asyncio.ensure_future(self._skip_when_ready(wait))
        self._skip_waiter = waiter
        waiter.add_done_callback(self._skip_waiter_done)
        return await asyncio.shield(waiter)

    def _skip_waiter_done(self, waiter: asyncio.Future[SkipResult]) -> None:
        if self._skip_waiter is waiter:
            self._skip_waiter = None
        if not waiter.cancelled():
            waiter.exception()  # retrieved: nobody may be left waiting on it

    async def _skip_when_ready(self, wait: float) -> SkipResult:
        target = self._active_request
        deadline = time.monotonic() + wait
        try:
            while True:
                if self._paused:
                    return SkipResult.NOTHING_PLAYING
                if self._active_request is not target:
                    return SkipResult.SKIPPED  # it ended while we waited: never cut the next one short
                if self._current_decoder is None and not self._resolving:
                    return SkipResult.NOTHING_PLAYING
                live = self._live_pending()
                if not live:
                    if not await self._radio_may_follow():
                        break  # nothing will follow: no successor to run ahead of
                    self._radio_pick_wanted = True
                elif self.is_ready(live[0]):
                    break
                self._prep_event.set()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return SkipResult.NOT_READY
                self._prep_progress.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._prep_progress.wait(), min(remaining, 0.5))
            # Same turn of the event loop as the check above: the song that follows is ready.
            return SkipResult.SKIPPED if self._interrupt_current() else SkipResult.NOTHING_PLAYING
        finally:
            self._radio_pick_wanted = False

    async def _radio_may_follow(self) -> bool:
        """True when the radio mix would pick the next song if the queue stayed empty."""
        if self._radio_suggest is None or self._last_played_webpage_url is None:
            return False
        if time.monotonic() - self._radio_fill_failed_at < _RADIO_RETRY_BACKOFF_SECONDS:
            return False
        if self._radio_enabled_getter is None:
            return True
        with contextlib.suppress(Exception):
            return bool(await self._radio_enabled_getter())
        return False

    def pause(self) -> bool:
        """Manual pause (desktop app). Returns False if already paused.

        Interrupts playback/resolving immediately — deliberately doesn't
        wait for a track boundary like _pause_when_no_listeners does, since
        pressing Pause usually means "stop it right now", not
        "in four minutes when this song ends". The interrupted request (if
        any) is preserved and replayed from the top on resume(): inserted
        straight into _pending[0], the same place any other "play this
        next" request lives, rather than a separate field — see
        _feed_loop. No seek support anywhere in this pipeline (the decoder
        is never given -ss), so "resume" always means from 0:00."""
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
                thumbnail_url=self._now_playing.thumbnail_url if self._now_playing else active.thumbnail_url,
                bypass_listener_pause=True,
            )
            self._pending.insert(0, resumed)
            self._fire_and_forget(self._persist_queue(), name="persist-queue")
        self._interrupt_current()
        # Whatever was being prepared for after the interrupted track is no
        # longer next — the interrupted track is. Free its decoders now
        # rather than letting them idle for however long the pause lasts.
        self._discard_warm()
        self._notify_state_changed()
        return True

    def resume(self) -> bool:
        """Returns False if not currently paused (nothing changed)."""
        if not self._paused:
            return False
        self._paused = False
        self._notify_state_changed()
        return True

    async def _notify(self, message: str) -> None:
        if self._notify_failure is None:
            return
        with contextlib.suppress(Exception):
            await self._notify_failure(message)

    async def _notify_failed(self, message: str) -> None:
        counters.record("tracks_failed")
        await self._notify(message)

    def _discard_warm(
        self, request: QueuedRequest | None = None, *, reap: bool = True
    ) -> list[asyncio.subprocess.Process]:
        """Kills and drops the ready decoder of one queued song, or of all of them - called
        whenever one turns out not to be among the next to play (preempted by a real request,
        cancelled, too old) or we're pausing/stopping."""
        if request is None:
            entries = list(self._warm.values())
            self._warm.clear()
        else:
            entry = self._warm.pop(id(request), None)
            entries = [entry] if entry is not None else []
        decoders = [entry.decoder for entry in entries]
        for decoder in decoders:
            with contextlib.suppress(ProcessLookupError):
                decoder.kill()
            if reap:
                self._fire_and_forget(_reap(decoder), name="radio-player-reap")
        return decoders

    def _forget(self, request: QueuedRequest) -> None:
        """Drops everything prepared for a request that has left the queue."""
        self._resolved.pop(id(request), None)
        self._prep_attempts.pop(id(request), None)
        self._discard_warm(request)

    def _fire_and_forget(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def start(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH — required to run the radio player.")
        self._stopping = False
        self._task = asyncio.create_task(self._run_forever(), name="radio-player")
        if self._prefetch_enabled:
            self._prep_task = asyncio.create_task(self._prep_loop(), name="radio-player-prepare")

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
        self._subs_changed.set()

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
        for task in (self._prep_task, self._skip_waiter):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._prep_task = None
        self._skip_waiter = None
        if self._radio_fill_task is not None:
            self._radio_fill_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._radio_fill_task
            self._radio_fill_task = None
        if self._lookahead_task is not None:
            self._lookahead_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._lookahead_task
            self._lookahead_task = None
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await asyncio.gather(*(_reap(d) for d in self._discard_warm(reap=False)), return_exceptions=True)
        self._resolved.clear()
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
        encoder, self._encoder = self._encoder, None
        if encoder is not None:
            with contextlib.suppress(ProcessLookupError):
                encoder.kill()
            with contextlib.suppress(Exception):
                await encoder.wait()
        self._reset_ogg_state()

    def _reset_ogg_state(self) -> None:
        self._ogg_header_bytes = b""
        self._ogg_header_ready = False
        self._ogg_prehistory = []

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
        self._encoder_spawned_at = time.monotonic()  # session start; see _STABLE_UPTIME_SECONDS
        self._backoff_reset_done = False
        feed_task = asyncio.create_task(self._feed_loop())
        manager_task = asyncio.create_task(self._encoder_manager(), name="radio-player-encoder-manager")
        try:
            done, pending = await asyncio.wait({feed_task, manager_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
        finally:
            feed_task.cancel()
            manager_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(feed_task, manager_task, return_exceptions=True)

    async def _encoder_manager(self) -> None:
        """Keeps the encoder running exactly while someone is listening (plus a
        short grace period), and raises if it dies on its own so the session
        restarts through the usual backoff path."""
        loop = asyncio.get_running_loop()
        pump: asyncio.Task[None] | None = None
        empty_since: float | None = None
        try:
            while True:
                # Cleared before looking at the state, so a change that lands
                # while we are busy below leaves the event set and the wait at
                # the bottom returns at once.
                self._subs_changed.clear()
                if self._subscribers:
                    empty_since = None
                    if self._encoder is None:
                        await self._spawn_encoder()
                        assert self._encoder is not None
                        pump = asyncio.create_task(
                            self._pump_encoder_output(self._encoder), name="radio-player-encoder-pump"
                        )
                elif self._encoder is not None:
                    now = loop.time()
                    if empty_since is None:
                        empty_since = now
                    if now - empty_since >= _ENCODER_IDLE_GRACE_SECONDS:
                        await self._retire_encoder(pump)
                        pump = None
                        empty_since = None

                timeout: float | None = None
                if not self._subscribers and self._encoder is not None and empty_since is not None:
                    timeout = max(0.1, _ENCODER_IDLE_GRACE_SECONDS - (loop.time() - empty_since))
                waiter = asyncio.ensure_future(self._subs_changed.wait())
                watched: set[asyncio.Future[Any]] = {waiter}
                if pump is not None:
                    watched.add(pump)
                try:
                    await asyncio.wait(watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
                if pump is not None and pump.done():
                    pump.result()  # raises when the encoder died; ends the session
        finally:
            if pump is not None and not pump.done():
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pump

    async def _retire_encoder(self, pump: asyncio.Task[None] | None) -> None:
        """Stops the encoder because nobody is listening. `self._encoder` is
        cleared first so a write already in flight sees it was retired on
        purpose rather than treating the broken pipe as a crash."""
        encoder, self._encoder = self._encoder, None
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
        self._reset_ogg_state()
        if encoder is not None:
            with contextlib.suppress(Exception):
                if encoder.stdin is not None:
                    encoder.stdin.close()
            with contextlib.suppress(ProcessLookupError):
                encoder.kill()
            with contextlib.suppress(Exception):
                await encoder.wait()
        log.info("Audio encoder stopped (nobody is listening).")

    async def _spawn_encoder(self) -> None:
        cmd = _encoder_cmd(self._audio_bitrate_kbps)
        self._reset_ogg_state()
        self._encoder = await _spawn(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        self._encoder_starts += 1
        log.info("Audio encoder started (%dkbps Opus).", self._audio_bitrate_kbps)

    async def _pump_encoder_output(self, encoder: asyncio.subprocess.Process) -> None:
        assert encoder.stdout is not None
        stdout = encoder.stdout
        # Per encoder — a restarted ffmpeg is a new logical bitstream
        # (_spawn_encoder already reset the header state).
        header_parse_buf = b""
        while True:
            chunk = await stdout.read(_STREAM_CHUNK_BYTES)
            if not chunk:
                # EOF means ffmpeg itself exited (crashed, OOM-killed) —
                # stop() and _retire_encoder() cancel this task directly
                # before a read could return empty on purpose. Raising
                # routes this through the same backoff/restart path as any
                # other encoder failure, with a log line explaining why.
                raise RuntimeError(f"Encoder stdout closed unexpectedly (exit code {encoder.returncode})")
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
            if self._ogg_header_ready:
                self._ogg_prehistory = []
            else:
                self._ogg_prehistory.append(chunk)
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
        while not self._stopping:
            encoder = self._encoder
            if encoder is not None and encoder.returncode is not None:
                raise RuntimeError(f"Encoder exited with code {encoder.returncode}")
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
                await self._write_paced_silence()
                continue
            about_to_resume = bool(self._pending) and self._pending[0].bypass_listener_pause
            if self._pending and not (
                self._pause_when_no_listeners and not self._subscribers and not about_to_resume
            ):
                request = self._pending.pop(0)
                self._fire_and_forget(self._persist_queue(), name="persist-queue")
                # One slot freed: let the lookahead refill the radio queue behind it.
                self.request_radio_lookahead()
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
                await self._write_paced_silence()
                continue
            if request.cancelled:
                self._forget(request)
                continue
            if request.on_start is not None:
                with contextlib.suppress(Exception):
                    request.on_start()
            await self._play_one(request)

    async def _write_pcm(self, chunk: bytes) -> None:
        """Hands decoded audio to the encoder. With nobody listening there is
        no encoder, and the audio is dropped: the decoder's own -re pacing
        already keeps a track playing in real time."""
        encoder = self._encoder
        if encoder is None or encoder.stdin is None:
            return
        try:
            encoder.stdin.write(chunk)
            await encoder.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            if encoder is self._encoder:
                raise  # it died on its own: the session restarts
            # else: retired on purpose while this write was in flight

    async def _idle_wait(self, timeout: float) -> None:
        """Sleeps until something wakes the feed loop (see _wake) or `timeout`."""
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout)

    async def _write_paced_silence(self) -> None:
        """Writes one silence chunk and sleeps until the *deadline* for the
        next one, rather than a flat _CHUNK_DURATION sleep.

        The flat-sleep version fed 0.1s of audio per (0.1s + write + event-
        loop latency) of wall clock, so the encoder ran slower than real
        time whenever silent — ~0.3% on an idle box (~11s of listener-buffer
        drain per hour), worse under load. OBS's Media Source pays for it as
        a progressive underrun.

        Accumulating against a monotonic deadline lets a late tick borrow
        from the next one instead of building permanent debt. The max()
        clamps the deadline forward after a long gap (a track just ended,
        the loop was paused) so it never "catches up" by dumping a burst
        of silence into the encoder.

        With no encoder (nobody listening) there is nothing to keep
        continuous, so this just waits for something to happen.
        """
        if self._encoder is None:
            await self._idle_wait(_IDLE_TICK_SECONDS)
            return
        await self._write_pcm(_SILENCE_CHUNK)
        await self._pace(_CHUNK_DURATION)

    async def _pace(self, seconds: float) -> None:
        """Accounts for `seconds` of audio just handed to the encoder and sleeps until the next
        chunk is due. Songs and silence share this one clock, so the stream never runs fast
        (a burst when a decoder prepared ahead of time is finally read) or slow (drift), and the
        hand-off from one song to the next is just the next chunk on the same schedule.

        A late tick borrows from the next one instead of building debt, and the deadline is clamped
        forward after a long gap (idle, a stall) so it never catches up with a burst."""
        self._pace_deadline = max(self._pace_deadline + seconds, time.monotonic())
        delay = self._pace_deadline - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _trickle_silence_until_cancelled(self) -> None:
        try:
            while True:
                await self._write_paced_silence()
        except asyncio.CancelledError:
            raise

    async def _play_one(self, request: QueuedRequest) -> None:
        self._active_request = request
        self._notify_state_changed()
        try:
            await self._play_one_inner(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error playing queued request: %s", request.webpage_url)
        finally:
            self._active_request = None
            self._notify_state_changed()  # also wakes the preparation loop: the line moved up

    def _remaining_seconds(self, duration: float) -> float:
        """How much of the current song is left, by how much of it has actually been played
        (not by the clock, which a stalled source would put ahead of the audio)."""
        return duration - self._played_bytes / _BYTES_PER_SECOND

    # -- preparing queued songs ahead of time ---------------------------------

    async def _prep_loop(self) -> None:
        """Runs for the player's whole life, beside whatever is playing. Keeps the songs at the
        front of the queue resolved and, for the first few, decoded and ready (see _prepare_pass),
        starting the moment they are queued rather than shortly before they are needed. Wakes on
        every queue or state change (_prep_event). Never raises."""
        while not self._stopping:
            busy = False
            try:
                self._prep_event.clear()
                busy = await self._prepare_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("Preparing queued songs failed (non-fatal).", exc_info=True)
                await asyncio.sleep(1.0)
            self._prep_progress.set()
            if busy:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._prep_event.wait(), _IDLE_TICK_SECONDS)

    def _feed_loop_starts_head_itself(self) -> bool:
        """True while the player is idle and the feed loop will start the first queued song within
        a tick: preparing that one here as well would only duplicate what it is about to do."""
        return (
            not self._paused
            and self._now_playing is None
            and self._active_request is None
            and not (self._pause_when_no_listeners and not self._subscribers)
        )

    def _fresh_resolved(self, request: QueuedRequest) -> _ResolvedAhead | None:
        entry = self._resolved.get(id(request))
        if entry is None or entry.request is not request:
            return None
        if time.monotonic() - entry.at > _RESOLVED_MAX_AGE_SECONDS:
            return None
        return entry

    def _prune(self, live: list[QueuedRequest]) -> None:
        """Drops preparation that no longer fits the queue: songs that left it, decoders that died
        or grew old, and warm decoders of songs that are no longer among the first in line."""
        now = time.monotonic()
        live_ids = {id(r) for r in live}
        warm_ids = {id(r) for r in live[:_WARM_AHEAD]}
        for key in [
            k
            for k, e in self._resolved.items()
            if k not in live_ids or now - e.at > _RESOLVED_MAX_AGE_SECONDS
        ]:
            del self._resolved[key]
        for key in [k for k in self._prep_attempts if k not in live_ids]:
            del self._prep_attempts[key]
        for key, entry in list(self._warm.items()):
            if (
                key not in warm_ids
                or not self._decoder_usable(entry.decoder)
                or now - entry.ready_at > _WARM_MAX_AGE_SECONDS
            ):
                self._discard_warm(entry.request)

    async def _prepare_pass(self) -> bool:
        """One step of preparation, nearest song first. Returns True when it did work, so the loop
        goes round again at once; False when everything wanted is done (or waiting to be retried).

        The first _WARM_AHEAD queued songs are made ready (resolved, decoder running, first chunk
        read); the next ones up to _RESOLVE_AHEAD only get their stream address. It looks at the
        queue afresh after every step, so a request that jumps ahead, or a skip, changes what is
        prepared next. Whatever is warm is used when its song's turn comes (see _play_one_inner)
        and is what makes skip() safe."""
        live = self._live_pending()
        self._prune(live)
        if self._paused:
            return False
        now = time.monotonic()
        idle_head = self._feed_loop_starts_head_itself()
        for index, request in enumerate(live[:_RESOLVE_AHEAD]):
            if index == 0 and idle_head:
                continue
            if self._prep_attempts.get(id(request), (0, 0.0))[1] > now:
                continue
            warm = index < _WARM_AHEAD
            if warm and self.is_ready(request):
                continue
            if not warm and self._fresh_resolved(request) is not None:
                continue
            await self._prepare_one(request, warm=warm)
            return True
        if not live:
            return await self._prepare_radio_pick()
        return False

    async def _prepare_one(self, request: QueuedRequest, *, warm: bool) -> None:
        key = id(request)
        resolved = self._fresh_resolved(request)
        if resolved is None:
            track, problem = await self._resolve_ahead(request)
            if request.cancelled or not self._is_queued(request):
                return  # it left the queue while we were looking it up
            if track is None:
                await self._prep_failed(request, problem)
                return
            resolved = _ResolvedAhead(request=request, track=track, at=time.monotonic())
            self._resolved[key] = resolved
            if not warm:
                self._prep_attempts.pop(key, None)
                return
        if not warm:
            return
        entry = await self._spawn_ahead(request, resolved.track)
        in_window = any(r is request for r in self._live_pending()[:_WARM_AHEAD])
        if entry is None:
            self._resolved.pop(key, None)  # the address may be what is wrong: look it up again
            if in_window:
                await self._prep_failed(request, "unavailable")
            return
        if not in_window or request.cancelled or key in self._warm:
            self._fire_and_forget(
                _reap(entry.decoder), name="radio-player-reap"
            )  # the line changed meanwhile
            return
        entry.ready_at = time.monotonic()
        self._warm[key] = entry
        self._prep_attempts.pop(key, None)
        self._prep_progress.set()

    async def _prep_failed(self, request: QueuedRequest, problem: str | None) -> None:
        """A queued song could not be prepared. Transient trouble is retried a few times with a
        pause in between; a song that turns out to be a livestream or too long is dropped at once.
        A song that finally cannot be prepared leaves the queue with the same notice it would have
        got at play time, so one broken entry never holds up the ones behind it - or a skip."""
        key = id(request)
        definitive = problem in ("live", "too_long")
        attempts = _PREP_ATTEMPTS if definitive else self._prep_attempts.get(key, (0, 0.0))[0] + 1
        if attempts < _PREP_ATTEMPTS:
            delay = _PREP_RETRY_DELAYS[min(attempts - 1, len(_PREP_RETRY_DELAYS) - 1)]
            self._prep_attempts[key] = (attempts, time.monotonic() + delay)
            return
        notices = {
            "live": f"Skipped {request.requester_name}'s song — it's a livestream now.",
            "too_long": f"Skipped {request.requester_name}'s song — it's too long to play now.",
        }
        message = notices.get(problem or "", f"Couldn't load {request.requester_name}'s song — skipping it.")
        log.warning(
            "Dropping %s from the queue: it could not be prepared (%s).", request.webpage_url, problem
        )
        removed = self.purge_pending(lambda r: r is request)
        if removed:
            if request.requester_id != 0:
                await self._notify_failed(message)
            else:
                counters.record("tracks_failed")  # radio-mix filler: nobody asked for it, nobody is told

    async def _prepare_radio_pick(self) -> bool:
        """With nothing queued, asks the radio mix for the next song - shortly before the current
        one ends, or at once when a skip is waiting for something to follow."""
        if self._radio_suggest is None:
            return False
        near_end = (
            self._current_decoder is not None
            and self._now_playing is not None
            and self._now_playing.duration > 0
            and self._remaining_seconds(self._now_playing.duration) <= _PREFETCH_LEAD_SECONDS
        )
        if not (near_end or self._radio_pick_wanted) or self._radio_fill_task is not None:
            return False
        if time.monotonic() - self._radio_fill_failed_at < _RADIO_RETRY_BACKOFF_SECONDS:
            return False
        picked = await self._get_radio_pick()
        if picked is None:
            return False
        if self._pending:
            return True  # a request (or the idle backstop) filled the queue while we asked
        self.enqueue(picked)
        log.info("Radio autoplay queued: %s", picked.title)
        return True

    async def _resolve_ahead(self, request: QueuedRequest) -> tuple[Track | None, str | None]:
        """The resolve+validate half of what _play_one_inner runs inline, run early. Network
        only, no decoder. Returns the track, or None with what was wrong: "live", "too_long",
        "unavailable" (nothing found) or "error" (the lookup itself failed, worth retrying). Touches
        none of the active-track state (_resolving, _current_decoder, _skip_pending): this request
        isn't active yet, so a failure here is no user-facing event until _prep_failed says so."""
        try:
            track = await self._resolver(request.webpage_url, request.requester_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("Prepare-ahead resolve failed for %s (non-fatal).", request.webpage_url, exc_info=True)
            return None, "error"
        if track is None:
            return None, "unavailable"
        if track.is_live:
            return None, "live"
        duration_limit = await self._current_duration_limit()
        if 0 < duration_limit < track.duration:
            return None, "too_long"
        return track, None

    async def _spawn_ahead(self, request: QueuedRequest, track: Track) -> _PreparedNext | None:
        """The decoder-spawn+first-chunk half — see _resolve_ahead."""
        decoder: asyncio.subprocess.Process | None = None
        try:
            try:
                decoder = await _spawn(
                    *_decoder_cmd(track.stream_url),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                )
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
                await _reap(decoder)

    def _maybe_start_radio_fill(self) -> None:
        """Kicks off a background radio-suggestion lookup when the queue is
        empty and nothing's already in flight — _feed_loop's idle-branch
        backstop for whenever _prepare_radio_pick's own attempt (above) didn't
        run or didn't catch it in time (e.g. a track too short for
        _PREFETCH_LEAD_SECONDS to matter, or radio toggled on mid-track).
        Guarded so the two can't double-queue a pick.

        _feed_loop can never reach this while paused, but the preparation loop
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
        by _run_radio_fill's fire-and-forget backstop and _prepare_radio_pick's
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

    async def _play_one_inner(self, request: QueuedRequest) -> None:
        key = id(request)
        prepared = self._warm.pop(key, None)
        resolved = self._resolved.pop(key, None)
        self._prep_attempts.pop(key, None)
        if prepared is not None and (
            prepared.request is not request or request.cancelled or not self._decoder_usable(prepared.decoder)
        ):
            # Cancelled while sitting ready, or the decoder died while it waited. Not usable: free
            # it and fall through to resolving `request` fresh below, as if nothing had been prepared.
            self._fire_and_forget(_reap(prepared.decoder), name="radio-player-reap")
            prepared = None
        reusable: Track | None = None
        if (
            resolved is not None
            and resolved.request is request
            and time.monotonic() - resolved.at <= _RESOLVED_MAX_AGE_SECONDS
        ):
            reusable = resolved.track  # looked up ahead of time; only the decoder is missing

        if prepared is not None:
            await self._start_and_stream(prepared.track, prepared.decoder, prepared.first_chunk, request)
            return

        self._skip_pending = False
        silence_task = asyncio.create_task(
            self._trickle_silence_until_cancelled(), name="radio-player-prefeed-silence"
        )
        decoder: asyncio.subprocess.Process | None = None
        track: Track | None = None
        first_chunk = b""
        self._resolving = True
        try:
            try:
                track = reusable or await self._resolver(request.webpage_url, request.requester_id)
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

            decoder = await _spawn(
                *_decoder_cmd(track.stream_url),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
            )
            self._current_decoder = decoder
            if self._skip_pending:
                self._skip_pending = False
                log.info("Skipped %s right after its decoder started (mid-spawn skip).", track.title)
                await _reap(decoder)
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
                await _reap(decoder)
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
        await self._start_and_stream(track, decoder, first_chunk, request)

    async def _start_and_stream(
        self,
        track: Track,
        decoder: asyncio.subprocess.Process,
        first_chunk: bytes,
        request: QueuedRequest,
    ) -> None:
        """Common tail for both playback paths — a freshly resolved-and-
        spawned track (the slow path above) and one handed off from
        self._warm (the gapless path) arrive here identically: a
        track, a live decoder, and its first chunk already in hand."""
        assert decoder.stdout is not None
        stdout = decoder.stdout
        self._current_decoder = decoder
        self._played_bytes = 0
        self._set_now_playing(
            NowPlaying(
                title=track.title,
                uploader=track.uploader,
                thumbnail_url=track.thumbnail_url,
                requester_name=request.requester_name,
                requester_id=request.requester_id,
                webpage_url=track.webpage_url,
                started_at=time.monotonic(),
                duration=track.duration,
            )
        )
        # Seed for the next radio-autoplay pick — set only once playback
        # is actually going ahead, so a track that fails to resolve/decode
        # never becomes a seed.
        self._last_played_webpage_url = track.webpage_url
        if self._radio_played_notifier is not None:
            self._radio_played_notifier(track.webpage_url)
        log.info("Now playing: %s (requested by %s)", track.title, request.requester_name)
        self._fire_and_forget(
            self._announce_now_playing(request, track), name="radio-player-announce-now-playing"
        )
        self._prep_event.set()  # a place in line opened up: prepare whatever moved to the front

        try:
            chunk = first_chunk
            while chunk and decoder is not self._cut:
                await self._write_pcm(chunk)
                self._played_bytes += len(chunk)
                await self._pace(len(chunk) / _BYTES_PER_SECOND)
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
            # Recorded first: this is the moment the song's last audio went out, which is what
            # the audible timeline (and so the overlay) counts from.
            self._set_now_playing(None)
            await _reap(decoder)
            self._current_decoder = None
            if self._cut is decoder:
                self._cut = None

    async def _announce_now_playing(self, request: QueuedRequest, track: Track) -> None:
        counters.record("tracks_played")
        if request.requester_id == 0:
            message = f"Now Playing: {track.title}"
        else:
            message = f"{request.requester_name}'s song request is Now Playing: {track.title}"
        await self._notify(message)
