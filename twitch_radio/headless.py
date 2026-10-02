"""Runs the bot as a child of the desktop app, talking JSON lines.

Protocol (one JSON object per line, UTF-8):

  core -> app (stdout)
    {"t":"hello", ...}          once, as soon as the process is up
    {"t":"log", ts, level, logger, msg, cont?}
    {"t":"phase", "phase": "starting|running|stopping|stopped"}
    {"t":"state", ...}          about once a second while running
    {"t":"fatal", message, hint, code}
    {"t":"ack", id, ok, ...}    reply to a command that carried an "id"

  app -> core (stdin)
    {"cmd":"stop"|"skip"|"pause"|"resume"|"clear_queue", "id"?: n}

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


def _read_commands(loop: asyncio.AbstractEventLoop, runtime: BotRuntime, emitter: Emitter) -> None:
    log = logging.getLogger("twitch_radio.headless")
    stdin = sys.stdin.buffer if sys.stdin is not None else None
    if stdin is None:
        return

    def dispatch(message: dict[str, Any]) -> None:
        command = message.get("cmd")
        reply: dict[str, Any] = {"t": "ack", "id": message.get("id"), "ok": True}
        if command == "stop":
            runtime.request_stop("stop requested from the app")
        elif command == "skip":
            reply["ok"] = runtime.skip()
        elif command == "pause":
            reply["ok"] = runtime.pause()
        elif command == "resume":
            reply["ok"] = runtime.resume()
        elif command == "clear_queue":
            reply["removed"] = runtime.clear_queue()
        else:
            reply.update(ok=False, error=f"unknown command {command!r}")
            log.warning("Ignoring unknown command %r", command)
        if message.get("id") is not None:
            emitter.send(reply)

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
            loop.call_soon_threadsafe(dispatch, message)


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
