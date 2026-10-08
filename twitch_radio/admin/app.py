"""Assembles and starts the aiohttp application. `run_admin_server` is the
only public entry point; everything it serves lives in handlers/.

The server is local only: it binds to 127.0.0.1, and a middleware rejects any
request whose Host header isn't a loopback name (DNS-rebinding defence)."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable

import aiohttp
from aiohttp import web

from twitch_radio.admin.assets import preload_static, read_logo
from twitch_radio.admin.context import CTX_KEY, AdminContext
from twitch_radio.admin.handlers.auth import origin_ok
from twitch_radio.admin.handlers import live, media
from twitch_radio.admin.handlers import settings as settings_handlers
from twitch_radio.admin.security import host_allowed
from twitch_radio.player import RadioPlayer
from twitch_radio.store import JsonStore

log = logging.getLogger(__name__)

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

_SHUTDOWN_GRACE_SECONDS = 2.0

# The only address the server ever listens on.
BIND_HOST = "127.0.0.1"

# Routes a web page open in the streamer's browser could otherwise use from
# another site: the live-state socket (queue and requester names) and the audio
# stream (a connection starts the Opus encoder, which costs CPU). OBS and curl
# send no Origin / Sec-Fetch-Site, so they pass; a browser always labels a
# cross-site request, and origin_ok() refuses it.
_SAME_ORIGIN_ONLY = frozenset({"/ws/nowplaying", "/stream.opus"})

_ROUTES: tuple[tuple[str, str, _Handler], ...] = (
    ("GET", "/nowplaying.json", live.handle_nowplaying),
    ("GET", "/healthz", live.handle_healthz),
    ("GET", "/ws/nowplaying", live.handle_ws_nowplaying),
    ("GET", "/overlay", live.handle_overlay),
    ("GET", "/player", live.handle_player),
    ("GET", "/logo.png", live.handle_logo),
    ("GET", "/thumb-proxy", media.handle_thumb_proxy),
    ("GET", "/stream.opus", media.handle_stream),
    ("GET", "/settings", settings_handlers.handle_settings_get),
    ("POST", "/settings", settings_handlers.handle_settings_post),
)


def _host_guard(port: int) -> Callable[..., Awaitable[web.StreamResponse]]:
    @web.middleware
    async def middleware(request: web.Request, handler: _Handler) -> web.StreamResponse:
        if not host_allowed(request.headers.get("Host"), port):
            # 421 Misdirected Request: the request reached us under a name we
            # don't serve. A browser tab on another site (DNS rebinding) lands here.
            return web.Response(status=421, text="This server only answers on 127.0.0.1 / localhost.")
        if request.path in _SAME_ORIGIN_ONLY and not origin_ok(request):
            return web.Response(status=403, text="Cross-site requests are not allowed here.")
        response = await handler(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    return middleware


async def run_admin_server(
    *,
    player: RadioPlayer,
    tunables_store: JsonStore,
    toggles_store: JsonStore,
    broadcast_info: dict[str, str],
    port: int,
) -> web.AppRunner:
    preload_static()

    thumb_session = aiohttp.ClientSession()
    logo = read_logo("logo-96.png")
    ctx = AdminContext(
        player=player,
        tunables_store=tunables_store,
        toggles_store=toggles_store,
        broadcast_info=broadcast_info,
        thumb_session=thumb_session,
        logo=logo,
        logo_small=read_logo("logo-32.png"),
    )

    app = web.Application(middlewares=[_host_guard(port)])
    app[CTX_KEY] = ctx
    for method, path, handler in _ROUTES:
        # add_get (not add_route) so HEAD requests keep working on GET routes.
        (app.router.add_get if method == "GET" else app.router.add_post)(path, handler)

    # Ties the thumbnail session's lifetime to the app's: runner.cleanup()
    # (already called in bot.py's shutdown path) fires this, so no separate
    # close() call is needed anywhere else.
    async def _close_thumb_session(_app: web.Application) -> None:
        await thumb_session.close()

    app.on_cleanup.append(_close_thumb_session)

    # aiohttp's default is a 60 s grace period for in-flight handlers on
    # cleanup(). /stream.opus and the overlay WebSocket never finish on their
    # own, so with OBS connected a plain Stop sat there for a full minute.
    # The player ends the audio streams itself (close_subscribers); anything
    # still attached after this short grace is cancelled.
    runner = web.AppRunner(app, shutdown_timeout=_SHUTDOWN_GRACE_SECONDS)
    try:
        await runner.setup()
        await web.TCPSite(runner, BIND_HOST, port).start()
    except BaseException:
        # e.g. the port is already taken: release what was acquired instead of
        # leaking an open client session, without masking the original error.
        with contextlib.suppress(Exception):
            await runner.cleanup()
        await thumb_session.close()
        raise
    log.info(
        "Local server listening on http://%s:%d (/stream.opus, /overlay, /player, "
        "/nowplaying.json, /ws/nowplaying, /healthz, /logo.png, /settings)",
        BIND_HOST,
        port,
    )
    return runner
