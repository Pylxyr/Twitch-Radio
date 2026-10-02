import dataclasses

import pytest

from twitch_radio.tunables import TUNABLE_BOUNDS, TUNABLE_LABELS, TwitchTunables


def test_defaults_are_inside_their_bounds() -> None:
    defaults = TwitchTunables().to_dict()
    for name, (low, high) in TUNABLE_BOUNDS.items():
        assert low <= defaults[name] <= high, name


def test_bounds_labels_and_fields_agree() -> None:
    fields = {f.name for f in dataclasses.fields(TwitchTunables)}
    assert set(TUNABLE_BOUNDS) == fields == set(TUNABLE_LABELS)


@pytest.mark.parametrize("name", sorted(TUNABLE_BOUNDS))
def test_out_of_range_values_are_clamped(name: str) -> None:
    low, high = TUNABLE_BOUNDS[name]
    assert getattr(TwitchTunables.from_dict({name: low - 1000}), name) == low
    assert getattr(TwitchTunables.from_dict({name: high + 1000}), name) == high
    assert getattr(TwitchTunables.from_dict({name: low}), name) == low
    assert getattr(TwitchTunables.from_dict({name: high}), name) == high


def test_garbage_falls_back_per_field() -> None:
    result = TwitchTunables.from_dict(
        {"queue_cap": "lots", "vote_skip_threshold": None, "request_cooldown_seconds": "30"}
    )
    defaults = TwitchTunables()
    assert result.queue_cap == defaults.queue_cap
    assert result.vote_skip_threshold == defaults.vote_skip_threshold
    assert result.request_cooldown_seconds == 30


def test_round_trip() -> None:
    original = TwitchTunables(queue_cap=7, request_cooldown_seconds=12)
    assert TwitchTunables.from_dict(original.to_dict()) == original
