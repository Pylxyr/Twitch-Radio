"""Runs the bot as a child of the desktop app, talking JSON lines.

Protocol (one JSON object per line, UTF-8):

  core -> app (stdout)
    {"t":"hello", ...}          once, as soon as the process is up
    {"t":"log", ts, level, logger, msg, cont?}
    {"t":"phase", "phase": "starting|running|stopping|stopped"}
    {"t":"state", ...}          when the player's state changes, plus every few
                                seconds while the app says it is watching
    {"t":"fatal", message, hint, code}
    {"t":"ack", id, ok, ...}    reply to a command that carried an "id"

  app -> core (stdin)
    {"cmd":"stop"|"skip"|"pause"|"resume"|"clear_queue", "id"?: n}
    {"cmd":"search", "query": "song name", "id": n}   up to six matches for the dashboard to choose from;
    {"cmd":"request", "query": "song name or link", "id": n}   queue a song for the streamer;
                                the ack carries {"ok", "title", "position"} or {"ok": false, "error"}
    {"cmd":"radio_lookahead", "id"?: n}   the radio-mix lookahead / auto-radio setting changed:
                                re-read it and trim or top up the queue
    {"cmd":"watch", "on": true|false}   the dashboard is / is not on screen

stdin reaching EOF means the app is gone (closed, crashed, killed): the bot
stops itself rather than keep playing audio nobody can control.

stdout is reserved for the protocol - anything else that prints is pointed at
stderr, which the app also shows in the Logs tab.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from twitch_radio.logbus import LogBus
from twitch_radio.paths import is_frozen
from twitch_radio.service import EXIT_CONFIG, BotRuntime
from twitch_radio.version import APP_VERSION

# More than this many unsent lines means the app stopped reading; logs are
# dropped (state and control messages are not) rather than growing without
# bound or, worse, blocking the event loop that paces the audio.
_MAX_BACKLOG = 5000


class Emitter:
    """Thread-safe, non-blocking writer for the protocol."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._queue: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._pending = 0
        self.broken = False
        self._thread = threading.Thread(target=self._pump, name="protocol-writer", daemon=True)
        self._thread.start()

    def send(self, message: dict[str, Any]) -> None:
        if self.broken:
            return
        if message.get("t") == "log" and self._pending > _MAX_BACKLOG:
            return
        try:
            line = (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        except (TypeError, ValueError):
            return
        with self._lock:
            self._pending += 1
        self._queue.put(line)

    def _pump(self) -> None:
        while True:
            line = self._queue.get()
            if line is None:
                return
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError):
                self.broken = True
                return
            finally:
                with self._lock:
                    self._pending -= 1

    def close(self, timeout: float = 3.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)


def _bootstrap_logging(emitter: Emitter) -> LogBus:
    bus = LogBus()
    bus.add_sink(lambda entry: emitter.send({"t": "log", **entry}))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(bus)
    root.setLevel(logging.INFO)
    return bus


