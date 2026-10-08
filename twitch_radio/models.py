from __future__ import annotations

import re
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


_YOUTUBE_ID = re.compile(r"[\w-]{11}")


def youtube_thumbnail(entry: dict) -> str | None:  # type: ignore[type-arg]
    """A picture for a yt-dlp entry. Flat results (search, radio mix) carry no `thumbnail`, but a
    YouTube video's picture is always at a predictable address on i.ytimg.com."""
    thumbnail = entry.get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail.startswith("https://"):
        return thumbnail
    video_id = entry.get("id")
    if isinstance(video_id, str) and _YOUTUBE_ID.fullmatch(video_id):
        return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    return None
