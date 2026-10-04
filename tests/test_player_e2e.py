"""End-to-end tests for RadioPlayer with a real ffmpeg.

A local HTTP server stands in for YouTube's CDN (the decoder is given
http:// URLs and -reconnect flags, which ffmpeg only accepts for network
inputs). Skipped when ffmpeg isn't installed.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from twitch_radio import player as player_mod
from twitch_radio.models import Track
from twitch_radio.player import QueuedRequest, RadioPlayer

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_audio(path: Path, seconds: float, volume_db: float = 0.0) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
            "-f", "lavfi", "-i", f"sine=f=440:d={seconds}:r=48000",
            "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r=48000:a=0.1",
            "-filter_complex", f"[0][1]amix=inputs=2:normalize=0,volume={volume_db}dB,aformat=channel_layouts=stereo",
            "-c:a", "libopus", "-b:a", "96k", str(path),
        ],
        check=True,
    )  # fmt: skip


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=60))  # type: ignore[arg-type]


async def until(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


class Env:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.server: TestServer | None = None

    async def __aenter__(self) -> Env:
        app = web.Application()
        app.router.add_static("/media", self.directory)  # supports Range requests
        self.server = TestServer(app)
        await self.server.start_server()
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        await self.server.close()

    def url(self, name: str) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.port}/media/{name}"

    def track(self, name: str, seconds: int) -> Track:
        return Track(
            title=name, webpage_url=f"https://youtu.be/{name}", stream_url=self.url(name),
            uploader="test", duration=seconds, requester_id=1,
        )  # fmt: skip

    def player(self, tracks: dict[str, Track], **kwargs: Any) -> RadioPlayer:
        async def resolver(query: str, requester_id: int) -> Track | None:
            return tracks.get(query.rsplit("/", 1)[-1])

        return RadioPlayer(resolver=resolver, audio_bitrate_kbps=96, prefetch_enabled=False, **kwargs)


def request(name: str) -> QueuedRequest:
    return QueuedRequest(
        webpage_url=f"https://youtu.be/{name}", requester_id=1, requester_name="tester", title=name
    )


def test_queue_advances_and_encoder_stays_off_with_nobody_listening(tmp_path: Path) -> None:
    make_audio(tmp_path / "a.webm", 2)
    make_audio(tmp_path / "b.webm", 2)

    async def scenario() -> None:
        async with Env(tmp_path) as env:
            tracks = {"a.webm": env.track("a.webm", 2), "b.webm": env.track("b.webm", 2)}
            player = env.player(tracks, loudness_mode="off")
            player.start()
            try:
                player.enqueue(request("a.webm"))
                player.enqueue(request("b.webm"))
                await until(lambda: player.now_playing is not None)
                assert player.now_playing is not None and player.now_playing.title == "a.webm"
                assert not player.encoder_running
                await until(lambda: player.now_playing is not None and player.now_playing.title == "b.webm")
                await until(lambda: player.now_playing is None and player.queue_size() == 0, timeout=20)
                assert player.encoder_starts == 0, "no listener ever connected, so no encoder should have run"
            finally:
                await player.stop()

    run(scenario())


def test_listener_starts_encoder_and_gets_a_decodable_stream(tmp_path: Path) -> None:
    make_audio(tmp_path / "a.webm", 6)

    async def scenario() -> None:
        async with Env(tmp_path) as env:
            player = env.player({"a.webm": env.track("a.webm", 6)}, loudness_mode="off")
            player.start()
            try:
                queue = player.subscribe()
                header = player.ogg_header_snapshot()
                player.enqueue(request("a.webm"))
                await until(lambda: player.encoder_running)
                received = bytearray(header)
                # The header (if any was ready) goes first, then the live chunks.
                while len(received) < 40_000:
                    received += await asyncio.wait_for(queue.get(), timeout=10)
                assert bytes(received[:4]) == b"OggS"
                assert player.encoder_starts == 1

                # A second listener joining mid-stream must get a decodable start too.
                late = player.subscribe()
                late_bytes = bytearray(player.ogg_header_snapshot())
                for _ in range(8):
                    late_bytes += await asyncio.wait_for(late.get(), timeout=10)
                assert bytes(late_bytes[:4]) == b"OggS"
                assert b"OpusHead" in bytes(late_bytes[:200])
                player.unsubscribe(late)
            finally:
                await player.stop()
            decode = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", "pipe:0", "-f", "null", "-"],
                input=bytes(received), capture_output=True,
            )  # fmt: skip
            assert decode.returncode == 0, decode.stderr.decode(errors="replace")

    run(scenario())


def test_listener_joining_during_header_capture_still_sees_the_first_byte(tmp_path: Path) -> None:
    """The window the lazy encoder opened: a second listener arriving after the
    encoder started but before its header was complete."""
    make_audio(tmp_path / "a.webm", 4)

    async def scenario() -> None:
        async with Env(tmp_path) as env:
            player = env.player({"a.webm": env.track("a.webm", 4)}, loudness_mode="off")
            player.start()
            try:
                first = player.subscribe()
                # Join as soon as the encoder exists, whatever the header state is.
                await until(lambda: player.encoder_running, timeout=10)
                second = player.subscribe()
                second_bytes = bytearray(player.ogg_header_snapshot())
                for _ in range(6):
                    second_bytes += await asyncio.wait_for(second.get(), timeout=10)
                assert bytes(second_bytes[:4]) == b"OggS"
                assert second_bytes.count(b"OpusHead") == 1, "header must appear exactly once"
                first_bytes = bytearray()
                while not first.empty():
                    first_bytes += first.get_nowait()
                assert bytes(first_bytes[:4]) == b"OggS"
            finally:
                await player.stop()

    run(scenario())


def test_encoder_stops_after_the_last_listener_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(player_mod, "_ENCODER_IDLE_GRACE_SECONDS", 0.5)

    async def scenario() -> None:
        async with Env(tmp_path) as env:
            player = env.player({}, loudness_mode="off")
            player.start()
            try:
                queue = player.subscribe()
                await until(lambda: player.encoder_running)
                await asyncio.wait_for(queue.get(), timeout=10)
                player.unsubscribe(queue)
                await until(lambda: not player.encoder_running, timeout=10)
                # ...and a new listener brings it back.
                again = player.subscribe()
                await until(lambda: player.encoder_running)
                await asyncio.wait_for(again.get(), timeout=10)
                assert player.encoder_starts == 2
            finally:
                await player.stop()

    run(scenario())


def test_static_loudness_brings_quiet_and_loud_tracks_close(tmp_path: Path) -> None:
    make_audio(tmp_path / "quiet.webm", 30, volume_db=-24)
    make_audio(tmp_path / "loud.webm", 30, volume_db=3)

    async def scenario() -> tuple[float, float]:
        async with Env(tmp_path) as env:
            gains = []
            for name in ("quiet.webm", "loud.webm"):
                gain = await player_mod._static_gain_db(env.url(name), 30)
                assert gain is not None
                gains.append(gain)
            return gains[0], gains[1]

    quiet_gain, loud_gain = run(scenario())
    # 27 dB apart at the source: the quiet one is boosted as far as the clamp
    # allows, the loud one barely touched.
    assert quiet_gain == pytest.approx(player_mod._LOUDNESS_MAX_GAIN_DB)
    assert quiet_gain - loud_gain > 8
    assert player_mod._LOUDNESS_MIN_GAIN_DB <= loud_gain < player_mod._LOUDNESS_MAX_GAIN_DB


def test_unmeasurable_tracks_fall_back_to_dynamic_loudness(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with Env(tmp_path) as env:
            player = env.player({}, loudness_mode="static")
            unknown_length = Track(
                title="x", webpage_url="u", stream_url=env.url("missing.webm"), uploader="", duration=0, requester_id=1
            )  # fmt: skip
            assert await player._audio_filter_for(unknown_length) == player_mod._DYNAMIC_LOUDNESS_FILTER
            broken = Track(
                title="x", webpage_url="u", stream_url=env.url("missing.webm"), uploader="", duration=120, requester_id=1
            )  # fmt: skip
            assert await player._audio_filter_for(broken) == player_mod._DYNAMIC_LOUDNESS_FILTER
            assert await env.player({}, loudness_mode="off")._audio_filter_for(broken) is None
            assert await env.player({}, loudness_mode="dynamic")._audio_filter_for(broken) == (
                player_mod._DYNAMIC_LOUDNESS_FILTER
            )

    run(scenario())


def test_static_gain_window_selection() -> None:
    calls: list[tuple[float, float]] = []

    async def fake_measure(url: str, start: float, length: float) -> float | None:
        calls.append((start, length))
        return -20.0

    original = player_mod._measure_lufs
    player_mod._measure_lufs = fake_measure  # type: ignore[assignment]
    try:
        assert run(player_mod._static_gain_db("u", 200)) == pytest.approx(4.0)
        assert run(player_mod._static_gain_db("u", 45)) == pytest.approx(4.0)
        assert run(player_mod._static_gain_db("u", 0)) is None
    finally:
        player_mod._measure_lufs = original  # type: ignore[assignment]
    assert calls[0] == (pytest.approx(80.0), pytest.approx(20.0))  # 40% into a 200 s track, 20 s long
    assert calls[1] == (0.0, 45.0)  # short tracks are measured whole


def test_static_loudness_and_gapless_prefetch_play_two_tracks_in_a_row(tmp_path: Path) -> None:
    """The production configuration: static gain filter, look-ahead decoder,
    one listener connected the whole time."""
    make_audio(tmp_path / "a.webm", 8, volume_db=-10)
    make_audio(tmp_path / "b.webm", 8, volume_db=-2)

    async def scenario() -> None:
        async with Env(tmp_path) as env:
            tracks = {"a.webm": env.track("a.webm", 8), "b.webm": env.track("b.webm", 8)}

            async def resolver(query: str, requester_id: int) -> Track | None:
                return tracks.get(query.rsplit("/", 1)[-1])

            player = RadioPlayer(
                resolver=resolver, audio_bitrate_kbps=96, loudness_mode="static", prefetch_enabled=True
            )
            player.start()
            try:
                queue = player.subscribe()
                received = 0

                async def drain() -> None:
                    nonlocal received
                    while True:
                        received += len(await queue.get())

                drainer = asyncio.create_task(drain())
                player.enqueue(request("a.webm"))
                player.enqueue(request("b.webm"))
                await until(lambda: player.now_playing is not None and player.now_playing.title == "a.webm")
                await until(
                    lambda: player.now_playing is not None and player.now_playing.title == "b.webm",
                    timeout=25,
                )
                await until(lambda: player.now_playing is None and player.queue_size() == 0, timeout=25)
                assert player.encoder_starts == 1, "the encoder must not restart between tracks"
                assert received > 50_000
                drainer.cancel()
            finally:
                await player.stop()

    run(scenario())
