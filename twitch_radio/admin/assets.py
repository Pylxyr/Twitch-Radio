"""Front-end files and the logo, read from disk once and cached.

The overlay pages, CSS, JS and HTML templates live as real files under
static/ (rather than as multi-hundred-line string constants in Python), so
they get proper editor support and the Python side only deals with logic.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path
from string import Template

from twitch_radio.config import BASE_DIR

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
# Served at /logo.png (96px) and /logo.png?s=32 (favicon). A missing asset
# degrades to "no logo", never an error — it's decoration, and a checkout that
# skipped the assets directory should still get working pages.
LOGO_DIR = BASE_DIR / "assets"

STATIC_FILES = (
    "overlay.html",
    "settings.html",
    "settings.css",
    "settings.js",
    "login.html",
    "login.css",
    "login.js",
)


@functools.cache
def static_text(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


@functools.cache
def template(name: str) -> Template:
    """A static HTML file whose ${placeholders} are filled by render/."""
    return Template(static_text(name))


def preload_static() -> None:
    """Read every static file now so a broken checkout fails at startup with
    a clear error, not on the first request for the page that needs it."""
    missing = [name for name in STATIC_FILES if not (STATIC_DIR / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Admin server static files missing from {STATIC_DIR}: {', '.join(missing)}")
    for name in STATIC_FILES:
        static_text(name)


def read_logo(name: str) -> bytes | None:
    try:
        return (LOGO_DIR / name).read_bytes()
    except OSError:
        log.debug("Logo asset %s not available — pages will render without it.", name)
        return None
