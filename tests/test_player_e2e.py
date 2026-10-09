"""End-to-end tests for RadioPlayer with a real ffmpeg.

A local HTTP server stands in for YouTube's CDN (the decoder is given
http:// URLs and -reconnect flags, which ffmpeg only accepts for network
inputs). Skipped when ffmpeg isn't installed.
"""

from __future__ import annotations

import asyncio
import os
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


# Test audio is made with a full ffmpeg (lavfi sources, amix); the player under test can be run against
# the small shipped build instead by putting that build first on PATH and pointing this at the full one:
#   FIXTURE_FFMPEG=/usr/bin/ffmpeg PATH=packaging/bin:$PATH pytest tests/test_player_e2e.py
FIXTURE_FFMPEG = os.environ.get("FIXTURE_FFMPEG", "ffmpeg")


def make_audio(path: Path, seconds: float, volume_db: float = 0.0) -> None:
    subprocess.run(
        [
            FIXTURE_FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-nostdin",
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
            player = env.player(tracks)
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
            player = env.player({"a.webm": env.track("a.webm", 6)})
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
                [FIXTURE_FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", "pipe:0", "-f", "null", "-"],
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
            player = env.player({"a.webm": env.track("a.webm", 4)})
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
            player = env.player({})
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


def test_gapless_prefetch_plays_two_tracks_in_a_row(tmp_path: Path) -> None:
    """The production configuration: look-ahead decoder, one listener connected
    the whole time."""
    make_audio(tmp_path / "a.webm", 8, volume_db=-10)
    make_audio(tmp_path / "b.webm", 8, volume_db=-2)

    async def scenario() -> None:
        async with Env(tmp_path) as env:
            tracks = {"a.webm": env.track("a.webm", 8), "b.webm": env.track("b.webm", 8)}

            async def resolver(query: str, requester_id: int) -> Track | None:
                return tracks.get(query.rsplit("/", 1)[-1])

            player = RadioPlayer(resolver=resolver, audio_bitrate_kbps=96, prefetch_enabled=True)
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


def test_decoder_never_reads_the_parent_control_pipe() -> None:
    """The core's stdin is the desktop app's control channel; a decoder that
    inherited it would read it for ffmpeg's interactive keys."""
    from twitch_radio.player import _decoder_cmd

    cmd = _decoder_cmd("http://127.0.0.1/x")
    assert "-nostdin" in cmd
    assert cmd.index("-nostdin") < cmd.index("-i")


def test_songs_play_at_their_own_volume() -> None:
    """No loudness normalisation: the decoder applies no audio filter at all."""
    cmd = player_mod._decoder_cmd("http://127.0.0.1/x.webm")
    assert "-af" not in cmd and "-filter:a" not in cmd and "-filter_complex" not in cmd
    assert not hasattr(player_mod, "_DYNAMIC_LOUDNESS_FILTER")


def _silent_stretches(ogg: bytes, directory: Path) -> tuple[float, list[float]]:
    """Total length of a captured stream and every silent stretch in it (>= 0.15 s below -45 dB)."""
    path = directory / "captured.ogg"
    path.write_bytes(ogg)
    pcm = subprocess.run(
        [FIXTURE_FFMPEG, "-v", "error", "-i", str(path), "-f", "s16le", "-ar", "48000", "-ac", "2", "-"],
        capture_output=True,
        check=True,
    ).stdout
    detect = subprocess.run(
        [FIXTURE_FFMPEG, "-hide_banner", "-nostats", "-i", str(path), "-af", "silencedetect=noise=-45dB:d=0.15",
         "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stderr  # fmt: skip
    gaps = [
        float(line.split("silence_duration:")[1])
        for line in detect.splitlines()
        if "silence_duration" in line
    ]
    return len(pcm) / (48000 * 2 * 2), gaps


def test_no_silence_between_songs_even_when_the_reported_duration_is_wrong(tmp_path: Path) -> None:
    """Metadata durations are rarely exact, and the CDN takes a moment to answer. The next song is
    prepared by how much of the current one has actually played, not by its claimed length, so the
    hand-off is still seamless: the captured stream is as long as the songs and has no silence."""
    names = ["a.webm", "b.webm", "c.webm"]
    for name in names:
        make_audio(tmp_path / name, 5)

    async def scenario() -> bytes:
        async with Env(tmp_path) as env:
            # Reported 7 s for songs that are really 5 s long, and the server takes a second to open one.
            tracks = {name: env.track(name, 7) for name in names}

            async def resolver(query: str, requester_id: int) -> Track | None:
                return tracks.get(query.rsplit("/", 1)[-1])

            player = RadioPlayer(resolver=resolver, audio_bitrate_kbps=96, prefetch_enabled=True)
            player.start()
            captured = bytearray()
            try:
                queue = player.subscribe()
                captured += player.ogg_header_snapshot()

                async def drain() -> None:
                    while True:
                        captured.extend(await queue.get())

                drainer = asyncio.create_task(drain())
                for name in names:
                    player.enqueue(request(name))
                seen: set[str] = set()
                deadline = asyncio.get_running_loop().time() + 40
                while len(seen) < len(names) or player.now_playing is not None or player.queue_size():
                    assert asyncio.get_running_loop().time() < deadline, "songs did not finish in time"
                    if player.now_playing is not None:
                        seen.add(player.now_playing.title)
                    await asyncio.sleep(0.05)
                await asyncio.sleep(1.0)
                drainer.cancel()
            finally:
                await player.stop()
            return bytes(captured)

    stream = run_long(scenario())
    total, gaps = _silent_stretches(stream, tmp_path)
    assert abs(total - 15) < 0.6, f"expected about 15 s of audio, the stream holds {total:.2f} s"
    assert gaps == [], f"silence between songs: {gaps}"


def run_long(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=90))  # type: ignore[arg-type]


def _production_player(env: Env, names: list[str], seconds: int, **kwargs: Any) -> RadioPlayer:
    tracks = {name: env.track(name, seconds) for name in names}

    async def resolver(query: str, requester_id: int) -> Track | None:
        return tracks.get(query.rsplit("/", 1)[-1])

    return RadioPlayer(resolver=resolver, audio_bitrate_kbps=96, prefetch_enabled=True, **kwargs)


def test_skip_goes_straight_to_the_song_that_was_prepared_while_the_first_one_played(tmp_path: Path) -> None:
    """Songs are prepared when they are queued, not shortly before the current one ends, so a skip a
    few seconds into the first song lands on a ready one: no gap, no encoder restart."""
    names = ["a.webm", "b.webm", "c.webm"]
    for name in names:
        make_audio(tmp_path / name, 30)

    async def scenario() -> None:
        async with Env(tmp_path) as env:
            player = _production_player(env, names, 30)
            player.start()
            try:
                queue = player.subscribe()

                async def drain() -> None:
                    while True:
                        await queue.get()

                drainer = asyncio.create_task(drain())
                for name in names:
                    player.enqueue(request(name))
                await until(lambda: player.now_playing is not None and player.now_playing.title == "a.webm")
                await until(lambda: player.next_ready, timeout=10)  # b is warm long before a is near its end
                started = asyncio.get_running_loop().time()
                assert await player.skip() is player_mod.SkipResult.SKIPPED
                await until(
                    lambda: player.now_playing is not None and player.now_playing.title == "b.webm", timeout=3
                )
                assert asyncio.get_running_loop().time() - started < 2.0
                assert player.encoder_starts == 1
                drainer.cancel()
            finally:
                await player.stop()

    run(scenario())


def test_rapid_skips_never_get_ahead_of_the_audio(tmp_path: Path) -> None:
    """Hammering skip: every skip that is accepted lands on the next song, which really starts; the
    ones that are refused leave the current song alone; and what the overlay shows never loses a song."""
    names = [f"{c}.webm" for c in "abcdef"]
    for name in names:
        make_audio(tmp_path / name, 30)

    async def scenario() -> None:
        async with Env(tmp_path) as env:
            player = _production_player(env, names, 30, overlay_delay_seconds=0.5)
            player.start()
            try:
                queue = player.subscribe()

                async def drain() -> None:
                    while True:
                        await queue.get()

                drainer = asyncio.create_task(drain())
                for name in names:
                    player.enqueue(request(name))
                await until(lambda: player.now_playing is not None and player.now_playing.title == "a.webm")

                accepted = refused = 0
                for _ in range(40):  # as fast as the answers come back, until the last song is reached
                    assert player.now_playing is not None
                    before = player.now_playing.title
                    if before == names[-1]:
                        break
                    result = await player.skip(wait=0.3)
                    if result is player_mod.SkipResult.SKIPPED:
                        accepted += 1
                        expected = names[names.index(before) + 1]
                        await until(
                            lambda e=expected: (
                                player.now_playing is not None and player.now_playing.title == e
                            ),
                            timeout=5,
                        )
                    else:
                        refused += 1
                        assert result is player_mod.SkipResult.NOT_READY
                        assert player.now_playing is not None and player.now_playing.title == before
                    current = player.now_playing.title
                    # nothing that has not played yet has disappeared from what listeners are shown
                    view = player.audible_view()
                    shown = {item.title for item in view.queue} | ({view.now.title} if view.now else set())
                    assert set(names[names.index(current) :]) <= shown, (current, shown)
                assert accepted >= 1
                final = player.now_playing
                assert final is not None and final.title == names[accepted], (
                    "every accepted skip moved on by exactly one song"
                )
                assert player.queue_size() == len(names) - 1 - accepted
                assert player.encoder_starts == 1
                drainer.cancel()
            finally:
                await player.stop()

    run(scenario())
