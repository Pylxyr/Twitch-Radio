"""Queued songs are prepared as soon as they are queued, and a skip needs a ready successor.

These tests need no ffmpeg: the decoder is replaced by a stand-in, and the player's preparation loop
runs beside a pretend "current song" (a stand-in decoder the skip can cut). The real pipeline is
covered end to end in test_player_e2e.py.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from twitch_radio import player as player_mod
from twitch_radio.models import Track
from twitch_radio.player import QueuedRequest, RadioPlayer, SkipResult


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=20))  # type: ignore[arg-type]


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)


class FakeStdout:
    def __init__(self, decoder: FakeDecoder) -> None:
        self._decoder = decoder

    async def read(self, n: int = -1) -> bytes:
        if self._decoder.killed:
            return b""  # end of output, as a real pipe reports once the process is gone
        return b"\x01" * 64


class FakeDecoder:
    """Stands in for a running ffmpeg: alive until killed, with audio ready to read."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.returncode: int | None = None
        self.stdout = FakeStdout(self)
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


def request(name: str, requester_id: int = 7, **kwargs: Any) -> QueuedRequest:
    return QueuedRequest(
        webpage_url=f"https://youtu.be/{name}",
        requester_id=requester_id,
        requester_name="viewer",
        title=name,
        **kwargs,
    )


class Rig:
    """A player whose resolver and decoder spawning are controlled by the test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.resolved: list[str] = []
        self.spawned: list[FakeDecoder] = []
        self.failing: set[str] = set()
        self.gate: asyncio.Event | None = None  # while set and not open, every lookup waits
        self.notices: list[str] = []
        self.player = RadioPlayer(resolver=self._resolve, audio_bitrate_kbps=96)
        monkeypatch.setattr(player_mod, "_spawn", self._spawn)

        async def notify(message: str) -> None:
            self.notices.append(message)

        self.player.set_track_failure_notifier(notify)
        self.current = FakeDecoder("current")

    async def _resolve(self, query: str, requester_id: int) -> Track | None:
        name = query.rsplit("/", 1)[-1]
        self.resolved.append(name)
        if self.gate is not None:
            await self.gate.wait()
        if name in self.failing:
            return None
        return Track(
            title=name,
            webpage_url=query,
            stream_url=f"http://127.0.0.1/{name}",
            uploader="u",
            duration=200,
            requester_id=requester_id,
        )

    async def _spawn(self, *args: str, **kwargs: Any) -> FakeDecoder:
        decoder = FakeDecoder(args[args.index("-i") + 1].rsplit("/", 1)[-1])
        self.spawned.append(decoder)
        return decoder

    def start(self) -> None:
        """Preparation runs; a song is 'playing' (so the player is not idle) but never ends."""
        player = self.player
        player._current_decoder = self.current  # type: ignore[assignment]
        player._active_request = request("current")
        player._prep_task = asyncio.create_task(player._prep_loop(), name="test-prepare")

    def ready(self, name: str) -> bool:
        return any(self.player.is_ready(r) for r in self.player.queued_items() if r.title == name)

    def decoder_of(self, name: str) -> FakeDecoder:
        return next(d for d in self.spawned if d.name == name)

    async def close(self) -> None:
        await self.player.stop()


@pytest.fixture(autouse=True)
def _pretend_ffmpeg_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")


# -- queue adds start preparation ------------------------------------------------------------------


def test_songs_are_prepared_as_soon_as_they_are_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not 60 seconds before the current song ends: it has only just started here."""

    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.start()
        try:
            rig.player.enqueue(request("a"))
            rig.player.enqueue(request("b"))
            await until(lambda: rig.ready("a") and rig.ready("b"))
            assert rig.resolved.count("a") == 1 and rig.resolved.count("b") == 1
            assert rig.player.next_ready
        finally:
            await rig.close()

    run(scenario())


def test_preparation_goes_only_as_deep_as_the_limits_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.start()
        try:
            for name in "abcdefg":
                rig.player.enqueue(request(name))
            await until(lambda: len(rig.resolved) >= player_mod._RESOLVE_AHEAD)
            await asyncio.sleep(0.2)
            assert sorted(set(rig.resolved)) == list("abcd")  # looked up: the first four only
            assert [r.title for r in rig.player.queued_items() if rig.player.is_ready(r)] == ["a", "b"]
            assert len(rig.spawned) == player_mod._WARM_AHEAD  # decoders: the first two only
        finally:
            await rig.close()

    run(scenario())


