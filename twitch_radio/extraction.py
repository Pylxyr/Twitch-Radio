from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import re
import sys
import time
from typing import Any
from urllib.parse import urlsplit

from twitch_radio import ytdlp_loader
from twitch_radio.config import BASE_DIR, DATA_DIR, Settings
from twitch_radio.models import Track, youtube_thumbnail
from twitch_radio.paths import hidden_subprocess_kwargs, is_frozen, source_root
from twitch_radio.telemetry import counters

# yt_dlp is deliberately NOT imported in this process. Every extraction runs in
# a child process (extractor_worker.py), so the bot itself never needs the
# ~40 MB the import costs; only the workers load it.

log = logging.getLogger(__name__)

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Only YouTube is allowed — every other extractor is more attack surface
# (arbitrary sites feeding crafted metadata into chat/overlay). Enforced
# twice: the host check below, plus `allowed_extractors` in
# _build_options() in case a redirect resolves through something else.
_ALLOWED_URL_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
# re.fullmatch against yt-dlp's lowercased IE_NAME; covers every youtube:*
# variant while excluding "generic", the scrape-any-webpage fallback this
# restriction exists to keep out.
_ALLOWED_EXTRACTORS = ["youtube(:.*)?"]

# yt-dlp's no-cookies default tries two clients ('visionos', 'web') with no
# short-circuit, so every resolve pays for both. 'visionos' is yt-dlp's own
# JS-less client — skips the ~6s Deno signature solve — so it's used alone
# as a fast first attempt, falling back to the full default list if it
# comes back empty. Only applies when nothing else already pinned the
# client list (no cookies, no YTDLP_PLAYER_CLIENT — see _fast_client_enabled;
# config.py forces cookie deployments onto a fixed list before this runs).
_FAST_PLAYER_CLIENT: tuple[str, ...] = ("visionos",)

# The signature *cipher* is cacheable, but the "n" throttling param is
# solved fresh on every resolve by design — so a fast JS runtime matters
# every time, cache or not. quickjs beats yt-dlp's default (deno) here:
# deno spawns a fresh process with full V8 + JIT startup on every call,
# and nothing here runs long enough for that JIT to pay for itself.
#
# Risk: if quickjs can't solve a challenge, yt-dlp warns and silently
# continues with whatever formats don't need it, rather than raising.
# _has_playable_url() below only catches a *missing* stream_url, not a
# worse-but-present one.
#
# Only useful together with _FAST_PLAYER_CLIENT (see _fast_path_enabled):
# swapping just the runtime doesn't change which requests get made, so if
# player_client is already pinned (e.g. via YTDLP_COOKIES_FILE), fast and
# fallback issue identical requests — a failed fast attempt then pays the
# full network cost twice. Seen in production: quickjs's own solve time
# rode right at _FAST_EXTRACT_TIMEOUT_SECONDS, turning an ~15-18s fallback
# into ~30s on every resolve.
_FAST_JS_RUNTIMES: dict[str, dict[str, str]] = {"quickjs": {}}

_FAST_EXTRACT_TIMEOUT_SECONDS = 15.0
_RADIO_MIX_TIMEOUT_SECONDS = 15.0
_SEARCH_TIMEOUT_SECONDS = 25.0

# Generous: yt_dlp's import alone can take seconds on a loaded VPS, and this
# only costs anything when something's genuinely wrong.
_WORKER_START_TIMEOUT_SECONDS = 60.0
# Short: a wedged worker is stuck inside yt-dlp/a JS runtime and isn't coming
# back, so there's nothing to wait for.
_WORKER_TERM_GRACE_SECONDS = 3.0


def _is_allowed_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host.lower() in _ALLOWED_URL_HOSTS


class DownloadError(Exception):
    """Raised when yt-dlp fails to resolve a query into a playable track."""


class UnsupportedSourceError(DownloadError):
    """Raised when a direct URL isn't from an allowed source (YouTube) —
    distinct from DownloadError so the chat bot can give a specific reply
    instead of a generic "couldn't fetch that"."""


