"""Radio-mix lookahead (the next few songs of the mix stay queued), songs requested from the
dashboard, and the control-channel commands that carry both."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from twitch_radio.extraction import UnsupportedSourceError
from twitch_radio.headless import build_dispatcher
from twitch_radio.lookahead import LOOKAHEAD_MAX, LOOKAHEAD_MIN, RadioLookahead, clamp_count
from twitch_radio.models import Track
from twitch_radio.player import QueuedRequest, RadioPlayer
from twitch_radio.radio import RadioSuggester
from twitch_radio.service import BotRuntime
from twitch_radio.store import JsonStore


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=20))  # type: ignore[arg-type]


def vid(n: int) -> str:
    return f"vid{n:08d}"[:11]  # YouTube ids are 11 characters


def url(n: int) -> str:
    return f"https://www.youtube.com/watch?v={vid(n)}"


def entry(n: int) -> dict[str, Any]:
    return {"id": vid(n), "title": f"Song {n}", "uploader": "Artist", "url": url(n)}


# -- the setting ---------------------------------------------------------------------------------


def test_setting_is_clamped_and_forgiving() -> None:
    assert (LOOKAHEAD_MIN, LOOKAHEAD_MAX) == (1, 15)
    assert clamp_count(0) == 1 and clamp_count(99) == 15 and clamp_count("7") == 7
    assert clamp_count(None) == 5 and clamp_count("many") == 5 and clamp_count(True) == 5
    assert RadioLookahead.from_dict({}) == RadioLookahead(enabled=True, count=5)
    assert RadioLookahead.from_dict({"enabled": False, "count": 12}) == RadioLookahead(False, 12)
    assert RadioLookahead.from_dict({"enabled": "no", "count": 40}) == RadioLookahead(True, 15)
    assert RadioLookahead(True, 3).to_dict() == {"enabled": True, "count": 3}


# -- RadioSuggester.suggest_many ---------------------------------------------------------------------


class FakeResolver:
    """Stands in for extraction.Resolver: radio mixes come from a table keyed by seed video id."""

    def __init__(self, mixes: dict[str, list[dict[str, Any]]]) -> None:
        self.mixes = mixes
        self.lookups: list[str] = []
        self.limits: list[int] = []

    async def resolve_radio_mix(self, mix_url: str, limit: int = 15) -> list[dict[str, Any]]:
        seed = mix_url.split("v=")[1].split("&")[0]
        self.lookups.append(seed)
        self.limits.append(limit)
        if seed not in self.mixes:
            raise RuntimeError("no such mix")
        return self.mixes[seed]


def test_suggest_many_follows_the_mix_in_order_and_skips_the_seed_and_exclusions() -> None:
    resolver = FakeResolver({vid(1): [entry(n) for n in (1, 2, 3, 4, 5, 6)]})
    suggester = RadioSuggester(resolver)  # type: ignore[arg-type]
    picks = run(suggester.suggest_many(url(1), 3, exclude_ids={vid(3)}))
    assert [p.title for p in picks] == ["Song 2", "Song 4", "Song 5"]
    assert all(p.requester_id == 0 for p in picks)  # radio filler, never a real viewer
    assert picks[0].webpage_url == url(2)


def test_suggest_many_reuses_the_mix_instead_of_asking_youtube_again() -> None:
    resolver = FakeResolver({vid(1): [entry(n) for n in range(1, 9)]})
    suggester = RadioSuggester(resolver)  # type: ignore[arg-type]
    first = run(suggester.suggest_many(url(1), 2, exclude_ids=set()))
    # The queue now ends with the last pick; the next top-up follows from there.
    second = run(suggester.suggest_many(first[-1].webpage_url, 2, exclude_ids={vid(2), vid(3)}))
    assert [p.title for p in first] == ["Song 2", "Song 3"]
    assert [p.title for p in second] == ["Song 4", "Song 5"]
    assert resolver.lookups == [vid(1)]  # one lookup served both


def test_suggest_many_starts_a_new_mix_for_an_unrelated_seed_and_when_the_old_one_runs_dry() -> None:
    resolver = FakeResolver(
        {
            vid(1): [entry(n) for n in (1, 2, 3)],
            vid(50): [entry(n) for n in (50, 51, 52)],
            vid(3): [entry(n) for n in (3, 60, 61)],
        }
    )
    suggester = RadioSuggester(resolver)  # type: ignore[arg-type]
    run(suggester.suggest_many(url(1), 1, exclude_ids=set()))  # mix of 1
    unrelated = run(suggester.suggest_many(url(50), 2, exclude_ids=set()))
    assert [p.title for p in unrelated] == ["Song 51", "Song 52"]
    assert resolver.lookups == [vid(1), vid(50)]

    resolver.lookups.clear()
    suggester = RadioSuggester(resolver)  # type: ignore[arg-type]
    picks = run(suggester.suggest_many(url(1), 3, exclude_ids=set()))
    # The mix of 1 only has two usable songs; the rest continue from the last one picked.
    assert [p.title for p in picks] == ["Song 2", "Song 3", "Song 60"]
    assert resolver.lookups == [vid(1), vid(3)]


def test_suggest_many_never_repeats_what_was_just_picked_and_survives_a_failed_lookup() -> None:
    resolver = FakeResolver({vid(1): [entry(n) for n in (1, 2, 3)]})
    suggester = RadioSuggester(resolver)  # type: ignore[arg-type]
    run(suggester.suggest_many(url(1), 2, exclude_ids=set()))
    assert run(suggester.suggest_many(url(1), 2, exclude_ids=set())) == []  # all used up recently
    assert (
        run(RadioSuggester(resolver).suggest_many(url(99), 3, exclude_ids=set())) == []
    )  # lookup raised  # type: ignore[arg-type]
    assert run(RadioSuggester(resolver).suggest_many("not a youtube url", 3, exclude_ids=set())) == []  # type: ignore[arg-type]


# -- the player ------------------------------------------------------------------------------------------


async def no_resolve(query: str, requester_id: int) -> Track | None:  # pragma: no cover - never reached
    raise AssertionError("these tests never play anything")


class Harness:
    """A RadioPlayer that is never started (nothing plays): only its queue logic runs."""

    def __init__(self, mix: list[int], enabled: bool = True, count: int = 3, radio: bool = True) -> None:
        self.player = RadioPlayer(resolver=no_resolve, audio_bitrate_kbps=96)
        self.setting = RadioLookahead(enabled, count)
        self.radio = radio
        self.mix = list(mix)
        self.calls: list[tuple[str, int, set[str]]] = []
        self.used: set[int] = set()  # like RadioSuggester's own memory of what it picked

        async def suggest_many(seed: str, n: int, exclude: Collection[str]) -> list[QueuedRequest]:
            self.calls.append((seed, n, set(exclude)))
            picks = [m for m in self.mix if vid(m) not in exclude and url(m) != seed and m not in self.used][
                :n
            ]
            self.used.update(picks)
            return [QueuedRequest(url(m), 0, "Radio Mix", f"Song {m}") for m in picks]

        async def lookahead() -> tuple[bool, int]:
            return self.setting.enabled, self.setting.count

        async def radio_on() -> bool:
            return self.radio

        self.player.set_radio_lookahead(suggest_many, lookahead)
        self.player.set_radio_enabled_getter(radio_on)

    def titles(self) -> list[str]:
        return [r.title for r in self.player.queued_items()]

    async def settle(self) -> None:
        for _ in range(50):
            await asyncio.sleep(0.01)
            task = self.player._lookahead_task
            if task is None or task.done():
                await asyncio.sleep(0.01)
                if task is None or task.done():
                    return


def request(n: int, user: int = 7) -> QueuedRequest:
    return QueuedRequest(url(n), user, f"user{user}", f"Song {n}")


def test_one_request_brings_the_next_n_songs_of_the_mix_behind_it() -> None:
    async def scenario() -> None:
        h = Harness(mix=[10, 11, 12, 13, 14, 15], count=3)
        h.player.enqueue(request(1))
        await h.settle()
        assert h.titles() == ["Song 1", "Song 10", "Song 11", "Song 12"]
        assert h.player.real_queue_size() == 1 and h.player.queue_size() == 4
        # It followed the requested song, and told the suggester what is already queued.
        assert h.calls[0][0] == url(1) and h.calls[0][1] == 3 and vid(1) in h.calls[0][2]

    run(scenario())


def test_requests_still_jump_ahead_of_radio_songs_and_keep_their_order() -> None:
    async def scenario() -> None:
        h = Harness(mix=[10, 11, 12, 13], count=2)
        h.player.enqueue(request(1))
        await h.settle()
        h.player.enqueue(request(2, user=8))
        await h.settle()
        assert h.titles()[:2] == ["Song 1", "Song 2"]
        assert all(r.requester_id == 0 for r in h.player.queued_items()[2:])
        assert sum(1 for r in h.player.queued_items() if r.requester_id == 0) == 2  # still just n

    run(scenario())


def test_the_queue_is_topped_up_as_songs_start_and_continues_from_its_end() -> None:
    async def scenario() -> None:
        h = Harness(mix=[10, 11, 12, 13, 14, 15], count=3)
        h.player.enqueue(request(1))
        await h.settle()
        assert h.titles() == ["Song 1", "Song 10", "Song 11", "Song 12"]
        # Song 1 starts playing, then song 10 does: each leaves a gap the lookahead refills.
        h.player._pending.pop(0)
        h.player._pending.pop(0)
        h.player.request_radio_lookahead()
        await h.settle()
        assert [r.title for r in h.player.queued_items()] == ["Song 11", "Song 12", "Song 13"]
        assert h.calls[-1][0] == url(12)  # followed the end of the queue, not the song that played

    run(scenario())


def test_nothing_is_queued_when_lookahead_or_auto_radio_is_off() -> None:
    async def scenario() -> None:
        off = Harness(mix=[10, 11], enabled=False)
        off.player.enqueue(request(1))
        await off.settle()
        assert off.titles() == ["Song 1"] and off.calls == []

        no_radio = Harness(mix=[10, 11], radio=False)
        no_radio.player.enqueue(request(1))
        await no_radio.settle()
        assert no_radio.titles() == ["Song 1"] and no_radio.calls == []

    run(scenario())


def test_changing_the_setting_trims_or_clears_what_is_queued() -> None:
    async def scenario() -> None:
        h = Harness(mix=list(range(10, 20)), count=5)
        h.player.enqueue(request(1))
        await h.settle()
        assert h.player.queue_size() == 6

        h.setting = RadioLookahead(True, 2)  # lower the count: the far end goes
        await h.player.apply_radio_lookahead()
        await h.settle()
        assert h.titles() == ["Song 1", "Song 10", "Song 11"]

        h.setting = RadioLookahead(True, 4)  # raise it: topped up from the end
        await h.player.apply_radio_lookahead()
        await h.settle()
        # (songs that were trimmed away count as recently picked, so the top-up carries on past them)
        assert h.titles()[:3] == ["Song 1", "Song 10", "Song 11"]
        assert len(h.titles()) == 5 and len(set(h.titles())) == 5

        h.setting = RadioLookahead(False, 4)  # off: every radio song leaves, the request stays
        await h.player.apply_radio_lookahead()
        await h.settle()
        assert h.titles() == ["Song 1"]

        h.setting = RadioLookahead(True, 3)
        h.radio = False  # auto-radio itself off wins over lookahead on
        await h.player.apply_radio_lookahead()
        await h.settle()
        assert h.titles() == ["Song 1"]

    run(scenario())


def test_a_failed_mix_lookup_is_not_retried_in_a_loop() -> None:
    async def scenario() -> None:
        h = Harness(mix=[])
        h.player.enqueue(request(1))
        await h.settle()
        h.player.enqueue(request(2))
        await h.settle()
        assert len(h.calls) == 1  # the second request fell inside the back-off

    run(scenario())


def test_a_request_replaces_the_same_song_when_it_was_already_queued_as_radio() -> None:
    async def scenario() -> None:
        h = Harness(mix=[10, 11, 12], count=3)
        h.player.enqueue(request(1))
        await h.settle()
        assert not h.player.is_already_requested(url(11))  # radio filler is not a request
        h.player.enqueue(request(11, user=9))
        assert h.titles().count("Song 11") == 1
        real = [r for r in h.player.queued_items() if r.requester_id != 0]
        assert [r.title for r in real] == ["Song 1", "Song 11"]
        assert h.player.is_already_requested(url(11))
        assert h.player.is_already_requested("https://youtu.be/" + vid(11))  # any URL shape

    run(scenario())


# -- a song requested from the dashboard ---------------------------------------------------------------


class OneTrackResolver:
    def __init__(self, track: Track | None = None, error: Exception | None = None) -> None:
        self.track, self.error, self.queries = track, error, []

    async def resolve(self, query: str, requester_id: int) -> Track | None:
        self.queries.append((query, requester_id))
        if self.error:
            raise self.error
        return self.track


def make_runtime(resolver: OneTrackResolver) -> tuple[BotRuntime, RadioPlayer]:
    runtime = BotRuntime(SimpleNamespace(owner_id="4242"))  # type: ignore[arg-type]
    player = RadioPlayer(resolver=no_resolve, audio_bitrate_kbps=96)
    runtime._player = player
    runtime._resolver = resolver  # type: ignore[assignment]
    return runtime, player


def track(n: int, live: bool = False) -> Track:
    return Track(f"Song {n}", url(n), "http://x/stream", "Artist", 200, 4242, is_live=live)


def test_dashboard_request_queues_a_song_for_the_streamer() -> None:
    async def scenario() -> None:
        resolver = OneTrackResolver(track(5))
        runtime, player = make_runtime(resolver)
        result = await runtime.request_song("  daft   punk  ")
        assert result == {"ok": True, "title": "Song 5", "position": 1}
        assert resolver.queries == [("daft punk", 4242)]  # whitespace tidied, asked as the streamer
        item = player.queued_items()[0]
        assert (item.requester_id, item.requester_name, item.webpage_url) == (4242, "Streamer", url(5))

    run(scenario())


def test_dashboard_request_explains_every_refusal() -> None:
    async def scenario() -> None:
        runtime, player = make_runtime(OneTrackResolver(track(5)))
        assert (await runtime.request_song("   "))["ok"] is False
        assert "too long" in (await runtime.request_song("x" * 400))["error"]
        assert (await runtime.request_song("one"))["ok"] is True
        again = await runtime.request_song("one")
        assert again == {"ok": False, "error": "Song 5 is already queued."}
        assert player.queue_size() == 1

        for resolver, expected in (
            (OneTrackResolver(None), "No results for that."),
            (OneTrackResolver(track(6, live=True)), "Can't queue a livestream."),
            (OneTrackResolver(error=UnsupportedSourceError("Only YouTube links.")), "Only YouTube links."),
            (OneTrackResolver(error=RuntimeError("boom")), "Couldn't fetch that"),
        ):
            runtime, _ = make_runtime(resolver)
            result = await runtime.request_song("something")
            assert result["ok"] is False and expected in result["error"]

        bare = BotRuntime(SimpleNamespace(owner_id="4242"))  # type: ignore[arg-type]
        assert "still starting" in (await bare.request_song("x"))["error"]

    run(scenario())


# -- the control channel -------------------------------------------------------------------------------


class Sent:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


class FakeRuntime:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.applied = 0

    async def request_song(self, query: str) -> dict[str, Any]:
        self.requests.append(query)
        if query == "boom":
            raise RuntimeError("unexpected")
        return {"ok": True, "title": "Song 5", "position": 2}

    async def apply_radio_lookahead(self) -> None:
        self.applied += 1

    async def skip(self) -> dict[str, Any]:
        return {"ok": False, "result": "not_ready", "error": "The next song is still loading."}


def test_request_and_lookahead_commands_are_answered_when_they_finish() -> None:
    async def scenario() -> None:
        runtime, sent = FakeRuntime(), Sent()
        dispatch: Callable[[dict[str, Any]], None] = build_dispatcher(runtime, sent)  # type: ignore[arg-type]

        dispatch({"cmd": "request", "query": "daft punk", "id": 1})
        dispatch({"cmd": "request", "query": 42, "id": 2})  # not a string: treated as empty
        dispatch({"cmd": "request", "query": "boom", "id": 3})
        dispatch({"cmd": "radio_lookahead", "id": 4})
        dispatch({"cmd": "skip", "id": 5})
        dispatch({"cmd": "nope", "id": 6})
        assert [m["id"] for m in sent.messages] == [6]  # only the unknown command is answered at once
        await asyncio.sleep(0.05)
        by_id = {m["id"]: m for m in sent.messages}
        assert by_id[1] == {"t": "ack", "id": 1, "ok": True, "title": "Song 5", "position": 2}
        assert runtime.requests == ["daft punk", "", "boom"]
        assert by_id[3]["ok"] is False and "Logs" in by_id[3]["error"]  # a crash still gets an answer
        assert by_id[4]["ok"] is True and runtime.applied == 1
        assert by_id[6]["ok"] is False
        # a skip is answered when it finishes, and says why when it did not happen
        assert (
            by_id[5]["ok"] is False and by_id[5]["result"] == "not_ready" and "loading" in by_id[5]["error"]
        )

    run(scenario())


def test_lookahead_file_round_trips_and_defaults_when_missing(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "radio_lookahead.json")

    async def scenario() -> None:
        assert RadioLookahead.from_dict(await store.read()) == RadioLookahead()
        await store.write({"enabled": False, "count": 9})
        assert RadioLookahead.from_dict(await store.read()) == RadioLookahead(False, 9)

    run(scenario())


@pytest.mark.parametrize("raw", [{"count": [3]}, {"enabled": None, "count": {}}, {"count": float("nan")}])
def test_garbage_in_the_file_falls_back_to_defaults(raw: dict[str, Any]) -> None:
    assert RadioLookahead.from_dict(raw) == RadioLookahead()


def test_the_desktop_app_and_the_core_agree_on_the_limits() -> None:
    """gui/validate.js and twitch_radio/lookahead.py each clamp the setting; they must use the same numbers."""
    import re

    from twitch_radio.lookahead import LOOKAHEAD_DEFAULT, LOOKAHEAD_DEFAULT_ENABLED

    source = (Path(__file__).resolve().parent.parent / "gui" / "validate.js").read_text(encoding="utf-8")

    def js_constant(name: str) -> str:
        match = re.search(rf"const {name} = (\w+);", source)
        assert match, f"{name} not found in gui/validate.js"
        return match.group(1)

    assert int(js_constant("LOOKAHEAD_MIN")) == LOOKAHEAD_MIN
    assert int(js_constant("LOOKAHEAD_MAX")) == LOOKAHEAD_MAX
    assert int(js_constant("LOOKAHEAD_DEFAULT")) == LOOKAHEAD_DEFAULT
    assert (js_constant("LOOKAHEAD_DEFAULT_ENABLED") == "true") == LOOKAHEAD_DEFAULT_ENABLED


def test_a_big_lookahead_asks_youtube_for_enough_of_the_mix() -> None:
    resolver = FakeResolver({vid(1): [entry(n) for n in range(1, 40)]})
    suggester = RadioSuggester(resolver)  # type: ignore[arg-type]
    picks = run(suggester.suggest_many(url(1), 15, exclude_ids=set()))
    assert len(picks) == 15
    assert resolver.limits == [25]  # 15 wanted + 10 spare for the seed, queued and recent songs
    small = FakeResolver({vid(1): [entry(n) for n in range(1, 40)]})
    run(RadioSuggester(small).suggest_many(url(1), 2, exclude_ids=set()))  # type: ignore[arg-type]
    assert small.limits == [15]  # never fewer than the normal 15