def test_radio_mix_entries_on_the_queue_are_prepared_too(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.start()
        try:
            rig.player.enqueue(request("mix1", requester_id=0))
            rig.player.enqueue(request("mix2", requester_id=0))
            await until(lambda: rig.ready("mix1") and rig.ready("mix2"))
        finally:
            await rig.close()

    run(scenario())


def test_a_request_jumping_the_line_is_prepared_and_the_pushed_back_decoder_is_freed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.start()
        try:
            for name in ("mix1", "mix2"):
                rig.player.enqueue(request(name, requester_id=0))
            await until(lambda: rig.ready("mix1") and rig.ready("mix2"))
            rig.player.enqueue(request("real"))  # goes ahead of the radio mix
            await until(lambda: rig.ready("real") and rig.ready("mix1"))
            await until(lambda: rig.decoder_of("mix2").killed)  # no longer among the first two
            assert not rig.decoder_of("mix1").killed
            assert [r.title for r in rig.player.queued_items()][:3] == ["real", "mix1", "mix2"]
        finally:
            await rig.close()

    run(scenario())


def test_a_cancelled_song_gives_up_its_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.start()
        try:
            rig.player.enqueue(request("a"))
            await until(lambda: rig.ready("a"))
            rig.player.purge_pending(lambda _r: True)
            assert rig.decoder_of("a").killed
            assert not rig.player._warm and not rig.player._resolved
        finally:
            await rig.close()

    run(scenario())


def test_an_old_decoder_is_replaced_before_it_can_be_used(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(player_mod, "_WARM_MAX_AGE_SECONDS", 0.2)
        rig = Rig(monkeypatch)
        rig.start()
        try:
            rig.player.enqueue(request("a"))
            await until(lambda: rig.ready("a"))
            first = rig.decoder_of("a")
            await until(lambda: first.killed, timeout=5)
            await until(lambda: rig.ready("a") and len(rig.spawned) >= 2)
        finally:
            await rig.close()

    run(scenario())


# -- skip needs a ready successor ------------------------------------------------------------------


def test_skip_is_refused_while_the_next_song_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.gate = asyncio.Event()  # lookups hang
        rig.start()
        try:
            rig.player.enqueue(request("a"))
            await until(lambda: "a" in rig.resolved)
            assert not rig.player.next_ready
            result = await rig.player.skip(wait=0.2)
            assert result is SkipResult.NOT_READY
            assert not rig.current.killed, "the song that is playing must be left alone"
        finally:
            rig.gate.set()
            await rig.close()

    run(scenario())


def test_skip_waits_for_the_next_song_and_then_goes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.gate = asyncio.Event()
        rig.start()
        try:
            rig.player.enqueue(request("a"))
            await until(lambda: "a" in rig.resolved)
            skipping = asyncio.create_task(rig.player.skip(wait=5))
            await asyncio.sleep(0.3)
            assert not skipping.done() and not rig.current.killed, "still waiting, still playing"
            rig.gate.set()
            assert await asyncio.wait_for(skipping, 3) is SkipResult.SKIPPED
            assert rig.ready("a") and rig.current.killed
        finally:
            await rig.close()

    run(scenario())


def test_skip_goes_at_once_when_the_next_song_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.start()
        try:
            rig.player.enqueue(request("a"))
            await until(lambda: rig.ready("a"))
            started = time.monotonic()
            assert await rig.player.skip() is SkipResult.SKIPPED
            assert time.monotonic() - started < 0.5
            assert rig.current.killed
        finally:
            await rig.close()

    run(scenario())


def test_rapid_skips_while_waiting_are_one_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mashing the button must not queue up several skips that all fire the moment the song is ready."""

    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.gate = asyncio.Event()
        rig.start()
        try:
            rig.player.enqueue(request("a"))
            rig.player.enqueue(request("b"))
            await until(lambda: "a" in rig.resolved)
            presses = [asyncio.create_task(rig.player.skip(wait=5)) for _ in range(6)]
            await asyncio.sleep(0.2)
            rig.gate.set()
            results = await asyncio.wait_for(asyncio.gather(*presses), 5)
            assert results == [SkipResult.SKIPPED] * 6
            assert rig.current.killed
            # one song was cut, once: the second song in line was not touched
            assert not any(d.killed for d in rig.spawned)
            assert player_mod.counters.count_last_hour("skips") >= 1
        finally:
            await rig.close()

    run(scenario())


def test_a_song_that_ends_while_skip_waits_is_not_followed_by_a_second_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.gate = asyncio.Event()
        rig.start()
        try:
            rig.player.enqueue(request("a"))
            await until(lambda: "a" in rig.resolved)
            skipping = asyncio.create_task(rig.player.skip(wait=5))
            await asyncio.sleep(0.1)
            rig.player._active_request = request("next")  # the feed loop moved on by itself
            rig.player._current_decoder = (successor := FakeDecoder("next"))  # type: ignore[assignment]
            rig.gate.set()
            assert await asyncio.wait_for(skipping, 3) is SkipResult.SKIPPED
            assert not successor.killed, "the song that started meanwhile must not be cut short"
        finally:
            await rig.close()

    run(scenario())


def test_skip_with_nothing_queued_and_no_radio_just_ends_the_song(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.start()
        try:
            assert rig.player.next_ready
            assert await rig.player.skip() is SkipResult.SKIPPED
            assert rig.current.killed
        finally:
            await rig.close()

    run(scenario())


def test_skip_with_nothing_playing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        rig.player._prep_task = asyncio.create_task(rig.player._prep_loop())
        try:
            assert await rig.player.skip() is SkipResult.NOTHING_PLAYING
        finally:
            await rig.close()

    run(scenario())


def test_skip_with_the_radio_waiting_to_pick_waits_for_that_pick_to_be_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        picks: list[str] = []

        async def suggest(seed: str) -> QueuedRequest | None:
            picks.append(seed)
            await asyncio.sleep(0.2)
            return request("radio-pick", requester_id=0)

        rig.player.set_radio_suggester(suggest)
        rig.player._last_played_webpage_url = "https://youtu.be/seed"
        rig.start()
        try:
            assert await rig.player.skip(wait=5) is SkipResult.SKIPPED
            assert picks == ["https://youtu.be/seed"]
            assert rig.ready("radio-pick"), "the skip may only complete once the pick is ready"
        finally:
            await rig.close()

    run(scenario())


def test_a_song_that_cannot_be_prepared_is_dropped_so_it_cannot_block_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(player_mod, "_PREP_RETRY_DELAYS", (0.05, 0.05))
        rig = Rig(monkeypatch)
        rig.failing.add("broken")
        rig.start()
        released: list[str] = []
        try:
            rig.player.enqueue(request("broken", on_start=lambda: released.append("broken")))
            rig.player.enqueue(request("good"))
            await until(lambda: rig.ready("good"))
            await until(lambda: [r.title for r in rig.player.queued_items()] == ["good"])
            assert rig.resolved.count("broken") == player_mod._PREP_ATTEMPTS
            assert released == ["broken"], "the requester's slot is freed, as when a song is cleared"
            assert any("Couldn't load" in notice for notice in rig.notices)
            assert await rig.player.skip() is SkipResult.SKIPPED
        finally:
            await rig.close()

    run(scenario())


def test_a_livestream_is_dropped_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        rig = Rig(monkeypatch)
        original = rig._resolve

        async def resolve(query: str, requester_id: int) -> Track | None:
            track = await original(query, requester_id)
            if track is not None and query.endswith("live"):
                track.is_live = True
            return track

        rig.player._resolver = resolve
        rig.start()
        try:
            rig.player.enqueue(request("live"))
            await until(lambda: rig.player.queue_size() == 0)
            assert rig.resolved.count("live") == 1
            assert any("livestream" in notice for notice in rig.notices)
        finally:
            await rig.close()

    run(scenario())


def test_radio_filler_that_cannot_be_prepared_leaves_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(player_mod, "_PREP_RETRY_DELAYS", (0.05, 0.05))
        rig = Rig(monkeypatch)
        rig.failing.add("mix")
        rig.start()
        try:
            rig.player.enqueue(request("mix", requester_id=0))
            await until(lambda: rig.player.queue_size() == 0)
            assert rig.notices == []
        finally:
            await rig.close()

    run(scenario())


# -- what listeners are shown ----------------------------------------------------------------------


def test_a_song_that_is_still_loading_stays_in_the_queue_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """It has left the queue but makes no sound yet: the overlay and dashboard must not show it as gone."""

    async def scenario() -> None:
        rig = Rig(monkeypatch)
        player = rig.player
        player.enqueue(request("loading"))
        player.enqueue(request("later"))
        taken = player._pending.pop(0)
        player._active_request = taken  # what the feed loop does when it starts a song
        view = player.audible_view()
        assert [r.title for r in view.queue] == ["loading", "later"]
        assert view.now is None

    run(scenario())
