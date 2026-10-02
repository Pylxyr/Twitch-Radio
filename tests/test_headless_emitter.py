import json
import threading
import time

from twitch_radio import headless


class _BlockingStream:
    """A pipe whose reader has stalled: write() blocks until released."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lines: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.release.wait(10)
        self.lines.append(data)

    def flush(self) -> None:
        pass


def _wait_for(predicate, timeout: float = 10.0) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_logs_are_dropped_but_state_is_kept_when_the_reader_stalls() -> None:
    stream = _BlockingStream()
    emitter = headless.Emitter(stream)
    emitter.send({"t": "phase", "phase": "running"})  # occupies the writer thread
    for i in range(headless._MAX_BACKLOG + 2000):
        emitter.send({"t": "log", "msg": str(i)})
    queued_logs = emitter._pending
    assert queued_logs <= headless._MAX_BACKLOG + 2  # the flood was capped, not queued in full
    emitter.send({"t": "state", "n": 1})
    emitter.send({"t": "fatal", "message": "boom"})
    assert emitter._pending == queued_logs + 2  # control messages still get in

    stream.release.set()
    assert _wait_for(lambda: emitter._pending == 0)
    kinds = [json.loads(line)["t"] for line in stream.lines]
    assert kinds.count("log") < headless._MAX_BACKLOG + 2000
    assert {"phase", "state", "fatal"} <= set(kinds)
    emitter.close()


def test_everything_is_delivered_when_the_reader_keeps_up() -> None:
    stream = _BlockingStream()
    stream.release.set()
    emitter = headless.Emitter(stream)
    for i in range(50):
        emitter.send({"t": "log", "msg": str(i)})
    assert _wait_for(lambda: len(stream.lines) == 50)
    assert [json.loads(line)["msg"] for line in stream.lines] == [str(i) for i in range(50)]
    emitter.close()


def test_lines_are_utf8_json_with_newline() -> None:
    stream = _BlockingStream()
    stream.release.set()
    emitter = headless.Emitter(stream)
    emitter.send({"t": "log", "msg": "naïve ✓"})
    assert _wait_for(lambda: len(stream.lines) == 1)
    assert stream.lines[0].endswith(b"\n")
    assert json.loads(stream.lines[0].decode("utf-8"))["msg"] == "naïve ✓"
    emitter.close()


def test_a_broken_pipe_stops_the_writer_quietly() -> None:
    class _Broken:
        def write(self, _data: bytes) -> None:
            raise OSError("pipe closed")

        def flush(self) -> None:
            pass

    emitter = headless.Emitter(_Broken())
    emitter.send({"t": "state"})
    assert _wait_for(lambda: emitter.broken)
    emitter.send({"t": "state"})  # must not raise or queue
    emitter.close()


def test_logbus_includes_tracebacks() -> None:
    import logging

    from twitch_radio.logbus import LogBus

    bus = LogBus()
    seen: list[dict[str, object]] = []
    bus.add_sink(lambda entry: seen.append(dict(entry)))
    logger = logging.getLogger("tests.logbus")
    logger.addHandler(bus)
    logger.propagate = False
    try:
        try:
            raise ValueError("kaboom")
        except ValueError:
            logger.exception("it failed")
    finally:
        logger.removeHandler(bus)
    messages = [str(entry["msg"]) for entry in seen]
    assert messages[0] == "it failed"
    assert any("ValueError: kaboom" in message for message in messages[1:])