def build_dispatcher(runtime: BotRuntime, emitter: Emitter) -> Callable[[dict[str, Any]], None]:
    """The function that carries out one command from the app. Must be called on the event loop."""
    log = logging.getLogger("twitch_radio.headless")
    background: set[asyncio.Task[None]] = set()

    def run_async(message: dict[str, Any], work: Any) -> None:
        """Commands that take a while (a song lookup): answered when they finish, not before."""

        async def finish() -> None:
            reply: dict[str, Any] = {"t": "ack", "id": message.get("id")}
            try:
                reply.update(await work())
            except Exception:  # noqa: BLE001 - the app gets an answer either way
                log.exception("Command %r failed", message.get("cmd"))
                reply.update(ok=False, error="Something went wrong. See the Logs tab.")
            if message.get("id") is not None:
                emitter.send(reply)

        task = asyncio.get_running_loop().create_task(finish(), name=f"command-{message.get('cmd')}")
        background.add(task)
        task.add_done_callback(background.discard)

    def dispatch(message: dict[str, Any]) -> None:
        command = message.get("cmd")
        reply: dict[str, Any] = {"t": "ack", "id": message.get("id"), "ok": True}
        if command == "request":
            query = message.get("query")

            async def request() -> dict[str, Any]:
                return await runtime.request_song(query if isinstance(query, str) else "")

            run_async(message, request)
            return
        if command == "search":
            search_query = message.get("query")

            async def search() -> dict[str, Any]:
                return await runtime.search_songs(search_query if isinstance(search_query, str) else "")

            run_async(message, search)
            return
        if command == "radio_lookahead":

            async def apply() -> dict[str, Any]:
                await runtime.apply_radio_lookahead()
                return {"ok": True}

            run_async(message, apply)
            return
        if command == "stop":
            runtime.request_stop("stop requested from the app")
        elif command == "skip":

            async def skip() -> dict[str, Any]:
                return await runtime.skip()

            run_async(message, skip)
            return
        elif command == "pause":
            reply["ok"] = runtime.pause()
        elif command == "resume":
            reply["ok"] = runtime.resume()
        elif command == "clear_queue":
            reply["removed"] = runtime.clear_queue()
        elif command == "watch":
            runtime.set_watching(bool(message.get("on")))
        else:
            reply.update(ok=False, error=f"unknown command {command!r}")
            log.warning("Ignoring unknown command %r", command)
        if message.get("id") is not None:
            emitter.send(reply)

    return dispatch


def _read_commands(loop: asyncio.AbstractEventLoop, runtime: BotRuntime, emitter: Emitter) -> None:
    stdin = sys.stdin.buffer if sys.stdin is not None else None
    if stdin is None:
        return
    dispatch: Callable[[dict[str, Any]], None] | None = None

    def ensure_dispatcher(message: dict[str, Any]) -> None:
        nonlocal dispatch
        if dispatch is None:
            dispatch = build_dispatcher(runtime, emitter)
        dispatch(message)

    while True:
        try:
            raw = stdin.readline()
        except (OSError, ValueError):
            raw = b""
        if not raw:
            loop.call_soon_threadsafe(runtime.request_stop, "the app closed the control channel")
            return
        try:
            message = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(message, dict):
            loop.call_soon_threadsafe(ensure_dispatcher, message)


def run_headless() -> int:
    # Claim the real stdout for the protocol before anything can print to it.
    protocol_out = sys.stdout.buffer if sys.stdout is not None else open(os.devnull, "wb")
    if sys.stdout is not None:
        sys.stdout = sys.stderr
    emitter = Emitter(protocol_out)
    emitter.send(
        {"t": "hello", "pid": os.getpid(), "version": APP_VERSION, "frozen": is_frozen(), "ts": time.time()}
    )
    bus = _bootstrap_logging(emitter)

    # Imported here so config problems surface as a message, not a traceback
    # before logging exists.
    from twitch_radio.bot import configure_logging
    from twitch_radio.config import load_settings

    code = EXIT_CONFIG
    try:
        try:
            settings = load_settings()
        except RuntimeError as exc:
            logging.getLogger("twitch_radio.headless").error("Config problem: %s", exc)
            emitter.send(
                {
                    "t": "fatal",
                    "message": str(exc),
                    "hint": "Fix it in Settings, then press Start.",
                    "code": EXIT_CONFIG,
                }
            )
            emitter.send({"t": "phase", "phase": "stopped"})
            return EXIT_CONFIG

        configure_logging(settings, console=False, bus=bus)
        logging.getLogger("twitch_radio.headless").info("Twitch Radio core %s starting.", APP_VERSION)

        async def main() -> int:
            runtime = BotRuntime(settings, emit=emitter.send)
            loop = asyncio.get_running_loop()
            threading.Thread(
                target=_read_commands, args=(loop, runtime, emitter), name="protocol-reader", daemon=True
            ).start()
            return await runtime.run()

        code = asyncio.run(main())
    finally:
        logging.shutdown()
        emitter.close()
    return code
