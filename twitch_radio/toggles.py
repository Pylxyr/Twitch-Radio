from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Same "single source of truth" idea as tunables.TUNABLE_BOUNDS — shared by
# /settings and !toggle so both stay in sync on valid keys.
TOGGLE_KEYS: dict[str, str] = {
    "radio_autoplay_enabled": "Auto-queue a similar track when the queue runs dry",
}


@dataclass(slots=True)
class FeatureToggles:
    # On by default: this is the specific gap ("radio silence") this
    # feature exists to close.
    radio_autoplay_enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeatureToggles":
        defaults = cls()

        def _field(name: str, default: bool) -> bool:
            if name not in data:
                return default
            value = data[name]
            return value if isinstance(value, bool) else default

        return cls(
            radio_autoplay_enabled=_field("radio_autoplay_enabled", defaults.radio_autoplay_enabled),
        )

    def to_dict(self) -> dict[str, bool]:
        return {
            "radio_autoplay_enabled": self.radio_autoplay_enabled,
        }