class ExtractionBackend:
    """Where an extraction physically runs. Resolver only needs this surface,
    which keeps the fast/fallback client dance, the caching, the request
    coalescing and the Track building independent of the process model.

    `extract` must raise DownloadError for a timeout or a yt-dlp failure and
    return a (possibly empty) info dict otherwise.
    """

    async def extract(self, query: str, options: dict[str, Any], timeout: float) -> dict[str, Any]:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        """Cheap, read-only snapshot for the desktop dashboard."""
        return {"mode": "process", "size": 0, "alive": 0, "idle": 0}


class _WorkerCrashed(Exception):
    """The worker died, desynchronised, or answered the wrong request id —
    anything where the right response is to recycle it rather than trust
    whatever came back."""


class _ExtractionWorker:
    """One `python -m twitch_radio.extractor_worker` child process.

    Handles one request at a time — ProcessBackend hands a worker to one caller
    at a time, so request() can read the reply inline instead of
    demultiplexing by id. The id is still checked: a mismatch means the
    framing desynced and the process can't be trusted anymore.
    """

    def __init__(self, index: int) -> None:
        self._index = index
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 0
        # Set by ProcessBackend when the worker goes back on the idle shelf.
        self.idle_since = 0.0

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def start(self) -> None:
        env = dict(os.environ)
        # UTF-8 JSON lines, unbuffered — inheriting block buffering on a
        # pipe would deadlock the very first request.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if is_frozen():
            # sys.executable is the packaged core itself, which has no `-m`;
            # its entry point dispatches this flag to extractor_worker.main().
            command = [sys.executable, "--extractor-worker"]
        else:
            # Makes `-m twitch_radio.extractor_worker` resolve regardless of
            # cwd. The project checkout, NOT BASE_DIR: BASE_DIR is the
            # writable data root, which TWITCH_RADIO_HOME can point anywhere.
            existing_path = env.get("PYTHONPATH")
            root = str(source_root())
            env["PYTHONPATH"] = f"{root}{os.pathsep}{existing_path}" if existing_path else root
            command = [sys.executable, "-m", "twitch_radio.extractor_worker"]
        self._proc = await asyncio.create_subprocess_exec(
            *command,
            **hidden_subprocess_kwargs(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
            env=env,
            # Default StreamReader limit is 64 KiB/line; a radio mix of long
            # titles + signed CDN URLs can exceed that and would otherwise
            # surface as a confusing LimitOverrunError.
            limit=1024 * 1024,
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"ytdlp-worker-{self._index}-stderr"
        )
        ready = await asyncio.wait_for(self._read_json(), timeout=_WORKER_START_TIMEOUT_SECONDS)
        if not ready.get("ready"):
            raise _WorkerCrashed(f"worker {self._index} sent {ready!r} instead of a ready banner")
        log.debug("Extraction worker %d ready (pid=%s).", self._index, self.pid)

    async def _drain_stderr(self) -> None:
        """yt-dlp's warnings, and anything the worker's redirected sys.stdout
        emits, land here. This has to keep reading whether or not anyone
        cares about the content: an undrained stderr pipe fills and blocks
        the worker mid-extraction."""
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    level = logging.WARNING if "challenge solver" in text.lower() else logging.DEBUG
                    log.log(level, "[ytdlp-worker-%d] %s", self._index, text)
        except (asyncio.CancelledError, ValueError):
            raise
        except Exception:
            return

    async def _read_json(self) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        line = await self._proc.stdout.readline()
        if not line:
            code = self._proc.returncode
            raise _WorkerCrashed(f"worker {self._index} closed stdout (exit code {code})")
        try:
            payload = json.loads(line.decode("utf-8", "replace"))
        except ValueError as exc:
            raise _WorkerCrashed(f"worker {self._index} sent unparseable framing") from exc
        if not isinstance(payload, dict):
            raise _WorkerCrashed(f"worker {self._index} sent a non-object response")
        return payload

    async def request(self, query: str, options: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdin is not None
        self._next_id += 1
        request_id = self._next_id
        body = json.dumps({"id": request_id, "query": query, "options": options}, ensure_ascii=True)
        try:
            self._proc.stdin.write(body.encode("utf-8") + b"\n")
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _WorkerCrashed(f"worker {self._index} stdin closed") from exc
        response = await asyncio.wait_for(self._read_json(), timeout=timeout)
        if response.get("id") != request_id:
            raise _WorkerCrashed(
                f"worker {self._index} answered id {response.get('id')!r}, expected {request_id}"
            )
        if response.get("ok"):
            info = response.get("info")
            return info if isinstance(info, dict) else {}
        error = str(response.get("error") or "unknown extraction error")
        # Both kinds become DownloadError, matching what the in-process path
        # raised; the distinction is kept in the message for the log.
        raise DownloadError(error)

    async def kill(self) -> None:
        """Terminate, then kill — the capability ThreadBackend structurally
        can't offer: a wedged extraction actually stops consuming CPU/memory
        instead of occupying a worker slot forever."""
        proc = self._proc
        self._proc = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
            self._stderr_task = None
        if proc is None:
            return
        # Closing stdin explicitly avoids a "RuntimeError: Event loop is
        # closed" from the transport's __del__ during interpreter teardown,
        # and gives the worker's stdin loop a clean EOF — often avoiding
        # the SIGTERM below entirely.
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_WORKER_TERM_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()


class ProcessBackend(ExtractionBackend):
    """Extraction worker processes, started on demand and retired when idle.

    Why child processes at all:

    1. Extraction is GIL-bound Python (regex, multi-MB JSON parsing, format
       sorting) - sharing an interpreter with RadioPlayer's real-time feed
       loop is why resolves and stream stutter correlate.
    2. Timeouts become real: a wedged extraction is killed, not just
       abandoned.
    3. A runaway extraction is charged to its own process.

    Why on demand: a worker holds roughly 50-80 MB while it exists and is
    only busy for a second or two per request, so a resident pool was the
    largest share of the bot's idle memory. Now the first request after a
    quiet spell starts a worker (about a second, normally hidden by the
    player's look-ahead), the most recently used worker is reused first, and
    any worker idle for `idle_seconds` exits. Up to `size` run at once.

    The in-memory signature cache dies with a worker, which is fine: the
    expensive half (the cipher) is persisted to disk under `cachedir` and
    shared across processes.
    """

    def __init__(self, size: int, idle_seconds: float) -> None:
        self._size = size
        self._idle_seconds = idle_seconds
        self._slots = asyncio.Semaphore(size)
        self._idle: list[_ExtractionWorker] = []  # most recently used last
        self._all: list[_ExtractionWorker] = []
        self._closing = False
        self._next_index = 0
        self._reap_handle: asyncio.TimerHandle | None = None
        self._background: set[asyncio.Task[None]] = set()

    def status(self) -> dict[str, Any]:
        return {
            "mode": "process",
            "size": self._size,
            "alive": len(self._all),
            "idle": len(self._idle),
        }

    async def _spawn(self) -> _ExtractionWorker:
        self._next_index += 1
        worker = _ExtractionWorker(self._next_index)
        try:
            await worker.start()
        except Exception as exc:
            await worker.kill()
            raise DownloadError(
                f"Couldn't start the extraction process: {exc or type(exc).__name__}"
            ) from exc
        self._all.append(worker)
        log.debug("Extraction worker %d started (%d alive).", worker._index, len(self._all))
        return worker

    async def _discard(self, worker: _ExtractionWorker) -> None:
        with contextlib.suppress(ValueError):
            self._all.remove(worker)
        await worker.kill()

    def _release(self, worker: _ExtractionWorker) -> None:
        if self._closing:
            self._spawn_background(self._discard(worker))
            return
        worker.idle_since = asyncio.get_running_loop().time()
        self._idle.append(worker)
        self._arm_reaper()

    def _spawn_background(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _arm_reaper(self) -> None:
        """One timer, re-armed only while something is idle - no periodic wakeups."""
        if self._reap_handle is not None or not self._idle:
            return
        loop = asyncio.get_running_loop()
        oldest = min(w.idle_since for w in self._idle)
        delay = max(0.5, oldest + self._idle_seconds - loop.time())
        self._reap_handle = loop.call_later(delay, self._reap)

    def _reap(self) -> None:
        self._reap_handle = None
        now = asyncio.get_running_loop().time()
        keep = [w for w in self._idle if now - w.idle_since < self._idle_seconds]
        expired = [w for w in self._idle if now - w.idle_since >= self._idle_seconds]
        self._idle = keep
        for worker in expired:
            self._spawn_background(self._discard(worker))
        if expired:
            log.debug("Stopped %d idle extraction worker(s).", len(expired))
        self._arm_reaper()

    async def extract(self, query: str, options: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self._closing:
            raise DownloadError("The bot is shutting down.")
        async with self._slots:
            worker = self._idle.pop() if self._idle else await self._spawn()
            try:
                info = await worker.request(query, options, timeout)
            except TimeoutError as exc:
                log.info("Extraction worker %s timed out on %r - stopping it.", worker.pid, query)
                await self._discard(worker)
                raise DownloadError(f"Timed out resolving {query!r}") from exc
            except _WorkerCrashed as exc:
                log.warning("Extraction worker problem (%s) - stopping it.", exc)
                await self._discard(worker)
                raise DownloadError(str(exc)) from exc
            except asyncio.CancelledError:
                # The worker is still mid-extraction and its eventual reply
                # would desynchronise the next caller's read, so it can't be reused.
                await self._discard(worker)
                raise
            except DownloadError:
                # A clean "yt-dlp couldn't do it" - the worker is fine.
                self._release(worker)
                raise
            else:
                self._release(worker)
                return info

    async def aclose(self) -> None:
        self._closing = True
        if self._reap_handle is not None:
            self._reap_handle.cancel()
            self._reap_handle = None
        workers = list(self._all)
        self._idle.clear()
        await asyncio.gather(*(w.kill() for w in workers), return_exceptions=True)
        self._all.clear()
        if self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)


class Resolver:
    """Turns a !sr query (URL or search text) into a playable Track.

    Deliberately simple: no per-guild semaphores, no playlist expansion —
    this service only ever needs one track per request, with no sibling
    feature in-process to protect from contention.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._backend: ExtractionBackend = ProcessBackend(
            settings.ytdlp_concurrency, settings.ytdlp_worker_idle_seconds
        )
        # A !sr resolves once in chat (confirm/queue) and again in the
        # player right before it plays (see resolve()). Keyed by the
        # resolved webpage_url, plus the case-folded search text for
        # non-URL queries.
        self._cache: dict[str, tuple[Track, float]] = {}

        # Coalesces concurrent resolves of the same query (double !sr, or
        # two chatters requesting the same song moments apart) into one
        # extraction — without this each caller misses the still-empty
        # `_cache` and pays the full round trip independently. Keyed the
        # same way `_cache` is; entries remove themselves once the shared
        # task finishes.
        self._inflight: dict[str, asyncio.Task[Track | None]] = {}

        # See the module comment above _FAST_PLAYER_CLIENT.
        self._fast_client_enabled = not settings.ytdlp_player_client
        # See the module comment above _FAST_JS_RUNTIMES.
        self._fast_runtime_enabled = not settings.ytdlp_js_runtime_path
        # Deliberately not "client_enabled or runtime_enabled": the fast
        # attempt only pays off when it skips the JS challenge entirely
        # (_FAST_PLAYER_CLIENT). A runtime-only difference still issues the
        # same requests as the fallback (player_client is unchanged), so it
        # buys nothing on success and doubles the network + JS-challenge
        # cost on a failed/timed-out attempt — which one deployment's logs
        # showed is the normal case for quickjs vs deno on that host. To
        # force quickjs as the sole runtime instead, set
        # YTDLP_JS_RUNTIME_NAME/PATH explicitly (config.py) rather than
        # relying on this fast-path logic.
        self._fast_path_enabled = self._fast_client_enabled
        self._solver_hint_shown = False

        self._ytdl_options: dict[str, Any] | None = None
        self._fast_ytdl_options: dict[str, Any] | None = None
        # Each worker reuses one YoutubeDL instance per options shape, so the
        # solved JS signature challenge is cached on the extractor instance
        # for as long as the worker lives.

    def worker_status(self) -> dict[str, Any]:
        return self._backend.status()

    async def aclose(self) -> None:
        """Async because the worker processes need reaping. Called from the
        service's teardown after the player and HTTP surface are down."""
        await self._backend.aclose()

    def _build_options(
        self,
        *,
        player_client_override: tuple[str, ...] | None = None,
        js_runtimes_override: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        verbose = self._settings.log_level == "DEBUG"
        options: dict[str, Any] = {
            # Audio-only when available — this pipeline decodes to raw PCM
            # and discards any video track anyway.
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": not verbose,
            "no_warnings": not verbose,
            "verbose": verbose,
            "default_search": "ytsearch",
            "socket_timeout": 15,
            "extract_flat": False,
            "allowed_extractors": _ALLOWED_EXTRACTORS,
            # Keep yt-dlp's cache inside the bot's own data folder rather than
            # the user's profile, so everything the bot writes lives in one place.
            #
            # Key is "cachedir", no underscore (yt_dlp.cache.Cache
            # ._get_root_dir()) — an earlier "cache_dir" typo here silently
            # went nowhere, so the ~7s JS-challenge solve reran on every
            # resolve instead of once per player rotation.
            "cachedir": str(DATA_DIR / ytdlp_loader.CACHE_DIRNAME),
            # The JS solver scripts are not bundled: yt-dlp downloads the ones
            # matching its own version from github.com/yt-dlp/ejs into cachedir.
            "remote_components": ["ejs:github"],
        }
        extractor_args: dict[str, dict[str, list[str]]] = {}
        player_client = (
            player_client_override
            if player_client_override is not None
            else self._settings.ytdlp_player_client
        )
        if player_client:
            extractor_args["youtube"] = {"player_client": list(player_client)}
        if self._settings.ytdlp_pot_provider_url:
            extractor_args["youtubepot-bgutilhttp"] = {"base_url": [self._settings.ytdlp_pot_provider_url]}
        if extractor_args:
            options["extractor_args"] = extractor_args
        if self._settings.ytdlp_cookies_file is not None:
            options["cookiefile"] = str(self._settings.ytdlp_cookies_file)
        if js_runtimes_override is not None:
            options["js_runtimes"] = js_runtimes_override
        elif self._settings.ytdlp_js_runtime_path:
            options["js_runtimes"] = {
                self._settings.ytdlp_js_runtime_name: {"path": self._settings.ytdlp_js_runtime_path}
            }
        # else: leave unset — yt-dlp's own default ({"deno": {}}) applies.
        return options

    def _get_ytdl_options(self, *, fast: bool) -> dict[str, Any]:
        if fast:
            if self._fast_ytdl_options is None:
                self._fast_ytdl_options = self._build_options(
                    player_client_override=_FAST_PLAYER_CLIENT if self._fast_client_enabled else None,
                    js_runtimes_override=_FAST_JS_RUNTIMES if self._fast_runtime_enabled else None,
                )
            return self._fast_ytdl_options
        if self._ytdl_options is None:
            self._ytdl_options = self._build_options()
        return self._ytdl_options

    async def _extract_info_via(self, query: str, *, fast: bool, timeout: float) -> dict[str, Any]:
        info = await self._backend.extract(query, self._get_ytdl_options(fast=fast), timeout)
        if not info:
            raise DownloadError(f"yt-dlp returned nothing for {query!r}")
        return info

    def _has_playable_url(self, info: dict[str, Any]) -> bool:
        # Mirrors resolve()'s own entries-vs-item unwrapping below — used
        # only to decide whether the fast attempt is good enough to keep,
        # not to build the actual Track (resolve() still does that itself
        # from whichever info dict this function ends up returning).
        entries = info.get("entries") if isinstance(info, dict) else None
        item = next((e for e in entries if e), None) if entries is not None else info
        return bool(item and item.get("url"))

    async def _extract_info(self, query: str) -> dict[str, Any]:
        if self._fast_path_enabled:
            start = time.monotonic()
            try:
                info = await self._extract_info_via(
                    query,
                    fast=True,
                    timeout=min(self._settings.ytdlp_extract_timeout_seconds, _FAST_EXTRACT_TIMEOUT_SECONDS),
                )
            except DownloadError as exc:
                log.info(
                    "Fast resolve failed for %r after %.1fs (%s) — falling back.",
                    query,
                    time.monotonic() - start,
                    exc,
                )
            else:
                if self._has_playable_url(info):
                    log.debug("Fast resolve for %r took %.1fs.", query, time.monotonic() - start)
                    return info
                log.info(
                    "Fast resolve returned no playable format for %r after %.1fs — falling back.",
                    query,
                    time.monotonic() - start,
                )

        start = time.monotonic()
        try:
            info = await self._extract_info_via(
                query, fast=False, timeout=self._settings.ytdlp_extract_timeout_seconds
            )
        except DownloadError as exc:
            self._hint_if_solver_missing(exc)
            raise
        log.debug("Resolve for %r took %.1fs.", query, time.monotonic() - start)
        return info

    def _hint_if_solver_missing(self, exc: Exception) -> None:
        """A failed resolve is the only time a missing JS solver matters; say so once."""
        if self._solver_hint_shown:
            return
        text = str(exc).lower()
        if not any(word in text for word in ("challenge", "signature", "js runtime", "requested format")):
            return
        if ytdlp_loader.solver_status(DATA_DIR / ytdlp_loader.CACHE_DIRNAME)["ready"]:
            return
        self._solver_hint_shown = True
        log.warning(
            "A YouTube link failed in a way the JS solver can fix, and the solver scripts aren't "
            "downloaded. yt-dlp fetches them from github.com/yt-dlp/ejs: check that this PC is online "
            "and that your firewall or antivirus allows github.com, then try again."
        )

    async def resolve_radio_mix(self, mix_url: str, limit: int = 15) -> list[dict[str, Any]]:
        """Flat-extracts a YouTube 'watch?v=X&list=RDX' Mix — YouTube's own
        "more like this" queue, reused instead of building a recommendation
        engine. extract_flat skips per-entry format resolution, so this
        needs no JS-runtime call at all. Returns [] on any failure; callers
        treat that as "no suggestion" and fall back to silence.
        """
        # noplaylist=True from _build_options() is right for a normal !sr
        # (a URL in some playlist should still resolve to just that video)
        # but fatal here: it makes yt-dlp ignore &list=RD<id> and resolve
        # only the seed video, so "entries" never comes back. This was the
        # actual bug behind "radio autoplay doesn't do anything".
        options = {
            **self._build_options(),
            "extract_flat": "in_playlist",
            "playlist_items": f"1-{max(1, limit)}",
            "noplaylist": False,
        }
        try:
            info = await self._backend.extract(mix_url, options, _RADIO_MIX_TIMEOUT_SECONDS)
        except Exception:
            # Broad on purpose (matches the docstring's "[] on any
            # failure") — narrowing to TimeoutError/DownloadError missed
            # real cases like ExtractorError for "no mix available".
            log.debug("Radio mix extraction failed for %s (non-fatal).", mix_url, exc_info=True)
            return []
        entries = info.get("entries") if isinstance(info, dict) else None
        return [e for e in (entries or []) if e]

    async def search(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        """The first few YouTube results for a text search, for the dashboard's picker: title,
        uploader, length, picture and link, nothing that needs a format resolved (flat, like the radio
        mix), so it is quick. Links are not searched. Raises on failure; [] means nothing found."""
        raw = " ".join(query.split())
        if not raw or _URL_RE.match(raw):
            return []
        limit = max(1, min(int(limit), 10))
        options = {**self._build_options(), "extract_flat": "in_playlist"}
        info = await self._backend.extract(f"ytsearch{limit}:{raw}", options, _SEARCH_TIMEOUT_SECONDS)
        results: list[dict[str, Any]] = []
        for entry in (info.get("entries") if isinstance(info, dict) else None) or []:
            video_id = entry.get("id") if isinstance(entry, dict) else None
            if not video_id or entry.get("is_live"):
                continue
            url = entry.get("url") or ""
            if not _URL_RE.match(url) or not _is_allowed_url(url):
                url = f"https://www.youtube.com/watch?v={video_id}"
            results.append(
                {
                    "title": entry.get("title") or "Unknown title",
                    "url": url,
                    "uploader": entry.get("uploader") or entry.get("channel") or "",
                    "duration": int(entry.get("duration") or 0),
                    "thumbnail": youtube_thumbnail(entry),
                }
            )
        return results

    def _query_for(self, raw: str) -> str:
        raw = raw.strip()
        if _URL_RE.match(raw):
            return raw
        return f"ytsearch1:{raw}"

    def _prune_cache(self, now: float) -> None:
        ttl = self._settings.ytdlp_cache_ttl_seconds
        expired = [key for key, (_, cached_at) in self._cache.items() if now - cached_at >= ttl]
        for key in expired:
            del self._cache[key]

    async def resolve(self, query: str, requester_id: int) -> Track | None:
        """Resolves one query to one Track, or None if nothing playable was
        found. Never raises for "not found" — only for actual failures
        (timeout, network error), which the caller must catch.

        Cached briefly (YTDLP_CACHE_TTL_SECONDS), keyed on the resolved
        webpage_url (so the player's pre-playback re-resolve and prefetch
        reuse this) and, for non-URL queries, the case-folded search text
        (so a repeat !sr by name skips the full resolve too). A cache hit
        still gets a fresh requester_id; treat it as informational, not
        gospel, for anything safety-relevant (e.g. is_live).
        """
        now = time.monotonic()
        raw = query.strip()
        if _URL_RE.match(raw) and not _is_allowed_url(raw):
            raise UnsupportedSourceError("Only YouTube links are supported.")

        # Search text gets its own case-folded key, separate from
        # webpage_url — URLs are case-sensitive (video IDs), so only
        # non-URL search text is folded. Without this, repeat requests for
        # the same song by name each pay the full resolve+JS-challenge cost
        # instead of reusing the just-resolved result.
        is_url = bool(_URL_RE.match(raw))
        search_key = raw.lower() if not is_url else None
        # Same key the result gets cached under below (search_key for a
        # non-URL query, the raw URL otherwise) — so two callers with the
        # exact same query still coalesce.
        dedup_key = search_key if search_key is not None else raw

        ttl = self._settings.ytdlp_cache_ttl_seconds
        if ttl > 0:
            self._prune_cache(now)
            cached = self._cache.get(raw) or (self._cache.get(search_key) if search_key else None)
            if cached is not None:
                track, cached_at = cached
                if now - cached_at < ttl:
                    return dataclasses.replace(track, requester_id=requester_id)

        # asyncio.shield on both paths below, not a bare await — awaiting a
        # Task directly propagates the *awaiter's* cancellation into the
        # Task itself. Concretely: RadioPlayer cancels its prefetch task
        # after every track, and that prefetch often shares a dedup_key
        # with a chatter's live !sr — without shield, the chatter's resolve
        # would die with CancelledError and vanish silently. shield cancels
        # only this waiter; the shared work keeps running for whoever's left.
        existing = self._inflight.get(dedup_key)
        if existing is not None:
            shared_track = await asyncio.shield(existing)
            return (
                dataclasses.replace(shared_track, requester_id=requester_id)
                if shared_track is not None
                else None
            )

        task = asyncio.ensure_future(self._do_resolve(query, dedup_key, now))
        self._inflight[dedup_key] = task

        def _evict(finished: "asyncio.Task[Track | None]", key: str = dedup_key) -> None:
            # Backstop for the shield above: if every waiter walks away
            # before the shared task finishes, nothing else removes it from
            # _inflight, leaving a stale done-task entry for later callers.
            if self._inflight.get(key) is finished:
                del self._inflight[key]
            # Retrieve the exception so asyncio doesn't log "Task exception
            # was never retrieved" when the only waiter was cancelled first.
            if not finished.cancelled() and finished.exception() is not None:
                log.debug("Shared resolve for %r failed with no waiter left.", key)

        task.add_done_callback(_evict)
        try:
            resolved_track = await asyncio.shield(task)
        finally:
            # Only clear our own entry, and only once finished — a
            # concurrent resolve() for a different query could have already
            # reclaimed dedup_key, and shield means this can run while
            # `task` is still in flight (this waiter cancelled, the shared
            # work didn't) — evicting then would send the next caller to
            # start a duplicate extraction. The done-callback above handles
            # that case instead.
            if self._inflight.get(dedup_key) is task and task.done():
                del self._inflight[dedup_key]
        return (
            dataclasses.replace(resolved_track, requester_id=requester_id)
            if resolved_track is not None
            else None
        )

    async def _do_resolve(self, query: str, dedup_key: str, started_at: float) -> Track | None:
        """Extraction + Track-building, run at most once per dedup_key —
        concurrent resolve() calls for the same query all await this one
        task. requester_id isn't baked in here; resolve() applies it
        per-caller via dataclasses.replace() on the shared result.
        """
        try:
            info = await self._extract_info(self._query_for(query))
        except Exception:
            counters.record("resolve_failure")
            raise
        counters.record("resolve_success")

        # A search query wraps its hit in "entries"; a URL resolves straight
        # to the item. entries == [] is a genuine zero-results search, so
        # check "is None" specifically rather than falsy — both read falsy.
        entries = info.get("entries") if isinstance(info, dict) else None
        item = next((e for e in entries if e), None) if entries is not None else info
        if not item:
            return None

        stream_url = item.get("url")
        webpage_url = item.get("webpage_url")
        if not webpage_url:
            if _URL_RE.match(query.strip()):
                webpage_url = query
            else:
                # No stable URL to persist — falling back to raw search text
                # would silently re-search on the next resolve, possibly
                # playing something different from what chat confirmed.
                log.warning(
                    "yt-dlp returned no webpage_url for %r and the query wasn't a URL either "
                    "— refusing to queue it rather than risk a different track playing later.",
                    query,
                )
                return None
        if not stream_url:
            return None

        track = Track(
            title=item.get("title") or "Unknown title",
            webpage_url=webpage_url,
            stream_url=stream_url,
            uploader=item.get("uploader") or "Unknown uploader",
            # yt-dlp reports duration=None for an in-progress livestream,
            # which would otherwise read as 0s and slip past the max-duration
            # check — is_live is what actually flags that case.
            duration=int(item.get("duration") or 0),
            requester_id=0,  # placeholder — each awaiting caller applies its own via resolve()
            thumbnail_url=item.get("thumbnail"),
            query=query,
            is_live=bool(item.get("is_live")),
        )
        ttl = self._settings.ytdlp_cache_ttl_seconds
        if ttl > 0:
            self._cache[webpage_url] = (track, started_at)
            if dedup_key != webpage_url:
                self._cache[dedup_key] = (track, started_at)
        return track
