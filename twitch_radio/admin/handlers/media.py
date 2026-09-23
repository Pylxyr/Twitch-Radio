"""Routes that move bytes: the continuous MP3 stream and the thumbnail relay."""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from twitch_radio.admin.context import client_ip, get_ctx
from twitch_radio.admin.security import resolve_thumb_redirect, validate_thumb_url

_THUMB_HOP_TIMEOUT = aiohttp.ClientTimeout(total=5)
_THUMB_TOTAL_SECONDS = 8.0
_THUMB_MAX_BYTES = 3 * 1024 * 1024  # real thumbnails run tens-to-low-hundreds of KB; a ceiling, not a target
_THUMB_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Raster formats only. SVG in particular is excluded: relayed from this origin
# it could carry script, which would run with this server's origin.
_THUMB_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"})


class _ThumbRejected(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


async def _fetch_thumbnail(session: aiohttp.ClientSession, url: str) -> tuple[bytes, str]:
    """Fetch an already-validated thumbnail URL, following redirects by hand:
    every hop is re-validated against the host allowlist before it is
    requested, so an allowlisted host can't bounce this server to an arbitrary
    address (the automatic redirect-following this replaces did exactly that)."""
    async with asyncio.timeout(_THUMB_TOTAL_SECONDS):
        for _ in range(_THUMB_MAX_REDIRECTS + 1):
            async with session.get(url, timeout=_THUMB_HOP_TIMEOUT, allow_redirects=False) as upstream:
                if upstream.status in _REDIRECT_STATUSES:
                    next_url = resolve_thumb_redirect(url, upstream.headers.get("Location", ""))
                    if next_url is None:
                        raise _ThumbRejected(502, "Redirect not allowed")
                    url = next_url
                    continue
                if upstream.status != 200:
                    raise _ThumbRejected(502, "Upstream fetch failed")
                content_type = upstream.content_type
                if content_type not in _THUMB_CONTENT_TYPES:
                    raise _ThumbRejected(502, "Not an image")
                try:
                    declared_length = upstream.content_length
                except ValueError:  # aiohttp raises on a non-numeric Content-Length
                    raise _ThumbRejected(502, "Upstream fetch failed") from None
                if declared_length is not None and declared_length > _THUMB_MAX_BYTES:
                    raise _ThumbRejected(502, "Image too large")
                # Bounded-chunk read: a single .read(n) would cap one read, not
                # the total, on a slow/chunked upstream. Checking the running
                # total lets us bail before ever buffering past the cap.
                body = bytearray()
                async for chunk in upstream.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > _THUMB_MAX_BYTES:
                        raise _ThumbRejected(502, "Image too large")
                return bytes(body), content_type
    raise _ThumbRejected(502, "Too many redirects")


async def handle_thumb_proxy(request: web.Request) -> web.Response:
    """Same-origin relay for a track's thumbnail, fetched only so the overlay's
    canvas-based colour extraction can read pixels back out of it. Loading the
    image straight from YouTube's CDN taints the canvas (no
    Access-Control-Allow-Origin), so getImageData() throws and the extraction
    silently does nothing; relaying it through our own origin sidesteps that.

    Unauthenticated like the rest of the overlay surface, so it must not be an
    open relay: the URL has to pass security.validate_thumb_url (fixed CDN
    hosts, no parser tricks), redirects are re-validated hop by hop, only
    raster image types come back, and clients are rate limited."""
    ctx = get_ctx(request)
    if not ctx.thumb_limiter.allow(client_ip(request)):
        return web.Response(status=429, text="Too many requests")
    url = validate_thumb_url(request.query.get("url", ""))
    if url is None:
        return web.Response(status=400, text="URL not allowed")
    try:
        body, content_type = await _fetch_thumbnail(ctx.thumb_session, url)
    except _ThumbRejected as rejected:
        return web.Response(status=rejected.status, text=rejected.reason)
    except (aiohttp.ClientError, TimeoutError):
        return web.Response(status=502, text="Upstream fetch failed")
    return web.Response(
        body=body,
        content_type=content_type,
        headers={
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff",
            # Thumbnails for a given video/track ID are effectively immutable,
            # so the browser can cache aggressively instead of re-hitting this
            # proxy on every track change.
            "Cache-Control": "public, max-age=3600",
        },
    )


async def handle_stream(request: web.Request) -> web.StreamResponse:
    ctx = get_ctx(request)
    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "audio/mpeg", "Cache-Control": "no-cache"},
    )
    await response.prepare(request)
    queue = ctx.player.subscribe()
    try:
        while True:
            chunk = await queue.get()
            if not chunk:
                # Empty-bytes sentinel from the player's encoder pump: this
                # subscriber was dropped for falling behind; nothing more
                # will arrive on this queue.
                break
            await response.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        ctx.player.unsubscribe(queue)
    return response
