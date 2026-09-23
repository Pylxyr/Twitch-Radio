from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class JsonStore:
    """Tiny atomic JSON key-value file, guarded by an in-process asyncio.Lock.

    Reads/writes are serialized by the lock, and writes are
    write-temp-then-rename so a crash mid-write can't leave a corrupt or
    half-written file behind.

    Reads are served from an in-memory copy, revalidated against the
    file's (mtime_ns, size) on every call — tunables/toggles get read on
    most chat commands (!sr, !voteskip, !radio, ...), so this avoids a
    lock acquisition, a thread-pool dispatch and a json.load() on every
    one of those just to answer "what's the current queue cap?". A
    stat() is cheap enough to do inline, and unlike a time-based cache it
    still picks up a hand-edited tunables.json/toggles.json immediately.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._cache: dict[str, Any] | None = None
        self._cache_key: tuple[int, int] | None = None

    def _stat_key(self) -> tuple[int, int] | None:
        """(mtime_ns, size), or None if the file doesn't exist yet. Both,
        not just mtime: some filesystems have coarse timestamp granularity,
        and a same-millisecond rewrite of a different length still changes
        the size."""
        try:
            st = self._path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def invalidate(self) -> None:
        """Drops the cached copy — next read() goes to disk unconditionally.
        Not needed for writes through this class (they refresh the cache
        themselves); here for a caller that knows the file changed underneath
        it in a way stat() can't see."""
        self._cache = None
        self._cache_key = None

    async def read(self) -> dict[str, Any]:
        async with self._lock:
            key = self._stat_key()
            if self._cache is not None and key == self._cache_key:
                # Shallow copy: callers treat the result as read-only and
                # build new containers for any mutation (see
                # toggles.py's/tunables.py's to_dict()), but handing out
                # the cached dict itself would make an accidental
                # in-place edit stick invisibly until the next file change.
                return dict(self._cache)
            data = await asyncio.to_thread(self._read_sync)
            self._cache = data
            # Re-stat *after* reading, not before: a write that landed while
            # the read was in flight would otherwise be cached under the
            # pre-write key and served stale until the next change.
            self._cache_key = self._stat_key()
            return dict(data)

    async def write(self, data: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_sync, data)
            self._cache = dict(data)
            self._cache_key = self._stat_key()

    async def update(
        self, mutator: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> dict[str, Any]:
        """Read-modify-write while holding the lock across all three steps,
        so two concurrent callers (e.g. two /settings submissions) can't
        silently clobber each other. `mutator` returns the dict to persist,
        or None to leave the file untouched."""
        async with self._lock:
            key = self._stat_key()
            if self._cache is not None and key == self._cache_key:
                current = dict(self._cache)
            else:
                current = await asyncio.to_thread(self._read_sync)
                self._cache = dict(current)
                self._cache_key = self._stat_key()
            updated = mutator(current)
            if updated is None:
                return current
            await asyncio.to_thread(self._write_sync, updated)
            self._cache = dict(updated)
            self._cache_key = self._stat_key()
            return updated

    def _read_sync(self) -> dict[str, Any]:
        try:
            with self._path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError):
            # Falls back to defaults either way, but this should show up in
            # the logs rather than silently vanishing someone's saved settings.
            log.warning("Couldn't read %s — falling back to defaults.", self._path, exc_info=True)
            return {}
        if not isinstance(loaded, dict):
            log.warning("%s did not contain a JSON object — falling back to defaults.", self._path)
            return {}
        return loaded

    def _write_sync(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)
            raise
