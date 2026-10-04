"""ProcessBackend: workers start on demand, are reused most-recent-first, and
exit when idle. A fake worker stands in for the real yt-dlp process."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from twitch_radio import extraction
from twitch_radio.extraction import DownloadError, ProcessBackend


class FakeWorker:
    instances: list[FakeWorker] = []

    def __init__(self, index: int) -> None:
        self._index = index
        self.idle_since = 0.0
        self.started = False
        self.killed = False
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.outcome: BaseException | None = None
        FakeWorker.instances.append(self)

    @property
    def pid(self) -> int:
        return 1000 + self._index

    async def start(self) -> None:
        self.started = True

    async def request(self, query: str, options: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.outcome is not None:
            raise self.outcome
        return {"id": query, "worker": self._index}

    async def kill(self) -> None:
        self.killed = True


@pytest.fixture(autouse=True)
def fake_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeWorker.instances = []
    monkeypatch.setattr(extraction, "_ExtractionWorker", FakeWorker)


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=20))


def test_no_worker_exists_until_the_first_request() -> None:
    async def scenario() -> None:
        backend = ProcessBackend(2, idle_seconds=60)
        assert backend.status()["alive"] == 0
        assert FakeWorker.instances == []
        info = await backend.extract("a", {}, 5)
        assert info["worker"] == 1
        assert backend.status() == {"mode": "process", "size": 2, "alive": 1, "idle": 1}
        await backend.aclose()

    run(scenario())


def test_sequential_requests_reuse_one_worker() -> None:
    async def scenario() -> None:
        backend = ProcessBackend(2, idle_seconds=60)
        for query in "abc":
            await backend.extract(query, {}, 5)
        assert len(FakeWorker.instances) == 1
        assert FakeWorker.instances[0].calls == 3
        await backend.aclose()

    run(scenario())


def test_a_second_worker_starts_only_under_concurrent_load() -> None:
    async def scenario() -> None:
        backend = ProcessBackend(2, idle_seconds=60)
        await backend.extract("warm", {}, 5)
        first = FakeWorker.instances[0]
        first.gate = asyncio.Event()
        slow = asyncio.create_task(backend.extract("slow", {}, 5))
        await asyncio.sleep(0.05)
        info = await backend.extract("fast", {}, 5)  # worker 1 is busy: a new one is started
        assert info["worker"] == 2
        first.gate.set()
        await slow
        assert backend.status()["alive"] == 2
        await backend.aclose()

    run(scenario())


def test_concurrency_is_capped_at_the_pool_size() -> None:
    async def scenario() -> None:
        backend = ProcessBackend(1, idle_seconds=60)
        await backend.extract("warm", {}, 5)
        FakeWorker.instances[0].gate = gate = asyncio.Event()
        first = asyncio.create_task(backend.extract("one", {}, 5))
        second = asyncio.create_task(backend.extract("two", {}, 5))
        await asyncio.sleep(0.1)
        assert len(FakeWorker.instances) == 1, "a second request must wait, not spawn"
        assert not second.done()
        gate.set()
        await asyncio.gather(first, second)
        await backend.aclose()

    run(scenario())


def test_idle_workers_exit_after_the_idle_period(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        backend = ProcessBackend(2, idle_seconds=0.6)
        await backend.extract("a", {}, 5)
        worker = FakeWorker.instances[0]
        assert not worker.killed
        await asyncio.sleep(1.6)
        assert worker.killed
        assert backend.status()["alive"] == 0
        # ...and the next request just starts a fresh one.
        info = await backend.extract("b", {}, 5)
        assert info["worker"] == 2
        await backend.aclose()

    run(scenario())


def test_a_busy_worker_is_not_reaped() -> None:
    async def scenario() -> None:
        backend = ProcessBackend(1, idle_seconds=0.5)
        await backend.extract("warm", {}, 5)
        worker = FakeWorker.instances[0]
        for _ in range(4):  # keeps it in use across several idle periods
            await asyncio.sleep(0.3)
            await backend.extract("again", {}, 5)
        assert not worker.killed
        await backend.aclose()

    run(scenario())


def test_timeout_discards_the_worker_and_raises_download_error() -> None:
    async def scenario() -> None:
        backend = ProcessBackend(1, idle_seconds=60)
        await backend.extract("warm", {}, 5)
        FakeWorker.instances[0].outcome = TimeoutError()
        with pytest.raises(DownloadError, match="Timed out"):
            await backend.extract("slow", {}, 5)
        assert FakeWorker.instances[0].killed
        assert backend.status()["alive"] == 0
        info = await backend.extract("next", {}, 5)  # recovers by starting a new worker
        assert info["worker"] == 2
        await backend.aclose()

    run(scenario())


def test_a_clean_extraction_failure_keeps_the_worker() -> None:
    async def scenario() -> None:
        backend = ProcessBackend(1, idle_seconds=60)
        await backend.extract("warm", {}, 5)
        FakeWorker.instances[0].outcome = DownloadError("Video unavailable")
        with pytest.raises(DownloadError, match="unavailable"):
            await backend.extract("bad", {}, 5)
        assert not FakeWorker.instances[0].killed
        assert backend.status()["alive"] == 1
        await backend.aclose()

    run(scenario())


def test_cancelled_request_discards_its_worker() -> None:
    async def scenario() -> None:
        backend = ProcessBackend(1, idle_seconds=60)
        await backend.extract("warm", {}, 5)
        FakeWorker.instances[0].gate = asyncio.Event()
        task = asyncio.create_task(backend.extract("stuck", {}, 5))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert FakeWorker.instances[0].killed, "its eventual reply would desynchronise the next caller"
        await backend.aclose()

    run(scenario())


def test_a_worker_that_cannot_start_raises_download_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(FakeWorker):
        async def start(self) -> None:
            raise OSError("no such file")

    monkeypatch.setattr(extraction, "_ExtractionWorker", Broken)

    async def scenario() -> None:
        backend = ProcessBackend(1, idle_seconds=60)
        with pytest.raises(DownloadError, match="Couldn't start"):
            await backend.extract("a", {}, 5)
        assert backend.status()["alive"] == 0
        await backend.aclose()

    run(scenario())


def test_aclose_stops_everything_and_refuses_new_work() -> None:
    async def scenario() -> None:
        backend = ProcessBackend(2, idle_seconds=60)
        await backend.extract("a", {}, 5)
        await backend.aclose()
        assert all(w.killed for w in FakeWorker.instances)
        with pytest.raises(DownloadError):
            await backend.extract("b", {}, 5)

    run(scenario())


def test_the_bot_process_never_imports_yt_dlp() -> None:
    import subprocess
    import sys

    code = "import sys, twitch_radio.extraction, twitch_radio.service; print('yt_dlp' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
