from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from twitch_radio.store import JsonStore
from twitch_radio.youtube import youtube_video_id


@dataclass(slots=True)
class BlockedTrack:
    video_id: str
    title: str
    uploader: str
    blocked_by: str
    blocked_at: float


class BlockList:
    """Per-video (not per-artist) block list, keyed by YouTube video ID —
    see !block/!unblock/!blocklist in components/song_requests.py. Checked
    at song-request time and by RadioSuggester at mix-suggestion time, so a
    blocked video can't come back through either path."""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    async def is_blocked(self, webpage_url: str) -> bool:
        vid = youtube_video_id(webpage_url)
        if vid is None:
            return False
        data = await self._store.read()
        return vid in data

    async def block(self, webpage_url: str, *, title: str, uploader: str, blocked_by: str) -> bool:
        """False if webpage_url isn't a recognizable YouTube video."""
        vid = youtube_video_id(webpage_url)
        if vid is None:
            return False

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            updated = dict(current)
            updated[vid] = {
                "title": title,
                "uploader": uploader,
                "blocked_by": blocked_by,
                "blocked_at": time.time(),
            }
            return updated

        await self._store.update(_mutate)
        return True

    async def unblock(self, webpage_url: str) -> bool:
        """False if it wasn't blocked (or isn't a recognizable YouTube video)."""
        vid = youtube_video_id(webpage_url)
        if vid is None:
            return False
        removed = False

        def _mutate(current: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal removed
            if vid not in current:
                return None
            removed = True
            updated = dict(current)
            del updated[vid]
            return updated

        await self._store.update(_mutate)
        return removed

    async def list_blocked(self) -> list[BlockedTrack]:
        data = await self._store.read()
        return [
            BlockedTrack(
                video_id=vid,
                title=str(entry.get("title", "")),
                uploader=str(entry.get("uploader", "")),
                blocked_by=str(entry.get("blocked_by", "")),
                blocked_at=float(entry.get("blocked_at", 0.0) or 0.0),
            )
            for vid, entry in data.items()
            if isinstance(entry, dict)
        ]
