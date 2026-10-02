"""Fans log records out to whoever is listening (the desktop app).

A logging.Handler that turns each record into a small dict and hands it to
registered sinks. It never raises and never blocks on the event loop: a sink
that fails is dropped from the record, not the record from the log.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

LogEntry = dict[str, Any]
LogSink = Callable[[LogEntry], None]
# logging.Handler has no formatException; the Formatter does.
_FORMATTER = logging.Formatter()


class LogBus(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._sinks: list[LogSink] = []
        self._sink_lock = threading.Lock()

    def add_sink(self, sink: LogSink) -> None:
        with self._sink_lock:
            self._sinks.append(sink)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            trace = ""
            if record.exc_info:
                trace = _FORMATTER.formatException(record.exc_info)
            elif record.exc_text:
                trace = record.exc_text
            entries: list[LogEntry] = []
            first, *rest = (message.splitlines() or [""])
            entries.append({"ts": record.created, "level": record.levelname, "logger": record.name, "msg": first})
            # One entry per line so the viewer can keep fixed-height rows;
            # `cont` marks a continuation of the entry above it.
            for extra in [*rest, *trace.splitlines()]:
                entries.append(
                    {"ts": record.created, "level": record.levelname, "logger": record.name, "msg": extra, "cont": True}
                )
            with self._sink_lock:
                sinks = list(self._sinks)
            for entry in entries:
                for sink in sinks:
                    try:
                        sink(entry)
                    except Exception:  # noqa: BLE001 - logging must never raise
                        pass
        except Exception:  # noqa: BLE001
            self.handleError(record)
