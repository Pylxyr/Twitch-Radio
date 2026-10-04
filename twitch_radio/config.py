from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from twitch_radio.paths import bundled_tool_path, env_template_path, home_dir, is_frozen

# The writable root (.env, data/, logs/). The project folder when run from
# source, %APPDATA%\TwitchRadio when packaged - see paths.py.
BASE_DIR = home_dir()
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
ENV_PATH = BASE_DIR / ".env"

# Variables that were already set in the real environment before we ever read
# .env. They keep winning over the file, exactly like python-dotenv's default
# (override=False) - a value exported in the shell is a deliberate override.
_REAL_ENV: dict[str, str | None] = {}
_FROM_DOTENV: set[str] = set()


def reload_env() -> None:
    """(Re)reads .env into os.environ.

    load_dotenv() at import time only ever adds keys, so a desktop app that
    edits .env and restarts the bot inside one process would keep serving
    stale values - and a line deleted from .env would never go away. This
    undoes what the previous call injected before applying the file again."""
    for key in list(_FROM_DOTENV):
        original = _REAL_ENV.get(key)
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original
    _FROM_DOTENV.clear()
    try:
        values = dotenv_values(ENV_PATH, encoding="utf-8", interpolate=False) if ENV_PATH.is_file() else {}
    except OSError:
        values = {}
    for key, value in values.items():
        if value is None:
            continue
        _REAL_ENV.setdefault(key, os.environ.get(key))
        if _REAL_ENV[key] is None:
            os.environ[key] = value
            _FROM_DOTENV.add(key)


def _warn(message: str) -> None:
    """print() before logging exists (the CLI), a real log record after it
    (the desktop app installs its log handler first, so these reach the Logs
    tab instead of vanishing)."""
    if logging.getLogger().handlers:
        logging.getLogger(__name__).warning(message.removeprefix("WARNING: "))
    else:
        print(message)


