"""Chooses which yt-dlp the core imports: the bundled one, or a newer one the
user installed under <home>/yt-dlp/current (see ytdlp_update.py).

Stdlib only, and importing this module never imports yt_dlp: the one-shot CLI
modes need the version but not the import cost. extraction.py and
extractor_worker.py call import_with_fallback() before their own
`import yt_dlp`, so both the core and every worker process make the same choice.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from twitch_radio.paths import home_dir, is_frozen, resource_dir

OVERRIDE_DIRNAME = "yt-dlp"
CACHE_DIRNAME = "yt-dlp-cache"
STATE_FILE = "state.json"
BUNDLED_VERSION_FILE = "ytdlp_bundled_version.txt"

_VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}(?:\.\d+)?$")
_VERSION_LINE = re.compile(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE)

_status: dict[str, Any] = {"done": False, "source": "bundled", "error": None}


def override_root() -> Path:
    return home_dir() / OVERRIDE_DIRNAME


def current_dir() -> Path:
    return override_root() / "current"


def previous_dir() -> Path:
    return override_root() / "previous"


def staging_dir() -> Path:
    return override_root() / "staging"


def parse_version(text: str) -> tuple[int, ...]:
    """'2026.08.19' and '2026.8.19.123456' -> comparable int tuples. Raises
    ValueError for anything that isn't a yt-dlp release version."""
    text = text.strip()
    if not _VERSION_RE.match(text):
        raise ValueError(f"not a yt-dlp version: {text!r}")
    return tuple(int(part) for part in text.split("."))


def compare_versions(a: str, b: str) -> int:
    left, right = parse_version(a), parse_version(b)
    return (left > right) - (left < right)


def read_state() -> dict[str, Any]:
    try:
        loaded = json.loads((override_root() / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(state: dict[str, Any]) -> None:
    root = override_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{STATE_FILE}.tmp"
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, root / STATE_FILE)


def version_in_dir(directory: Path) -> str | None:
    """Reads __version__ from <directory>/yt_dlp/version.py without importing it."""
    try:
        text = (directory / "yt_dlp" / "version.py").read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION_LINE.search(text)
    return match.group(1) if match else None


def bundled_version() -> str | None:
    if is_frozen():
        try:
            return (resource_dir() / BUNDLED_VERSION_FILE).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    try:
        from importlib import metadata

        return metadata.version("yt-dlp")
    except Exception:  # noqa: BLE001 - metadata missing is not an error
        return None


def plan() -> dict[str, Any]:
    """Which copy would be used, decided without importing anything."""
    bundled = bundled_version()
    info: dict[str, Any] = {"source": "bundled", "version": bundled, "bundled": bundled, "reason": None}
    if os.environ.get("TWITCH_RADIO_NO_YTDLP_OVERRIDE"):
        info["reason"] = "disabled by TWITCH_RADIO_NO_YTDLP_OVERRIDE"
        return info
    version = version_in_dir(current_dir())
    if version is None:
        return info
    try:
        newer = bundled is None or compare_versions(version, bundled) > 0
    except ValueError:
        info["reason"] = f"installed override has an unreadable version ({version!r})"
        return info
    if not newer:
        info["reason"] = f"override {version} is not newer than the bundled {bundled}"
        return info
    if read_state().get("failed_version") == version:
        info["reason"] = "override failed to load earlier"
        return info
    info.update(source="override", version=version)
    return info


def purge_yt_dlp_modules() -> None:
    for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        del sys.modules[name]


def import_with_fallback() -> dict[str, Any]:
    """Imports yt_dlp from the override if one qualifies, else (or if that
    import fails) from the bundled copy. Idempotent; never raises because of
    the override."""
    if _status["done"]:
        return dict(_status)
    _status["done"] = True
    chosen = plan()
    _status.update(source="bundled", error=None, reason=chosen["reason"])
    if chosen["source"] == "override":
        directory = str(current_dir())
        sys.path.insert(0, directory)
        try:
            import yt_dlp

            origin = Path(yt_dlp.__file__ or "").resolve()
            if not origin.is_relative_to(current_dir().resolve()):
                raise ImportError(f"yt_dlp was imported from {origin}, not from the override")
            _status["source"] = "override"
            return dict(_status)
        except Exception as exc:  # noqa: BLE001 - any failure means: use the bundled copy
            with contextlib.suppress(ValueError):
                sys.path.remove(directory)
            purge_yt_dlp_modules()
            _status["error"] = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(OSError):
                write_state(
                    {**read_state(), "failed_version": chosen["version"], "failed_error": _status["error"]}
                )
    import yt_dlp  # noqa: F401 - the bundled copy

    return dict(_status)


def describe() -> dict[str, Any]:
    """For the preflight report; cheap, no import."""
    chosen = plan()
    state = read_state()
    previous = version_in_dir(previous_dir())
    return {
        "version": chosen["version"],
        "source": chosen["source"],
        "bundled": chosen["bundled"],
        "previous": previous,
        "note": chosen["reason"],
        "override_error": state.get("failed_error") if state.get("failed_version") else None,
    }


def solver_status(cache_dir: Path) -> dict[str, Any]:
    """Whether yt-dlp has downloaded and cached the JS challenge solver's lib
    script (the part that is not vendored in yt-dlp). Written by yt-dlp itself
    as <cachedir>/challenge-solver/lib.json after a successful download."""
    path = cache_dir / "challenge-solver" / "lib.json"
    ready = False
    version = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ready = isinstance(data, dict) and bool(data.get("code"))
        version = data.get("version") if isinstance(data, dict) else None
    except (OSError, ValueError):
        pass
    return {"ready": ready, "version": version, "path": str(path)}
