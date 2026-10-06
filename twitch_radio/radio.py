from __future__ import annotations

import logging
from collections import deque

from twitch_radio.extraction import Resolver
from twitch_radio.player import QueuedRequest
from twitch_radio.youtube import youtube_video_id

log = logging.getLogger(__name__)

_REQUESTER_LABEL = "\U0001f4fb Radio Mix"
# Bounds how far back "don't immediately repeat" looks — not a full play
# history, just enough to stop the same handful of tracks looping when a
# mix is short. In-memory only, same as everything else this player tracks.
_RECENT_HISTORY = 40


class RadioSuggester:
    """Auto-fills the queue from YouTube's own "RD<video_id>" Mix playlist —
    the same related-music ranking YouTube Music's autoplay uses — rather
    than building a recommendation engine here."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._recent: deque[str] = deque(maxlen=_RECENT_HISTORY)

    def note_played(self, webpage_url: str) -> None:
        """Called for every track that actually plays, requests included —
        the RD mix for a same-artist seed will happily re-surface a song a
        listener already requested earlier, and suggest() only knew about
        its own past picks without this."""
        vid = youtube_video_id(webpage_url)
        if vid:
            self._recent.append(vid)

    async def suggest(self, seed_webpage_url: str) -> QueuedRequest | None:
        video_id = youtube_video_id(seed_webpage_url)
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
