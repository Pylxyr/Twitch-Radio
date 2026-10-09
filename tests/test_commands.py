"""The chat command surface: exactly !sr, !skip, !sq (!songqueue) and !radio,
and who may use them."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from twitchio import Chatter
from twitchio.ext import commands

from twitch_radio.components.song_requests import SongRequestComponent
from twitch_radio.player import SKIP_NOT_READY_MESSAGE, SkipResult
from twitch_radio.store import JsonStore
from twitch_radio.toggles import FeatureToggles


def _chatter(user_id: int = 100, *, moderator: bool = False, broadcaster: bool = False) -> MagicMock:
    chatter = MagicMock(spec=Chatter)
    chatter.id = str(user_id)
    chatter.moderator = moderator
    chatter.broadcaster = broadcaster
    chatter.display_name = f"user{user_id}"
    chatter.name = f"user{user_id}"
    return chatter


def _make(tmp_path: Path, *, active_requester_id: int | None = None, queued: list | None = None):
    player = MagicMock()
    player.active_requester_id = active_requester_id
    player.skip = AsyncMock(
        return_value=SkipResult.SKIPPED if active_requester_id is not None else SkipResult.NOTHING_PLAYING
    )
    player.queued_items.return_value = queued or []
    player.apply_radio_lookahead = AsyncMock()
    bot = SimpleNamespace(
        player=player,
        safe_reply=AsyncMock(),
        toggles_store=JsonStore(tmp_path / "toggles.json"),
    )
    return SongRequestComponent(bot), bot  # type: ignore[arg-type]


def _ctx(chatter: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.chatter = chatter
    return ctx


def _reply_text(bot) -> str:
    return str(bot.safe_reply.await_args.args[1])


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=30))  # type: ignore[arg-type]


def _commands() -> list[commands.Command]:
    return [v for v in vars(SongRequestComponent).values() if isinstance(v, commands.Command)]


def test_only_the_wanted_commands_exist() -> None:
    names = {name for command in _commands() for name in (command.name, *command.aliases)}
    assert names == {"sr", "skip", "sq", "songqueue", "radio"}
    assert "queue" not in names  # another bot in the channel owns !queue


# -- !skip ---------------------------------------------------------------------


def test_requester_can_skip_their_own_song(tmp_path: Path) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path, active_requester_id=100)
        await component.skip.callback(component, _ctx(_chatter(100)))
        bot.player.skip.assert_awaited_once()
        assert "Skipped" in _reply_text(bot)

    run(go())


def test_other_viewer_cannot_skip(tmp_path: Path) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path, active_requester_id=100)
        await component.skip.callback(component, _ctx(_chatter(200)))
        bot.player.skip.assert_not_awaited()
        assert "only skip your own" in _reply_text(bot)

    run(go())


@pytest.mark.parametrize("role", ["moderator", "broadcaster"])
def test_mod_and_broadcaster_can_skip_anything(tmp_path: Path, role: str) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path, active_requester_id=100)
        await component.skip.callback(component, _ctx(_chatter(200, **{role: True})))
        bot.player.skip.assert_awaited_once()
        assert "Skipped" in _reply_text(bot)

    run(go())


def test_skip_with_nothing_playing(tmp_path: Path) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path, active_requester_id=None)
        await component.skip.callback(component, _ctx(_chatter(100)))
        bot.player.skip.assert_not_awaited()
        assert "Nothing's playing" in _reply_text(bot)

    run(go())


def test_a_refused_skip_tells_the_viewer_the_song_keeps_playing(tmp_path: Path) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path, active_requester_id=100)
        bot.player.skip.return_value = SkipResult.NOT_READY
        await component.skip.callback(component, _ctx(_chatter(100)))
        assert _reply_text(bot) == SKIP_NOT_READY_MESSAGE
        assert "Skipped" not in _reply_text(bot)

    run(go())


# -- !sq / !songqueue ----------------------------------------------------------


def test_queue_command_empty_and_filled(tmp_path: Path) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path)
        await component.queue_cmd.callback(component, _ctx(_chatter()))
        assert "empty" in _reply_text(bot)

        items = [SimpleNamespace(title=f"Song {i}") for i in range(5)]
        component, bot = _make(tmp_path, queued=items)
        await component.queue_cmd.callback(component, _ctx(_chatter()))
        text = _reply_text(bot)
        assert "5 queued" in text and "Song 0" in text and "+2 more" in text

    run(go())


# -- !radio --------------------------------------------------------------------


async def _radio_enabled(bot) -> bool:
    return FeatureToggles.from_dict(await bot.toggles_store.read()).radio_autoplay_enabled


def test_radio_status_is_visible_to_everyone(tmp_path: Path) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path)
        await component.radio_toggle.callback(component, _ctx(_chatter()), arg="")
        assert "Radio autoplay is on" in _reply_text(bot)

    run(go())


@pytest.mark.parametrize("arg", ["on", "off"])
def test_only_the_broadcaster_can_change_radio(tmp_path: Path, arg: str) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path)
        before = await _radio_enabled(bot)
        for who in (_chatter(1), _chatter(2, moderator=True)):
            await component.radio_toggle.callback(component, _ctx(who), arg=arg)
            assert "Only the broadcaster" in _reply_text(bot)
            assert await _radio_enabled(bot) == before

    run(go())


def test_broadcaster_can_turn_radio_on_and_off(tmp_path: Path) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path)
        boss = _chatter(1, broadcaster=True)
        await component.radio_toggle.callback(component, _ctx(boss), arg="off")
        assert await _radio_enabled(bot) is False
        assert "now off" in _reply_text(bot)
        await component.radio_toggle.callback(component, _ctx(boss), arg="on")
        assert await _radio_enabled(bot) is True

    run(go())


def test_radio_rejects_unknown_argument(tmp_path: Path) -> None:
    async def go() -> None:
        component, bot = _make(tmp_path)
        await component.radio_toggle.callback(component, _ctx(_chatter(1, broadcaster=True)), arg="maybe")
        assert "Usage: !radio" in _reply_text(bot)
        assert await _radio_enabled(bot) is True

    run(go())
