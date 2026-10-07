"""The radio-mix lookahead setting: keep the next few songs of the radio mix queued.

Stored in its own small file (radio_lookahead.json), deliberately apart from tunables.json and
toggles.json: those are what chat commands and the browser /settings page can reach, and this one
is changed from the desktop dashboard only. The dashboard writes the file and tells the running
bot to re-read it; the bot never writes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

LOOKAHEAD_MIN = 1
LOOKAHEAD_MAX = 15
LOOKAHEAD_DEFAULT = 5
LOOKAHEAD_DEFAULT_ENABLED = True


def clamp_count(value: Any, default: int = LOOKAHEAD_DEFAULT) -> int:
    """An int in [LOOKAHEAD_MIN, LOOKAHEAD_MAX]; anything unusable falls back to `default`."""
    if isinstance(value, bool):  # True is an int in Python, but not a count
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(LOOKAHEAD_MIN, min(LOOKAHEAD_MAX, number))


@dataclass(slots=True, frozen=True)
class RadioLookahead:
    enabled: bool = LOOKAHEAD_DEFAULT_ENABLED
    count: int = LOOKAHEAD_DEFAULT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RadioLookahead:
        enabled = data.get("enabled")
        return cls(
            enabled=enabled if isinstance(enabled, bool) else LOOKAHEAD_DEFAULT_ENABLED,
            count=clamp_count(data.get("count")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "count": self.count}
