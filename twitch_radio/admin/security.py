"""Security helpers for the admin server that need nothing but the stdlib.

Everything here is a plain function or class with no aiohttp dependency, so
the rules that actually protect /settings and /thumb-proxy live in one small
module that can be read (and reasoned about) on its own instead of being
spread through the request handlers.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

# ---------------------------------------------------------------------------
# Login redirects and CSRF
# ---------------------------------------------------------------------------

DEFAULT_LANDING = "/settings"
_MAX_NEXT_LENGTH = 512
_NEXT_DENYLIST = ("/login", "/logout")


def safe_next_path(raw: str | None, default: str = DEFAULT_LANDING) -> str:
    """A same-site absolute path from the `next` field, else `default`."""
    if not raw or len(raw) > _MAX_NEXT_LENGTH:
        return default
    if not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return default
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return default
    try:
        parts = urlsplit(raw)
    except ValueError:
        return default
    if parts.scheme or parts.netloc:
        return default
    if parts.path.rstrip("/") in _NEXT_DENYLIST:
        return default
    return raw


def origin_matches_host(origin: str | None, referer: str | None, hosts: tuple[str | None, ...]) -> bool:
    """Origin (or Referer) must name one of `hosts`. Requests carrying neither
    header (curl, scripts) pass; browsers always send Origin on cross-site POSTs."""
    source = origin
    if source is None and referer:
        try:
            parts = urlsplit(referer)
        except ValueError:
            return False
        source = f"{parts.scheme}://{parts.netloc}"
    if source is None:
        return True
    allowed: set[str] = set()
    for host in hosts:
        host = (host or "").strip().lower()
        if host:
            allowed.update((f"http://{host}", f"https://{host}"))
    return source.lower() in allowed


def fetch_site_ok(sec_fetch_site: str | None) -> bool:
    if sec_fetch_site is None:
        return True
    return sec_fetch_site.strip().lower() in ("same-origin", "none")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class _SlidingWindow:
    """Per-key event timestamps within a trailing window, bounded in memory.

    Keys whose events have all expired are dropped rather than kept forever
    (the old limiters only pruned a key when that same key came back, so every
    one-off client left an entry behind for the life of the process), and the
    number of tracked keys is capped so a flood of distinct source addresses
    can't grow it without limit.
    """

    def __init__(
        self,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ) -> None:
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._events: dict[str, list[float]] = {}

    def _live(self, key: str, now: float) -> list[float]:
        events = [t for t in self._events.get(key, ()) if now - t < self._window]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return events

    def count(self, key: str) -> int:
        return len(self._live(key, self._clock()))

    def add(self, key: str) -> None:
        now = self._clock()
        events = self._live(key, now)
        events.append(now)
        self._events[key] = events
        if len(self._events) > self._max_keys:
            self._shrink(now)

    def clear(self, key: str) -> None:
        self._events.pop(key, None)

    def seconds_until_below(self, key: str, limit: int) -> float:
        now = self._clock()
        events = self._live(key, now)
        if len(events) < limit:
            return 0.0
        return max(0.0, events[len(events) - limit] + self._window - now)

    def _shrink(self, now: float) -> None:
        for key in list(self._events):
            self._live(key, now)
        # Still over the cap with every remaining key live: drop the
        # longest-tracked ones (dict order is insertion order).
        excess = len(self._events) - (self._max_keys * 3) // 4
        if excess > 0:
            for key in list(self._events)[:excess]:
                del self._events[key]

    def __len__(self) -> int:
        return len(self._events)


class AuthRateLimiter:
    """Lockout for failed /login attempts, keyed by client address. In-memory."""

    def __init__(
        self,
        max_attempts: int = 10,
        window_seconds: float = 300.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_attempts = max_attempts
        self._failures = _SlidingWindow(window_seconds, clock=clock)

    def is_blocked(self, key: str) -> bool:
        return self._failures.count(key) >= self._max_attempts

    def record_failure(self, key: str) -> None:
        self._failures.add(key)

    def record_success(self, key: str) -> None:
        self._failures.clear(key)

    def retry_after(self, key: str) -> int:
        if not self.is_blocked(key):
            return 0
        return max(1, int(self._failures.seconds_until_below(key, self._max_attempts)) + 1)


class RequestRateLimiter:
    """Plain per-key throttle with no lockout escalation, for public routes
    where there's no secret to brute-force and the goal is just to stop one
    client turning a cheap page into load on the process that also serves the
    audio stream. allow() says yes or no for *this* request."""

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_requests
        self._hits = _SlidingWindow(window_seconds, clock=clock)

    def allow(self, key: str) -> bool:
        if self._hits.count(key) >= self._max:
            return False
        self._hits.add(key)
        return True


# ---------------------------------------------------------------------------
# /thumb-proxy URL validation
# ---------------------------------------------------------------------------

# Hostnames /thumb-proxy will fetch from: YouTube's thumbnail CDN (ytimg.com),
# and YouTube channel/avatar images (ggpht.com, googleusercontent.com —
# yt-dlp occasionally surfaces these as a video's "thumbnail"). Suffix-matched:
# host == suffix or a subdomain of it.
THUMB_HOST_SUFFIXES = ("ytimg.com", "ggpht.com", "googleusercontent.com")

_MAX_THUMB_URL_LENGTH = 2048
_DNS_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


def is_allowed_thumb_host(host: str) -> bool:
    host = host.lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in THUMB_HOST_SUFFIXES)


def validate_thumb_url(url: str) -> str | None:
    """Return `url` unchanged if it is safe to fetch, else None.

    The allowlist is only meaningful if the host we *validate* is the host the
    HTTP client *connects to*. Two different URL parsers (urllib here, yarl
    inside aiohttp) can disagree about odd input — backslashes, userinfo
    (`https://evil@ytimg.com`), embedded control characters — which is the
    classic way to slip past a host check. So instead of trusting them to
    agree, refuse anything unusual outright: printable ASCII only, no
    backslash, no userinfo, only default web ports, and a plain DNS-style
    hostname. What's left parses the same everywhere.
    """
    if not url or len(url) > _MAX_THUMB_URL_LENGTH:
        return None
    if any(ord(ch) <= 0x20 or ord(ch) >= 0x7F or ch == "\\" for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or "@" in parts.netloc:
        return None
    if port not in (None, 80, 443):
        return None
    host = parts.hostname or ""
    if not _DNS_HOST.fullmatch(host) or not is_allowed_thumb_host(host):
        return None
    return url


def resolve_thumb_redirect(current_url: str, location: str) -> str | None:
    """Validate a redirect hop before following it (None = refuse). Redirects
    are followed manually, one validated hop at a time, because an HTTP
    client's automatic redirect-following would happily carry an allowlisted
    host's response to any address — including this machine's own network."""
    if not location or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in location):
        return None
    return validate_thumb_url(urljoin(current_url, location))
