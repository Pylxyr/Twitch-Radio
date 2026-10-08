"""The dashboard's song picker (search results to choose from) and thumbnails in the queue."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest

from twitch_radio.extraction import Resolver
from twitch_radio.models import Track, youtube_thumbnail
from twitch_radio.player import QueuedRequest, RadioPlayer
from twitch_radio.radio import RadioSuggester
from twitch_radio.service import BotRuntime
from twitch_radio.store import JsonStore


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=20))  # type: ignore[arg-type]


class FakeBackend:
    def __init__(self, info: dict[str, Any]) -> None:
        self.info = info
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def extract(self, query: str, options: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.calls.append((query, options))
        return self.info


def resolver_with(info: dict[str, Any]) -> tuple[Resolver, FakeBackend]:
    resolver = Resolver.__new__(Resolver)
    backend = FakeBackend(info)
    resolver._backend = backend  # type: ignore[assignment]
    resolver._build_options = lambda: {}  # type: ignore[method-assign]
    return resolver, backend


def test_search_returns_choices_with_pictures_and_skips_livestreams() -> None:
    resolver, backend = resolver_with(
        {
            "entries": [
                {"id": "abcdefghijk", "title": "Song A", "uploader": "Artist", "duration": 201},
                {
                    "id": "lmnopqrstuv",
                    "title": "Song B",
                    "channel": "Chan",
                    "url": "https://www.youtube.com/watch?v=lmnopqrstuv",
                },
                {"id": "liveliveliv", "title": "Radio 24/7", "is_live": True},
                {"title": "no id"},
            ]
        }
    )
    results = run(resolver.search("  some   song ", limit=4))
    assert backend.calls[0][0] == "ytsearch4:some song"
    assert backend.calls[0][1]["extract_flat"] == "in_playlist"
    assert [r["title"] for r in results] == ["Song A", "Song B"]
    assert results[0] == {
        "title": "Song A",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "uploader": "Artist",
        "duration": 201,
        "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg",
    }
    assert results[1]["uploader"] == "Chan" and results[1]["duration"] == 0


def test_search_ignores_links_and_empty_text() -> None:
    resolver, backend = resolver_with({"entries": []})
    assert run(resolver.search("https://www.youtube.com/watch?v=abcdefghijk")) == []
    assert run(resolver.search("   ")) == []
    assert backend.calls == []


def test_youtube_thumbnail_prefers_what_the_entry_has() -> None:
    assert (
        youtube_thumbnail({"thumbnail": "https://i.ytimg.com/x.jpg", "id": "abcdefghijk"})
        == "https://i.ytimg.com/x.jpg"
    )
    assert youtube_thumbnail({"id": "abcdefghijk"}) == "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg"
    assert youtube_thumbnail({"id": "short"}) is None
    assert youtube_thumbnail({"thumbnail": "http://insecure/x.jpg"}) is None


def test_search_songs_answers_the_dashboard_in_every_case(settings: Any) -> None:
    runtime = BotRuntime(settings)
    assert run(runtime.search_songs("   "))["ok"] is False
    assert "starting" in run(runtime.search_songs("x"))["error"]

    class Found:
        async def search(self, query: str) -> list[dict[str, Any]]:
            return [{"title": "T", "url": "u", "uploader": "", "duration": 1, "thumbnail": None}]

    class Nothing:
        async def search(self, query: str) -> list[dict[str, Any]]:
            return []

    class Broken:
        async def search(self, query: str) -> list[dict[str, Any]]:
            raise RuntimeError("network down")

    runtime._resolver = Found()  # type: ignore[assignment]
    assert run(runtime.search_songs("x")) == {
        "ok": True,
        "results": [{"title": "T", "url": "u", "uploader": "", "duration": 1, "thumbnail": None}],
    }
    runtime._resolver = Nothing()  # type: ignore[assignment]
    assert run(runtime.search_songs("x")) == {"ok": False, "error": "No results for that."}
    runtime._resolver = Broken()  # type: ignore[assignment]
    assert "Couldn't search" in run(runtime.search_songs("x"))["error"]


async def _no_resolve(query: str, requester_id: int) -> Track | None:
    return None


def test_queue_keeps_thumbnails_through_a_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = JsonStore(tmp_path / "queue.json")
        first = RadioPlayer(resolver=_no_resolve, audio_bitrate_kbps=96)
        first.set_queue_store(store)
        first.enqueue(
            QueuedRequest(
                webpage_url="https://youtu.be/abcdefghijk",
                requester_id=7,
                requester_name="v",
                title="T",
                thumbnail_url="https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg",
            )
        )
        await asyncio.sleep(0.1)  # persisted in the background
        second = RadioPlayer(resolver=_no_resolve, audio_bitrate_kbps=96)
        second.set_queue_store(store)
        assert await second.restore_queue() == 1
        assert (
            second.audible_view().queue[0].thumbnail_url == "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg"
        )

    run(scenario())


def test_radio_picks_carry_a_thumbnail() -> None:
    request = RadioSuggester._as_request({"id": "abcdefghijk", "title": "T"})
    assert request.thumbnail_url == "https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg"


def test_a_song_not_yet_audible_keeps_its_picture_in_the_queue() -> None:
    from twitch_radio.player import NowPlaying

    async def scenario() -> None:
        player = RadioPlayer(resolver=_no_resolve, audio_bitrate_kbps=96, overlay_delay_seconds=5)
        player._set_now_playing(
            NowPlaying(
                title="A",
                uploader="u",
                thumbnail_url="https://i.ytimg.com/a.jpg",
                requester_name="v",
                requester_id=7,
                webpage_url="https://youtu.be/a",
                started_at=0.0,
                duration=10,
            )
        )
        assert player.audible_view().queue[0].thumbnail_url == "https://i.ytimg.com/a.jpg"

    run(scenario())


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    from twitch_radio import config

    for name, value in {
        "TWITCH_CLIENT_ID": "id",
        "TWITCH_CLIENT_SECRET": "secret",
        "TWITCH_BOT_ID": "1",
        "TWITCH_OWNER_ID": "2",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(config, "reload_env", lambda: None)
    return config.load_settings()
