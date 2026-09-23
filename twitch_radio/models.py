from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Track:
    """A resolved, playable track. stream_url is a direct, short-lived
    media URL from yt-dlp — don't hold onto one past Resolver's own cache
    window (YTDLP_CACHE_TTL_SECONDS), since it expires."""

    title: str
    webpage_url: str
    stream_url: str
    uploader: str
    duration: int
    requester_id: int
    thumbnail_url: str | None = None
    query: str = ""
    is_live: bool = False
