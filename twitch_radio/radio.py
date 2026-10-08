from __future__ import annotations

import logging
from collections import deque
from collections.abc import Collection
from typing import Any

from twitch_radio.extraction import Resolver
from twitch_radio.models import youtube_thumbnail
from twitch_radio.player import QueuedRequest
from twitch_radio.youtube import youtube_video_id

log = logging.getLogger(__name__)

_REQUESTER_LABEL = "\U0001f4fb Radio Mix"
# Bounds how far back "don't immediately repeat" looks — not a full play
# history, just enough to stop the same handful of tracks looping when a
# mix is short. In-memory only, same as everything else this player tracks.
_RECENT_HISTORY = 40

# How many entries of a radio mix a lookup asks YouTube for at least (more when the lookahead wants
# more songs). Flat entries only, so a bigger list costs a little network time and no extraction work.
_MIX_ENTRIES = 15


class RadioSuggester:
    """Auto-fills the queue from YouTube's own "RD<video_id>" Mix playlist —
    the same related-music ranking YouTube Music's autoplay uses — rather
    than building a recommendation engine here."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver
        self._recent: deque[str] = deque(maxlen=_RECENT_HISTORY)
        # The mix suggest_many() is currently walking: the video it was built from, its entries,
        # and their ids. None until the first lookup.
        self._mix_origin: str | None = None
        self._mix_entries: list[dict[str, Any]] = []
        self._mix_ids: set[str] | None = None

    def note_played(self, webpage_url: str) -> None:
        """Called for every track that actually plays, requests included —
        the RD mix for a same-artist seed will happily re-surface a song a
        listener already requested earlier, and suggest() only knew about
        its own past picks without this."""
        vid = youtube_video_id(webpage_url)
        if vid:
            self._recent.append(vid)

    async def suggest_many(
        self, seed_webpage_url: str, count: int, exclude_ids: Collection[str] = ()
    ) -> list[QueuedRequest]:
        """Up to `count` tracks that follow `seed_webpage_url` in its radio mix, in the mix's own
        order - what the radio-mix lookahead queues.

        One lookup yields the whole mix (a few dozen entries), so the mix is kept and reused while
        the queue keeps following it: each top-up continues where the last one stopped, the way
        YouTube Music's radio does, instead of asking YouTube again after every song. A seed that is
        not part of the kept mix (a fresh request, say) starts a new one. `exclude_ids` are video
        ids that must not be picked again (already queued or playing)."""
        seed_id = youtube_video_id(seed_webpage_url)
        if seed_id is None or count <= 0:
            return []
        skip = set(exclude_ids) | {seed_id}

        # Ask for more entries than songs wanted: some are the seed, queued or recently played.
        limit = max(_MIX_ENTRIES, count + 10)
        reused = self._mix_ids is not None and (seed_id == self._mix_origin or seed_id in self._mix_ids)
        if not reused:
            await self._load_mix(seed_id, limit)
        picks = self._take(skip, count)
        if len(picks) < count and (picks or reused):
            # The mix ran dry (kept, or a short fresh one): continue with a new mix built from the
            # last song picked - or from the seed, when a kept mix had nothing left to give.
            last_id = (picks[-1].get("id") if picks else None) or seed_id
            await self._load_mix(str(last_id), limit)
            picks += self._take(skip | {str(p.get("id")) for p in picks}, count - len(picks))
        return [self._as_request(entry) for entry in picks]

    async def _load_mix(self, video_id: str, limit: int) -> None:
        mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        try:
            entries = await self._resolver.resolve_radio_mix(mix_url, limit)
        except Exception:
            log.debug("Radio mix lookup failed for %s (non-fatal).", video_id, exc_info=True)
            entries = []
        self._mix_origin = video_id
        self._mix_entries = [e for e in (entries or []) if e.get("id")]
        self._mix_ids = {str(e["id"]) for e in self._mix_entries}

    def _take(self, skip: set[str], count: int) -> list[dict[str, Any]]:
        """The next `count` usable entries of the kept mix, marking each as recently picked."""
        taken: list[dict[str, Any]] = []
        for entry in self._mix_entries:
            vid = str(entry["id"])
            # The mix's own origin is the song that started it: never queue it behind itself.
            if vid in skip or vid in self._recent or vid == self._mix_origin:
                continue
            taken.append(entry)
            skip = skip | {vid}
            if len(taken) == count:
                break
        for entry in taken:
            self._recent.append(str(entry["id"]))
        return taken

    @staticmethod
    def _as_request(entry: dict[str, Any]) -> QueuedRequest:
        vid = entry["id"]
        return QueuedRequest(
            webpage_url=entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
            requester_id=0,  # never a real Twitch user ID, same convention as suggest()
            requester_name=_REQUESTER_LABEL,
            title=entry.get("title") or "Unknown title",
            uploader=entry.get("uploader") or entry.get("channel") or "",
            thumbnail_url=youtube_thumbnail(entry),
        )

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
                thumbnail_url=youtube_thumbnail(entry),
            )
        return None
