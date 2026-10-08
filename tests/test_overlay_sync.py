"""What the overlay and the dashboard show follows what listeners hear, not what the player does.

The stream reaches OBS (or the app's audio element) a few seconds after the player produced it, so
the player's own "now playing" runs ahead of the sound. The audible view lags it by the overlay delay.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import Any

from twitch_radio.models import Track
from twitch_radio.player import NowPlaying, QueuedRequest, RadioPlayer


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=20))  # type: ignore[arg-type]


async def _no_resolve(query: str, requester_id: int) -> Track | None:
    return None


def song(title: str) -> NowPlaying:
    return NowPlaying(
        title=title,
        uploader="u",
        thumbnail_url=None,
        requester_name="viewer",
        requester_id=7,
        webpage_url=f"https://youtu.be/{title}",
        started_at=time.monotonic(),
        duration=200,
    )


def queued(title: str, requester_id: int = 7) -> QueuedRequest:
    return QueuedRequest(
        webpage_url=f"https://youtu.be/{title}",
        requester_id=requester_id,
        requester_name="viewer",
        title=title,
    )


def make_player(delay: float) -> RadioPlayer:
    return RadioPlayer(resolver=_no_resolve, audio_bitrate_kbps=96, overlay_delay_seconds=delay)


def test_a_new_song_is_shown_only_when_it_is_audible() -> None:
    async def scenario() -> None:
        player = make_player(0.4)
        player._set_now_playing(song("a"))
        view = player.audible_view()
        assert player.now_playing is not None and player.now_playing.title == "a"  # the player moved on
        assert view.now is None, "nothing is audible yet"
        assert [item.title for item in view.queue] == ["a"], "...so it is still 'up next'"
        await asyncio.sleep(0.5)
        view = player.audible_view()
        assert view.now is not None and view.now.title == "a"
        assert view.queue == []
        assert 0.0 <= view.elapsed < 0.3, "elapsed counts from when the song became audible"

    run(scenario())


def test_a_song_that_ended_stays_until_its_last_sound_is_heard() -> None:
    async def scenario() -> None:
        player = make_player(0.3)
        player._set_now_playing(song("a"))
        await asyncio.sleep(0.4)
        player._set_now_playing(None)  # the player is done with it (or it was skipped)
        assert player.now_playing is None
        assert player.audible_view().now is not None, "listeners are still hearing the tail"
        await asyncio.sleep(0.4)
        assert player.audible_view().now is None

    run(scenario())


def test_back_to_back_songs_never_flash_nothing_playing() -> None:
    async def scenario() -> None:
        player = make_player(0.3)
        player._set_now_playing(song("a"))
        await asyncio.sleep(0.4)
        seen: list[str | None] = []
        player._set_now_playing(None)
        player._set_now_playing(song("b"))  # the gapless hand-off: a few milliseconds apart
        end = time.monotonic() + 0.6
        while time.monotonic() < end:
            now = player.audible_view().now
            seen.append(now.title if now else None)
            await asyncio.sleep(0.02)
        assert None not in seen, seen
        assert seen[0] == "a" and seen[-1] == "b"

    run(scenario())


def test_a_new_request_appears_in_the_queue_at_once() -> None:
    async def scenario() -> None:
        player = make_player(5.0)
        player.enqueue(queued("later"))
        assert [item.title for item in player.audible_view().queue] == ["later"]

    run(scenario())


def test_zero_delay_follows_the_player_exactly() -> None:
    async def scenario() -> None:
        player = make_player(0.0)
        player._set_now_playing(song("a"))
        view = player.audible_view()
        assert view.now is not None and view.now.title == "a" and view.queue == []

    run(scenario())


def test_state_subscribers_are_woken_when_the_change_becomes_audible() -> None:
    async def scenario() -> None:
        player = make_player(0.3)
        wakeups = player.subscribe_state()
        player._set_now_playing(song("a"))
        await asyncio.wait_for(wakeups.get(), timeout=1)  # the player's own change
        while not wakeups.empty():
            wakeups.get_nowait()
        await asyncio.wait_for(wakeups.get(), timeout=1)  # and again when it is audible
        assert player.audible_view().now is not None

    run(scenario())
