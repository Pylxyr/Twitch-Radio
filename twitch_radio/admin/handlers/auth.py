"""Request checks for the /settings routes: response headers that keep the
page out of caches and frames, and the Origin check for saving."""

from __future__ import annotations

import logging

from aiohttp import web

from twitch_radio.admin.security import fetch_site_ok, origin_matches_host

log = logging.getLogger(__name__)

PROTECTED_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


def protect(response: web.Response) -> web.Response:
    for name, value in PROTECTED_HEADERS.items():
        response.headers[name] = value
    return response


def origin_ok(request: web.Request) -> bool:
    ok = origin_matches_host(
        request.headers.get("Origin"),
        request.headers.get("Referer"),
        (request.headers.get("Host"),),
    ) and fetch_site_ok(request.headers.get("Sec-Fetch-Site"))
    if not ok:
        log.warning(
            "Rejected %s %s - Origin/Referer/Sec-Fetch-Site didn't match Host (possible cross-site request).",
            request.method,
            request.path,
        )
    return ok
