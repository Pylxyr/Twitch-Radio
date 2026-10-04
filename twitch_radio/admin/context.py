"""The shared state every admin handler reads.

Handlers are plain functions rather than methods on one large class; the state
they share lives here, is built once by run_admin_server(), and is fetched from
the aiohttp app with get_ctx(request).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import web

from twitch_radio.admin.security import RequestRateLimiter

if TYPE_CHECKING:
    import aiohttp

    from twitch_radio.player import RadioPlayer
    from twitch_radio.store import JsonStore


@dataclass(slots=True)
class AdminContext:
    player: RadioPlayer
    tunables_store: JsonStore
    toggles_store: JsonStore
    broadcast_info: dict[str, str]
    # Reused across every /thumb-proxy request rather than opening a fresh
    # connection per fetch; owned (created and closed) by run_admin_server().
    thumb_session: aiohttp.ClientSession
    # Read once at startup; None just means the pages render without a mark.
    logo: bytes | None
    logo_small: bytes | None
    started_at: float = field(default_factory=time.monotonic)
    # The overlay fetches one thumbnail per track change; this only stops a
    # misbehaving page using the proxy as a free download relay.
    thumb_limiter: RequestRateLimiter = field(default_factory=lambda: RequestRateLimiter(120, 60.0))


CTX_KEY = web.AppKey("admin_ctx", AdminContext)


def get_ctx(request: web.Request) -> AdminContext:
    return request.app[CTX_KEY]
