"""In-memory login sessions for /settings. Stdlib only.

Only the SHA-256 of each token is stored. Lifetime is absolute (no idle
timeout, so a settings page left open all stream doesn't lose a Save).
A restart signs everyone out.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable


class SessionStore:
    def __init__(
        self,
        *,
        lifetime_seconds: float,
        remember_seconds: float,
        max_sessions: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.lifetime_seconds = lifetime_seconds
        self.remember_seconds = remember_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._expiry: dict[str, float] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, *, remember: bool) -> str:
        now = self._clock()
        for key in [k for k, expires in self._expiry.items() if expires <= now]:
            del self._expiry[key]
        token = secrets.token_urlsafe(32)
        self._expiry[self._key(token)] = now + (self.remember_seconds if remember else self.lifetime_seconds)
        while len(self._expiry) > self._max_sessions:
            del self._expiry[next(iter(self._expiry))]
        return token

    def is_valid(self, token: str | None) -> bool:
        if not token:
            return False
        key = self._key(token)
        expires = self._expiry.get(key)
        if expires is None:
            return False
        if expires <= self._clock():
            del self._expiry[key]
            return False
        return True

    def destroy(self, token: str | None) -> None:
        if token:
            self._expiry.pop(self._key(token), None)
