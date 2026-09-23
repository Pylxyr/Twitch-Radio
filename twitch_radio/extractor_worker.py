"""Child process for out-of-process yt-dlp extraction.

Run as ``python -m twitch_radio.extractor_worker`` by
extraction.ProcessBackend; never imported by the bot itself. Speaks
newline-delimited JSON over stdin/stdout, one request per line, one
response per line, strictly in order (a worker handles one request at a
time — the pool provides concurrency by running several of these).

    -> {"id": 7, "query": "ytsearch1:...", "options": {...}}
    <- {"id": 7, "ok": true,  "info": {...}}
    <- {"id": 7, "ok": false, "kind": "download", "error": "..."}

Why a long-lived process rather than one per extraction: importing yt_dlp
costs the best part of a second, and YoutubeDL caches the solved signature
challenge on the extractor instance, which a fresh process would throw
away every time. So the process is reused, and `options` is hashed to key
a YoutubeDL instance per distinct configuration (fast/fallback attempts
differ in player_client/js_runtimes, the radio-mix lookup differs again)
— the same trick ThreadBackend plays with threading.local, except a hung
one here can actually be killed.

Three things this file has to get right because the parent can't fix them:

1. stdout is the protocol channel and nothing else may touch it. yt-dlp
   writes to sys.stdout directly on several paths regardless of `quiet`,
   and a single stray line of progress output would desynchronise the
   framing for the rest of the process's life. sys.stdout is therefore
   redirected to stderr immediately on startup and the real handle is kept
   private to _respond().

2. Responses are projected down to the handful of fields the resolver
   actually reads. A full YouTube info dict is megabytes of format
   listings; serialising that through a pipe on every resolve would cost
   more than it saves.

3. A failure has to come back as a response, not a traceback on stderr —
   the parent is waiting on a line for this request id and would otherwise
   sit there until its timeout fires.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

# Fields the parent's Track building and radio-mix picking actually read
# (see Resolver._do_resolve, Resolver._has_playable_url, and
# RadioSuggester.suggest). Everything else — formats, subtitles, chapters,
# heatmaps, the full description — is dropped before it ever hits the pipe.
_KEEP_FIELDS = (
    "id",
    "url",
    "webpage_url",
    "title",
    "uploader",
    "channel",
    "duration",
    "thumbnail",
    "is_live",
)

# Hard ceiling on projected playlist entries, so a pathological playlist
# can't produce an enormous response line. The only playlist this ever sees
# is a radio mix already limited to 15 by `playlist_items`.
_MAX_ENTRIES = 50


def _project(info: dict[str, Any]) -> dict[str, Any]:
    """Reduces a yt-dlp info dict to _KEEP_FIELDS, recursively for
    `entries`. Nulls are dropped rather than sent — the parent uses
    `.get(...) or <default>` throughout, so absent and None behave
    identically there and absent is smaller on the wire."""
    out: dict[str, Any] = {}
    for key in _KEEP_FIELDS:
        value = info.get(key)
        if value is not None:
            out[key] = value
    entries = info.get("entries")
    if entries is not None:
        # May be a generator in flat-playlist mode; materialise it, drop the
        # None padding yt-dlp uses for unavailable items, and cap the count.
        projected = []
        for entry in entries:
            if not entry:
                continue
            projected.append(_project(entry))
            if len(projected) >= _MAX_ENTRIES:
                break
        out["entries"] = projected
    return out


class _Worker:
    def __init__(self, out: TextIO) -> None:
        self._out = out
        # YoutubeDL instances keyed by their options — see the module
        # docstring on why these are reused rather than rebuilt per request.
        self._instances: dict[str, Any] = {}

    def _ydl(self, options: dict[str, Any]) -> Any:
        import yt_dlp

        key = json.dumps(options, sort_keys=True)
        instance = self._instances.get(key)
        if instance is None:
            instance = yt_dlp.YoutubeDL(options)
            self._instances[key] = instance
        return instance

    def _respond(self, payload: dict[str, Any]) -> None:
        self._out.write(json.dumps(payload, ensure_ascii=True) + "\n")
        self._out.flush()

    def handle(self, request: dict[str, Any]) -> None:
        import yt_dlp

        request_id = request.get("id")
        try:
            info = self._ydl(request["options"]).extract_info(request["query"], download=False)
        except yt_dlp.utils.DownloadError as exc:
            # Kept as its own kind so the parent can keep raising its own
            # DownloadError for it, exactly as the in-process path did.
            self._respond({"id": request_id, "ok": False, "kind": "download", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — must not escape; see module docstring
            self._respond(
                {"id": request_id, "ok": False, "kind": "other", "error": f"{type(exc).__name__}: {exc}"}
            )
        else:
            projected = _project(info) if isinstance(info, dict) else {}
            self._respond({"id": request_id, "ok": True, "info": projected})

    def run(self) -> None:
        self._respond({"ready": True})
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except ValueError:
                # Unparseable framing: there's no id to answer against, so
                # the parent's timeout is the only available recovery.
                # Nothing useful to do but keep going.
                continue
            if not isinstance(request, dict) or "query" not in request:
                continue
            self.handle(request)


def main() -> int:
    # Claim the real stdout for the protocol and point sys.stdout at stderr
    # before anything else can print to it. Must happen before yt_dlp is
    # imported anywhere in this process.
    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    try:
        _Worker(protocol_out).run()
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        # Parent went away (shutdown, or it killed us after a timeout).
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
