"""The bot's lifecycle as one object: start it, watch it, stop it cleanly.

This used to be one long function in bot.py that could only be ended with
Ctrl+C. The desktop app needs three more things from it, all provided here:

* a stop() that does an orderly shutdown from any caller (a button, a signal,
  the controlling app disappearing),
* live state for the dashboard (what is playing, who is connected, how the
  machine is coping), and
* failures reported as one clear message and an exit code instead of a
  traceback in a console nobody is looking at.

Teardown order matters and is deliberate - see _teardown().
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from twitch_radio.admin.app import run_admin_server
from twitch_radio.blocklist import BlockList
from twitch_radio.chatbot import TwitchChatBot
from twitch_radio.config import Settings, token_status
from twitch_radio.extraction import Resolver
from twitch_radio.player import RadioPlayer
from twitch_radio.radio import RadioSuggester
from twitch_radio.store import JsonStore
from twitch_radio.telemetry import counters
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONFIG = 2  # the user has to change something; retrying is pointless
EXIT_RUNTIME = 3  # may well work on a retry (network down, Twitch hiccup)

# TwitchIO's built-in OAuth web server (twitchio.web.AiohttpAdapter) - the
# bot cannot be authorized without it.
OAUTH_PORT = 4343

_STATE_INTERVAL_SECONDS = 1.0
_PROC_SAMPLE_SECONDS = 2.0
_STEP_TIMEOUT_SECONDS = 8.0
_MAX_QUEUE_IN_SNAPSHOT = 100

EmitFn = Callable[[dict[str, Any]], None]


class StartupError(Exception):
    def __init__(self, message: str, *, code: int = EXIT_RUNTIME, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def port_in_use(host: str, port: int) -> bool:
    """True if something is already listening where we'd bind. Mirrors asyncio's
    own default (SO_REUSEADDR on POSIX, off on Windows) so the answer matches
    what the real bind would do."""
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "localhost") else host
    family = socket.AF_INET6 if ":" in probe_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_REUSEADDR") and not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((probe_host, port))
        except OSError as exc:
            return exc.errno in (errno.EADDRINUSE, 10048, 10013)
    return False


def describe_failure(exc: BaseException) -> tuple[str, str, int]:
    """(message, hint, exit code) for an exception that ended a run."""
    if isinstance(exc, StartupError):
        return str(exc), exc.hint, exc.code
    status = getattr(exc, "status", None)
    if status in (400, 401, 403) and "twitch" in type(exc).__module__.lower():
        return (
            "Twitch rejected the Client ID / Client Secret.",
            "Open Settings and re-check both values against dev.twitch.tv/console/apps.",
            EXIT_CONFIG,
        )
    if isinstance(exc, (aiohttp.ClientConnectionError, TimeoutError, socket.gaierror, ConnectionError)):
        return (
            "Couldn't reach Twitch.",
            "Check your internet connection. The app can retry automatically (Settings > App).",
            EXIT_RUNTIME,
        )
    return f"{type(exc).__name__}: {exc}", "See the Logs tab for the full traceback.", EXIT_RUNTIME


class _ProcSampler:
    """CPU / memory of the core and everything it spawned (ffmpeg, yt-dlp
    workers). psutil is optional: without it the dashboard just shows no
    numbers instead of the app failing to start."""

    def __init__(self) -> None:
        try:
            import psutil
        except ImportError:
            self._ps: Any = None
            self._me: Any = None
            self._cpus = 1
            self._cache: dict[int, Any] = {}
            return
        self._ps = psutil
        self._me = psutil.Process()
        self._cpus = psutil.cpu_count() or 1
        self._cache = {}
        with contextlib.suppress(Exception):
            self._me.cpu_percent(None)

    def sample(self) -> dict[str, Any]:
        ps = self._ps
        if ps is None:
            return {"available": False}
        try:
            procs = [self._me, *self._me.children(recursive=True)]
        except Exception:  # noqa: BLE001 - a vanished process mid-walk is routine
            return {"available": False}
        alive: dict[int, Any] = {}
        cpu = 0.0
        rss = 0
        core_rss = 0
        threads = 0
        for proc in procs:
            pid = proc.pid
            proc = self._cache.get(pid, proc)  # keep the object so cpu_percent has a baseline
            try:
                cpu += proc.cpu_percent(None)
                mem = proc.memory_info().rss
                rss += mem
                threads += proc.num_threads()
                if pid == self._me.pid:
                    core_rss = mem
            except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess):
                continue
            alive[pid] = proc
        self._cache = alive
        return {
            "available": True,
            "cpu": round(cpu / self._cpus, 1),
            "mem_mb": round(rss / 1048576, 1),
            "core_mem_mb": round(core_rss / 1048576, 1),
            "processes": len(alive),
            "threads": threads,
        }


class BotRuntime:
    def __init__(self, settings: Settings, emit: EmitFn | None = None) -> None:
        self.settings = settings
        self._emit = emit
        self._stop = asyncio.Event()
        self._stop_reason = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._phase = "starting"
        self._started_at = time.monotonic()

        self._resolver: Resolver | None = None
        self._player: RadioPlayer | None = None
        self._tunables_store: JsonStore | None = None
        self._toggles_store: JsonStore | None = None
        self._admin: aiohttp.web.AppRunner | None = None
        self._bot: TwitchChatBot | None = None
        self._warmup: asyncio.Task[None] | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._sampler_task: asyncio.Task[None] | None = None
        self._sampler = _ProcSampler()
        self._proc_stats: dict[str, Any] = {"available": False}
        self._tokens: dict[str, object] = {}
        self._tokens_at = 0.0
        self._radio_autoplay: bool | None = None
        self._bg: set[asyncio.Task[Any]] = set()

    # -- public ----------------------------------------------------------

    @property
    def phase(self) -> str:
        return self._phase

    def request_stop(self, reason: str = "requested") -> None:
        """Idempotent and safe to call from the event loop thread at any
        point of startup or run; a second request changes nothing."""
        if self._stop.is_set():
            log.info("Stop already in progress (%s).", reason)
            return
        self._stop_reason = reason
        log.info("Stopping: %s", reason)
        self._stop.set()

    def skip(self) -> bool:
        return bool(self._player and self._player.skip_current())

    def pause(self) -> bool:
        return bool(self._player and self._player.pause())

    def resume(self) -> bool:
        return bool(self._player and self._player.resume())

    def clear_queue(self) -> int:
        if self._player is None:
            return 0
        removed = self._player.purge_pending(lambda _request: True)
        if removed:
            log.info("Cleared %d queued request(s) from the dashboard.", len(removed))
        return len(removed)

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        self._set_phase("starting")
        code = EXIT_OK
        try:
            await self._run_inner()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - reported, then torn down below
            message, hint, code = describe_failure(exc)
            if isinstance(exc, StartupError):
                log.error("%s%s", message, f" {hint}" if hint else "")
            else:
                log.error("%s", message, exc_info=exc)
            self._send({"t": "fatal", "message": message, "hint": hint, "code": code})
        finally:
            await self._teardown()
        self._set_phase("stopped")
        return code

    # -- startup ---------------------------------------------------------

    def _check_ports(self) -> None:
        s = self.settings
        if port_in_use(s.nowplaying_host, s.nowplaying_port):
            raise StartupError(
                f"Port {s.nowplaying_port} is already in use.",
                code=EXIT_CONFIG,
                hint="Another copy of the bot (or another program) is using it. Close it, "
                "or change the port in Settings > Network.",
            )
        if port_in_use("127.0.0.1", OAUTH_PORT):
            raise StartupError(
                f"Port {OAUTH_PORT} (Twitch sign-in) is already in use.",
                code=EXIT_CONFIG,
                hint="Another copy of the bot is probably still running. Close it and start again.",
            )

    async def _run_inner(self) -> None:
        s = self.settings
        self._check_ports()

        self._resolver = Resolver(s)
        self._tunables_store = JsonStore(s.tunables_path)
        self._toggles_store = JsonStore(s.toggles_path)
        blocklist = BlockList(JsonStore(s.blocklist_path))

        # Warms yt-dlp's workers before the first real !sr; never awaited
        # inline (must not delay startup) and non-fatal by design.
        self._warmup = asyncio.create_task(self._resolver.warm_up(), name="resolver-warmup")

        player = self._player = RadioPlayer(
            resolver=self._resolver.resolve,
            audio_bitrate_kbps=s.audio_bitrate_kbps,
            pause_when_no_listeners=s.pause_when_no_listeners,
            prefetch_enabled=s.ytdlp_cache_ttl_seconds > 0,
        )
        suggester = RadioSuggester(self._resolver, blocklist)
        player.set_radio_suggester(suggester.suggest)
        player.set_radio_played_notifier(suggester.note_played)

        toggles_store = self._toggles_store
        tunables_store = self._tunables_store

        async def _radio_enabled() -> bool:
            return FeatureToggles.from_dict(await toggles_store.read()).radio_autoplay_enabled

        async def _duration_limit() -> int:
            return TwitchTunables.from_dict(await tunables_store.read()).max_request_duration_seconds

        player.set_radio_enabled_getter(_radio_enabled)
        player.set_duration_limit_getter(_duration_limit)
        player.set_queue_store(JsonStore(s.queue_state_path))

        if self._emit is not None:
            self._monitor = asyncio.create_task(self._monitor_loop(), name="gui-monitor")
            self._sampler_task = asyncio.create_task(self._sampler_loop(), name="gui-sampler")

        restored = await player.restore_queue()
        if restored:
            log.info("Restored %d queued request(s) from the last run.", restored)
        if self._stop.is_set():
            return

        # Inside the try that tears everything down: a missing ffmpeg used to
        # raise from here with the worker pool already running and nothing
        # responsible for closing it.
        try:
            player.start()
        except RuntimeError as exc:
            raise StartupError(
                str(exc),
                code=EXIT_CONFIG,
                hint="The packaged app bundles ffmpeg; if you run from source, install it and add it to PATH.",
            ) from exc

        try:
            self._admin = await run_admin_server(
                player=player,
                tunables_store=tunables_store,
                toggles_store=toggles_store,
                settings_password=s.settings_password,
                broadcast_info={
                    "Audio stream": "/stream.opus",
                    "Overlay": "/overlay",
                    "Audio bitrate": f"{s.audio_bitrate_kbps} kbps",
                    "Chat command prefix": s.prefix,
                },
                host=s.nowplaying_host,
                port=s.nowplaying_port,
                trusted_proxies=s.trusted_proxies,
                session_hours=s.session_hours,
                session_remember_days=s.session_remember_days,
                allow_open_settings=s.settings_allow_open,
            )
        except OSError as exc:
            raise StartupError(
                f"Couldn't start the web server on {s.nowplaying_host}:{s.nowplaying_port} ({exc.strerror or exc}).",
                code=EXIT_CONFIG,
                hint="Change the address or port in Settings > Network.",
            ) from exc
        if self._stop.is_set():
            return

        bot = self._bot = TwitchChatBot(
            client_id=s.client_id,
            client_secret=s.client_secret,
            bot_id=s.bot_id,
            owner_id=s.owner_id,
            prefix=s.prefix,
            resolver=self._resolver,
            player=player,
            tunables_store=tunables_store,
            toggles_store=toggles_store,
            blocklist=blocklist,
            token_storage_path=s.token_path,
        )
        bot.on_ready_callback = self._on_bot_ready
        player.set_track_failure_notifier(bot.announce)

        self._set_phase("running")
        log.info("Radio is up. Connecting to Twitch...")

        start_task = asyncio.create_task(bot.start(), name="twitch-bot")
        stop_task = asyncio.create_task(self._stop.wait(), name="stop-wait")
        try:
            await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if not start_task.done():
                # Stop requested. Cancelling start() makes TwitchIO run its own
                # close() (it wraps the wait in try/finally); doing it this way
                # rather than calling close() from outside also covers a stop
                # that arrives mid-login, before there is anything to close.
                start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await start_task
        finally:
            stop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_task

    def _on_bot_ready(self) -> None:
        # aiohttp's runner (started inside twitchio) swaps in its own
        # SIGINT/SIGTERM handlers on POSIX, which would turn a polite
        # SIGTERM into an abrupt GracefulExit. Take them back.
        self._install_signal_handlers()
        self._send_state_now()

    # -- teardown --------------------------------------------------------

    async def _step(self, name: str, fn: Callable[[], Awaitable[Any]]) -> None:
        try:
            await asyncio.wait_for(fn(), timeout=_STEP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning("Shutdown step %r took longer than %.0fs - moving on.", name, _STEP_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            log.exception("Shutdown step %r failed.", name)

    async def _teardown(self) -> None:
        self._set_phase("stopping")
        log.info("Shutting down...")
        for task in (self._monitor, self._sampler_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._warmup is not None and not self._warmup.done():
            self._warmup.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._warmup

        # 1. Twitch first: stops new chat commands arriving mid-teardown and
        #    saves the OAuth tokens. Idempotent after start() already closed it.
        if self._bot is not None:
            await self._step("Twitch connection", self._bot.close)
        # 2. End the audio streams, then the web server. Without the first,
        #    a connected OBS source holds the server open for its whole grace
        #    period (see admin/app.py).
        if self._player is not None:
            self._player.close_subscribers()
        if self._admin is not None:
            await self._step("web server", self._admin.cleanup)
        # 3. Player (also re-saves the song that was playing), then the
        #    extraction workers it may still be waiting on.
        if self._player is not None:
            await self._step("audio player", self._player.stop)
        if self._resolver is not None:
            await self._step("yt-dlp workers", self._resolver.aclose)
        for task in list(self._bg):
            task.cancel()
        log.info("Shutdown complete.")

    # -- signals ---------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        loop = self._loop
        if loop is None:
            return

        def _on_signal(signum: int) -> None:
            self.request_stop(f"{signal.Signals(signum).name} received")

        names = ["SIGTERM", "SIGINT", "SIGBREAK"]  # SIGBREAK: Ctrl+Break, Windows only
        for name in names:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, _on_signal, sig)
            except NotImplementedError:
                # Windows' loop has no add_signal_handler; plain signal.signal
                # still delivers, so hop back onto the loop from the handler.
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, lambda signum, _frame: loop.call_soon_threadsafe(_on_signal, signum))
            except (ValueError, RuntimeError):
                pass

    # -- state for the dashboard ----------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        if self._emit is not None:
            self._emit(message)

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        self._send({"t": "phase", "phase": phase})
        if phase in ("running", "stopping"):
            self._send_state_now()

    def _send_state_now(self) -> None:
        if self._emit is not None:
            self._emit(self.snapshot())

    async def _sampler_loop(self) -> None:
        while True:
            self._proc_stats = await asyncio.to_thread(self._sampler.sample)
            await asyncio.sleep(_PROC_SAMPLE_SECONDS)

    async def _monitor_loop(self) -> None:
        loop = asyncio.get_running_loop()
        lag_ms = 0.0
        while True:
            started = loop.time()
            await asyncio.sleep(_STATE_INTERVAL_SECONDS)
            # How late the loop woke us is how long it was blocked: the number
            # that predicts audio stutter, since the same loop paces the stream.
            lag_ms = max(0.0, (loop.time() - started - _STATE_INTERVAL_SECONDS) * 1000)
            await self._refresh_slow_state()
            try:
                self._emit_snapshot(lag_ms)
            except Exception:  # noqa: BLE001 - a bad snapshot must not end monitoring
                log.debug("Snapshot failed.", exc_info=True)

    async def _refresh_slow_state(self) -> None:
        now = time.monotonic()
        if now - self._tokens_at >= 5.0:
            self._tokens_at = now
            s = self.settings
            self._tokens = await asyncio.to_thread(token_status, s.bot_id, s.owner_id, s.token_path)
        if self._toggles_store is not None:
            with contextlib.suppress(Exception):
                self._radio_autoplay = FeatureToggles.from_dict(await self._toggles_store.read()).radio_autoplay_enabled

    def _emit_snapshot(self, lag_ms: float) -> None:
        if self._emit is not None:
            self._emit(self.snapshot(lag_ms))

    def snapshot(self, lag_ms: float = 0.0) -> dict[str, Any]:
        s = self.settings
        player = self._player
        bot = self._bot
        now: dict[str, Any] | None = None
        queue: list[dict[str, Any]] = []
        queue_size = 0
        player_info: dict[str, Any] = {"state": "idle", "paused": False, "listeners": 0}
        if player is not None:
            np = player.now_playing
            if np is not None:
                now = {
                    "title": np.title,
                    "uploader": np.uploader,
                    "thumbnail": np.thumbnail_url,
                    "requester": np.requester_name,
                    "radio": np.requester_id == 0,
                    "url": np.webpage_url,
                    "elapsed": max(0.0, time.monotonic() - np.started_at),
                    "duration": np.duration,
                }
            items = player.queued_items()
            queue_size = len(items)
            queue = [
                {
                    "title": item.title or "Unknown title",
                    "uploader": item.uploader or "",
                    "requester": item.requester_name,
                    "radio": item.requester_id == 0,
                }
                for item in items[:_MAX_QUEUE_IN_SNAPSHOT]
            ]
            player_info = {
                "state": player.state.value,
                "paused": player.is_paused,
                "listeners": player.listener_count,
                "encoder_running": player.encoder_running,
                "encoder_starts": player.encoder_starts,
            }
        host = "localhost" if s.nowplaying_host in ("0.0.0.0", "", "::") else s.nowplaying_host
        base = f"http://{host}:{s.nowplaying_port}"
        return {
            "t": "state",
            "phase": self._phase,
            "uptime": round(time.monotonic() - self._started_at, 1),
            "player": player_info,
            "now": now,
            "queue": queue,
            "queue_size": queue_size,
            "radio_autoplay": self._radio_autoplay,
            "chat": {
                "ready": bool(bot and bot.ready),
                "subscribed": bool(bot and bot.chat_subscribed),
                "tokens": self._tokens,
            },
            "http": {
                "up": self._admin is not None,
                "base": base,
                "stream": f"{base}/stream.opus",
                "overlay": f"{base}/overlay",
                "settings": f"{base}/settings",
                "exposed": s.nowplaying_host not in ("127.0.0.1", "localhost", "::1"),
            },
            "workers": self._resolver.worker_status() if self._resolver is not None else None,
            "counters": {
                name: counters.count_last_hour(name)
                for name in (
                    "resolve_success",
                    "resolve_failure",
                    "tracks_played",
                    "tracks_failed",
                    "skips",
                    "requests_queued",
                )
            },
            "proc": {**self._proc_stats, "loop_lag_ms": round(lag_ms, 1)},
        }