def ensure_home() -> None:
    """Creates the writable folders and, on first run, a .env from the
    template - what setup.bat did for the source install."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    template = env_template_path()
    if not ENV_PATH.exists() and template.is_file():
        shutil.copyfile(template, ENV_PATH)


reload_env()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # print(), not log — this runs before configure_logging() exists.
        _warn(f"WARNING: {name}={raw!r} is not a valid integer — using {default}.")
        return default


_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off"}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False
    _warn(f"WARNING: {name}={raw!r} is not a recognized boolean — using {default}.")
    return default


def _clamped_int_env(name: str, default: int, lo: int, hi: int) -> int:
    value = _int_env(name, default)
    clamped = max(lo, min(hi, value))
    if clamped != value:
        _warn(f"WARNING: {name}={value} is outside the allowed range {lo}-{hi} — using {clamped}.")
    return clamped


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _log_level_env(name: str, default: str) -> str:
    raw = os.getenv(name, "").strip().upper()
    if not raw:
        return default
    if raw not in _VALID_LOG_LEVELS:
        _warn(f"WARNING: {name}={raw!r} is not a valid log level — using {default}.")
        return default
    return raw


def _check_cookies_path_writable(raw: str, path: Path) -> None:
    # yt-dlp rewrites this file on every extraction - fail loudly at startup
    # instead of on every !sr.
    cookies_dir = path.parent
    try:
        cookies_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"YTDLP_COOKIES_FILE={raw!r} resolves to {path}, but its directory ({cookies_dir}) "
            f"couldn't be created: {exc}. Use a path under data/ instead, e.g. "
            f"YTDLP_COOKIES_FILE=data/cookies.txt."
        ) from exc
    if not os.access(cookies_dir, os.W_OK):
        raise RuntimeError(
            f"YTDLP_COOKIES_FILE={raw!r} resolves to {path}, but {cookies_dir} isn't writable. "
            f"Use a path under data/ instead, e.g. YTDLP_COOKIES_FILE=data/cookies.txt."
        )


# yt-dlp player_client names and whether each accepts cookie auth (mirrors
# a private yt-dlp module's INNERTUBE_CLIENTS table) — hardcoded since it's
# private API; re-verify against the pinned yt-dlp version if this needs
# updating.
_VALID_PLAYER_CLIENTS = {
    "web": True,
    "web_safari": True,
    "web_embedded": True,
    "web_music": True,
    "web_creator": True,
    "android": False,
    "android_vr": False,
    "ios": False,
    "visionos": False,
    "mweb": True,
    "tv": True,
    "tv_downgraded": True,
    "tv_simply": False,
}


def _check_player_clients(raw_clients: tuple[str, ...], cookies_configured: bool) -> None:
    unknown = [c for c in raw_clients if c not in _VALID_PLAYER_CLIENTS]
    if unknown:
        _warn(
            f"WARNING: YTDLP_PLAYER_CLIENT has unrecognized client name(s) {unknown} — yt-dlp "
            f"will just skip them with a warning. Valid names: {sorted(_VALID_PLAYER_CLIENTS)}"
        )
    if cookies_configured and raw_clients:
        cookie_ok = [c for c in raw_clients if _VALID_PLAYER_CLIENTS.get(c)]
        if not cookie_ok:
            raise RuntimeError(
                f"YTDLP_PLAYER_CLIENT={','.join(raw_clients)!r} has no client that supports "
                f"cookie auth, but YTDLP_COOKIES_FILE is set — every client gets skipped and "
                f"every request fails. android/android_vr/ios/visionos/tv_simply all reject "
                f"cookies outright; mix in at least one of web/web_safari/web_embedded/"
                f"web_music/web_creator/mweb/tv/tv_downgraded, or unset YTDLP_COOKIES_FILE."
            )


@dataclass(frozen=True, slots=True)
class Settings:
    # Twitch app credentials — from https://dev.twitch.tv/console/apps
    client_id: str
    client_secret: str
    bot_id: str
    owner_id: str
    # Not configurable — kept as a Settings field (rather than a bare
    # module constant) since chatbot.py/admin.app.py just read
    # settings.prefix either way, same as every other value here.
    prefix: str

    # Audio
    audio_bitrate_kbps: int
    # If True, don't start a new track while nobody's subscribed to
    # /stream.opus — holds at the current boundary and resumes once someone
    # (re)connects. A track already playing finishes normally either way.
    # Off by default (the queue has always run on a real-time clock
    # regardless of listeners); opt in via PAUSE_QUEUE_WHEN_NO_LISTENERS=true.
    pause_when_no_listeners: bool

    # How loudness is evened out between tracks:
    #   "static"  (default) measure ~20 s of the track once, apply a fixed gain
    #             plus a limiter - about a tenth of dynamic's CPU and memory;
    #   "dynamic" ffmpeg's loudnorm filter on the whole stream (the old behaviour);
    #   "off"     play tracks as they are.
    loudness_mode: str

    # Local HTTP surface (127.0.0.1 only) - /stream.opus, /overlay, /nowplaying.json, /settings
    nowplaying_port: int

    # Persistence, all under DATA_DIR.
    token_path: Path
    tunables_path: Path
    toggles_path: Path
    blocklist_path: Path
    queue_state_path: Path

    # yt-dlp
    ytdlp_cookies_file: Path | None
    ytdlp_js_runtime_path: str | None
    ytdlp_js_runtime_name: str
    ytdlp_concurrency: int
    ytdlp_extract_timeout_seconds: int
    ytdlp_player_client: tuple[str, ...]
    ytdlp_cache_ttl_seconds: int
    ytdlp_pot_provider_url: str | None
    # Extraction runs in child processes started on demand; one that has been
    # idle this long exits (see extraction.ProcessBackend).
    ytdlp_worker_idle_seconds: int

    # Logging
    log_level: str
    log_to_file: bool
    log_dir: Path


LOUDNESS_MODES = ("static", "dynamic", "off")

HOST_JS_ENV = "TWITCH_RADIO_HOST_JS_EXE"


def _host_js_runtime() -> str | None:
    """Path of the desktop app executable that launched this core, if it handed one over.

    Only the packaged desktop app sets it (gui/main.js). Never run this executable without
    ELECTRON_RUN_AS_NODE=1: it is the full GUI application.
    """
    exe = os.getenv(HOST_JS_ENV, "").strip()
    return exe if exe and Path(exe).is_file() else None


def load_settings() -> Settings:
    ensure_home()
    reload_env()

    def _required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise RuntimeError(f"{name} is not set — add it to .env before starting. See .env.example.")
        return value

    def _required_numeric_id(name: str) -> str:
        # Guards against e.g. TWITCH_BOT_ID pasted as "Twitch ID:1536026185"
        # instead of just the digits — Helix rejects that with a bare
        # "Bad Identifiers" error.
        value = _required(name)
        if not value.isdigit():
            raise RuntimeError(
                f"{name}={value!r} isn't a plain numeric Twitch user ID — digits only, no "
                f"username, no label. Look one up at "
                f"https://www.streamweasels.com/tools/convert-twitch-username-to-user-id/"
            )
        return value

    client_id = _required("TWITCH_CLIENT_ID")
    client_secret = _required("TWITCH_CLIENT_SECRET")
    bot_id = _required_numeric_id("TWITCH_BOT_ID")
    owner_id = _required_numeric_id("TWITCH_OWNER_ID")

    js_runtime_path = os.getenv("YTDLP_JS_RUNTIME_PATH", "").strip() or None
    js_runtime_name = os.getenv("YTDLP_JS_RUNTIME_NAME", "deno").strip() or "deno"
    if js_runtime_path is None and is_frozen() and js_runtime_name == "deno":
        # Packaged app. A Deno bundled next to the core wins (custom builds can still ship one);
        # otherwise the desktop app's own executable is the runtime. It is Electron, which contains
        # Node: with ELECTRON_RUN_AS_NODE=1 it behaves like `node` and yt-dlp accepts it as one.
        bundled_deno = bundled_tool_path("deno")
        if bundled_deno is not None:
            js_runtime_path = str(bundled_deno)
        elif (host_exe := _host_js_runtime()) is not None:
            js_runtime_path, js_runtime_name = host_exe, "node"
            os.environ["ELECTRON_RUN_AS_NODE"] = "1"  # inherited by the runtime yt-dlp starts

    cookies_raw = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    cookies_path = (BASE_DIR / cookies_raw) if cookies_raw else None
    if cookies_path is not None:
        _check_cookies_path_writable(cookies_raw, cookies_path)

    player_client_raw = os.getenv("YTDLP_PLAYER_CLIENT", "").strip()
    if not player_client_raw and cookies_path is not None:
        # yt-dlp's default client list with cookies set includes
        # tv_downgraded, which has a known open bug (yt-dlp#17389) — pin
        # to the other two already-default clients instead.
        player_client_raw = "web_embedded,web"
    ytdlp_player_client = tuple(c.strip() for c in player_client_raw.split(",") if c.strip())
    _check_player_clients(ytdlp_player_client, cookies_configured=cookies_path is not None)

    loudness_mode = os.getenv("LOUDNESS_MODE", "static").strip().lower() or "static"
    if loudness_mode not in LOUDNESS_MODES:
        _warn(
            f"WARNING: LOUDNESS_MODE={loudness_mode!r} is not one of {', '.join(LOUDNESS_MODES)} - using 'static'."
        )
        loudness_mode = "static"

    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        bot_id=bot_id,
        owner_id=owner_id,
        # Not read from the environment — this bot only ever answers to "!".
        prefix="!",
        audio_bitrate_kbps=_clamped_int_env("AUDIO_BITRATE_KBPS", 160, 64, 256),
        pause_when_no_listeners=_bool_env("PAUSE_QUEUE_WHEN_NO_LISTENERS", False),
        loudness_mode=loudness_mode,
        nowplaying_port=_clamped_int_env("TWITCH_NOWPLAYING_PORT", 8098, 1024, 65535),
        token_path=DATA_DIR / os.getenv("TWITCH_TOKEN_FILE", "twitch_tokens.json").strip(),
        tunables_path=DATA_DIR / os.getenv("TWITCH_TUNABLES_FILE", "tunables.json").strip(),
        toggles_path=DATA_DIR / os.getenv("TWITCH_TOGGLES_FILE", "toggles.json").strip(),
        blocklist_path=DATA_DIR / os.getenv("TWITCH_BLOCKLIST_FILE", "blocklist.json").strip(),
        queue_state_path=DATA_DIR / os.getenv("TWITCH_QUEUE_STATE_FILE", "queue_state.json").strip(),
        ytdlp_cookies_file=cookies_path,
        ytdlp_js_runtime_path=js_runtime_path,
        ytdlp_js_runtime_name=js_runtime_name,
        # The most extraction processes that may run at once. They are started
        # on demand and exit when idle (YTDLP_WORKER_IDLE_SECONDS), so this
        # costs memory only while requests are actually in flight.
        ytdlp_concurrency=_clamped_int_env("YTDLP_CONCURRENCY", 2, 1, 8),
        ytdlp_extract_timeout_seconds=_clamped_int_env("YTDLP_EXTRACT_TIMEOUT_SECONDS", 45, 10, 120),
        ytdlp_player_client=ytdlp_player_client,
        # Skips the player's second extraction (chat resolves once to queue,
        # then re-resolves right before playing) for anything near the front
        # of the queue. 0 disables caching. Raised from 300: a cached Track
        # is a few KB, so a bigger window costs nothing on 32GB while
        # cutting repeat-request resolve work.
        ytdlp_cache_ttl_seconds=_clamped_int_env("YTDLP_CACHE_TTL_SECONDS", 900, 0, 3600),
        # Points yt-dlp's PO-token plugin at a bgutil-ytdlp-pot-provider
        # instance, if one's set up. None is a no-op.
        ytdlp_pot_provider_url=os.getenv("YTDLP_POT_PROVIDER_URL", "").strip() or None,
        ytdlp_worker_idle_seconds=_clamped_int_env("YTDLP_WORKER_IDLE_SECONDS", 120, 15, 3600),
        log_level=_log_level_env("LOG_LEVEL", "INFO"),
        log_to_file=_bool_env("LOG_TO_FILE", True),
        log_dir=LOG_DIR,
    )


def token_status(
    bot_id: str | None, owner_id: str | None, token_path: Path | None = None
) -> dict[str, object]:
    """Which of the two OAuth authorizations exist on disk.

    twitchio's token file is a JSON object keyed by Twitch user ID (verified
    in twitchio/authentication/tokens.py: `_tokens[user_id] = {...}`), so
    membership of the two configured IDs is exactly "did each account
    authorize". The tokens themselves are never read out."""
    path = token_path or (DATA_DIR / os.getenv("TWITCH_TOKEN_FILE", "twitch_tokens.json").strip())
    saved: set[str] = set()
    readable = True
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                saved = {str(k) for k in loaded}
            else:
                readable = False
        except (OSError, ValueError):
            readable = False
    return {
        "path": str(path),
        "file_exists": path.is_file(),
        "readable": readable,
        "bot": bool(bot_id) and bot_id in saved,
        "owner": bool(owner_id) and owner_id in saved,
    }
