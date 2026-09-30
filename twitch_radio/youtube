from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_YOUTUBE_ID_RE = re.compile(r"^[\w-]{11}$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def youtube_video_id(url: str) -> str | None:
    """Extracts the 11-character video ID from a YouTube watch/shorts/
    youtu.be URL, or None if it isn't one. Shared by radio.py (mix dedup)
    and blocklist.py (block/unblock keying) — one canonical ID per video
    regardless of which URL shape it was requested with."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        vid = parts.path.strip("/").split("/")[0]
        return vid if _YOUTUBE_ID_RE.match(vid) else None
    if host in _YOUTUBE_HOSTS:
        if parts.path.startswith("/shorts/"):
            vid = parts.path.removeprefix("/shorts/").strip("/").split("/")[0]
            return vid if _YOUTUBE_ID_RE.match(vid) else None
        values = parse_qs(parts.query).get("v")
        v = values[0] if values else None
        return v if v and _YOUTUBE_ID_RE.match(v) else None
    return None
