from __future__ import annotations

import logging
import re
from collections import deque
from urllib.parse import parse_qs, urlsplit

from twitch_radio.extraction import Resolver
from twitch_radio.player import QueuedRequest

log = logging.getLogger(__name__)

_REQUESTER_LABEL = "\U0001f4fb Radio Mix"
# Bounds how far back "don't immediately repeat" looks — not a full play
# history, just enough to stop the same handful of tracks looping when a
# mix is short. In-memory only, same as everything else this player tracks.
_RECENT_HISTORY = 25

_YOUTUBE_ID_RE = re.compile(r"^[\w-]{11}$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def _youtube_video_id(url: str) -> str | None:
    """Extracts the 11-character video ID from a YouTube watch/shorts/
    youtu.be URL, or None if it isn't one."""
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


class RadioSuggester:
    """Auto-fills the queue from YouTube's own "RD<video_id>" Mix playlist —
    the same related-music ranking YouTube Music's autoplay uses — rather
    than building a recommendation engine here."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._recent: deque[str] = deque(maxlen=_RECENT_HISTORY)

    async def suggest(self, seed_webpage_url: str) -> QueuedRequest | None:
        video_id = _youtube_video_id(seed_webpage_url)
        if video_id is None:
            return None
        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        try:
            entries = await self._resolver.resolve_radio_mix(mix_url)
        except Exception:
            log.debug("Radio mix lookup failed for %s (non-fatal).", seed_webpage_url, exc_info=True)
            return None
        if not entries:
            return None

        for entry in entries:
            vid = entry.get("id")
            if not vid or vid == video_id or vid in self._recent:
                continue
            entry_url = entry.get("url") or f"https://www.youtube.com/watch?v={vid}"
            uploader = entry.get("uploader") or entry.get("channel") or ""
            self._recent.append(vid)
            return QueuedRequest(
                webpage_url=entry_url,
                requester_id=0,  # never a real Twitch user ID — see models.Track's same convention
                requester_name=_REQUESTER_LABEL,
                title=entry.get("title") or "Unknown title",
                uploader=uploader,
            )
        return None
