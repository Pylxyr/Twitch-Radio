"""Read-only, unauthenticated routes: live now-playing data (JSON and
WebSocket), the health check, the OBS overlay page, and the logo. All of it
is data the streamer already shows on stream, which is why none of it is
gated."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from aiohttp import web

from twitch_radio.admin.assets import static_text
from twitch_radio.admin.context import AdminContext, get_ctx
from twitch_radio.telemetry import counters

_WS_HEARTBEAT_SECONDS = 30


def nowplaying_payload(ctx: AdminContext) -> dict[str, Any]:
    np = ctx.player.now_playing
    queue = [
        {"title": item.title or "Unknown title", "requester_name": item.requester_name}
        for item in ctx.player.queued_items()
    ]
    # "state" drives the /settings page's realtime chips (idle / resolving /
    # playing / paused); the overlay ignores fields it doesn't recognise.
    state = ctx.player.state.value
    if np is None:
        return {"playing": False, "state": state, "queue_size": len(queue), "queue": queue}
    return {
        "playing": True,
        "state": state,
        "title": np.title,
        "uploader": np.uploader,
        "thumbnail_url": np.thumbnail_url,
        "requester_name": np.requester_name,
        "webpage_url": np.webpage_url,
        "elapsed_seconds": max(0.0, time.monotonic() - np.started_at),
        "duration_seconds": np.duration,
        "queue_size": len(queue),
        "queue": queue,
    }


async def handle_nowplaying(request: web.Request) -> web.Response:
    return web.json_response(nowplaying_payload(get_ctx(request)))


async def handle_healthz(request: web.Request) -> web.Response:
    """Unauthenticated on purpose (same exposure as /nowplaying.json;
    nothing here is sensitive) — for an uptime monitor, or a quick health
    check without opening /settings. Resolve counts are an in-memory
    rolling window (telemetry.py) that resets on restart, not a
    persisted log."""
    ctx = get_ctx(request)
    return web.json_response(
        {
            "uptime_seconds": round(time.monotonic() - ctx.started_at, 1),
            "player_state": ctx.player.state.value,
            "queue_size": ctx.player.queue_size(),
            "resolves_last_hour": {
                "success": counters.count_last_hour("resolve_success"),
                "failure": counters.count_last_hour("resolve_failure"),
            },
        }
    )


async def _push_ws(
    request: web.Request,
    *,
    payload: Callable[[], Any],
    subscribe: Callable[[], asyncio.Queue[None]],
    unsubscribe: Callable[[asyncio.Queue[None]], None],
) -> web.WebSocketResponse:
    """One snapshot on connect, then another whenever the source signals a
    change on its state queue. The queue wait times out every 30s purely so a
    dead connection is noticed via ws.closed even when nothing is changing."""
    ws = web.WebSocketResponse(heartbeat=_WS_HEARTBEAT_SECONDS)
    await ws.prepare(request)
    state_queue = subscribe()
    try:
        await ws.send_json(payload())
        while True:
            try:
                await asyncio.wait_for(state_queue.get(), timeout=_WS_HEARTBEAT_SECONDS)
            except TimeoutError:
                pass
            if ws.closed:
                break
            await ws.send_json(payload())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        unsubscribe(state_queue)
    return ws


async def handle_ws_nowplaying(request: web.Request) -> web.WebSocketResponse:
    """Push counterpart to /nowplaying.json — the overlay prefers it and falls
    back to polling. The client ticks elapsed time between pushes itself, so
    nothing needs to be sent every second."""
    ctx = get_ctx(request)
    return await _push_ws(
        request,
        payload=lambda: nowplaying_payload(ctx),
        subscribe=ctx.player.subscribe_state,
        unsubscribe=ctx.player.unsubscribe_state,
    )


async def handle_overlay(request: web.Request) -> web.Response:
    return web.Response(text=static_text("overlay.html"), content_type="text/html")


async def handle_logo(request: web.Request) -> web.Response:
    """The bot mark, for the settings page and its favicon. Public like the
    rest of the overlay surface — a static image with nothing
    deployment-specific in it — and served from memory."""
    ctx = get_ctx(request)
    body = ctx.logo_small if request.query.get("s") == "32" else ctx.logo
    if body is None:
        return web.Response(status=404, text="No logo asset installed")
    return web.Response(body=body, content_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
