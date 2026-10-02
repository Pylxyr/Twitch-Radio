"""Command-line entry point.

    python bot.py                 run the bot in this console (Ctrl+C stops it)
    python bot.py --check-config  validate .env and exit
    python bot.py --hash-password make a /settings password hash

The desktop app starts the same code with --headless (see headless.py) and
uses --preflight / --hash-password-stdin for its Settings screen. The
lifecycle itself lives in service.py.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import sys

from twitch_radio.admin.passwords import is_password_hash
from twitch_radio.config import Settings, load_settings
from twitch_radio.logbus import LogBus
from twitch_radio.paths import prepend_bundled_bins_to_path
from twitch_radio.service import BotRuntime

# One generation of 5 MB x 3 is days of INFO logging; the file used to grow
# without bound.
_LOG_FILE_BYTES = 5 * 1024 * 1024
_LOG_FILE_BACKUPS = 3


def configure_logging(settings: Settings, *, console: bool = True, bus: LogBus | None = None) -> None:
    handlers: list[logging.Handler] = []
    if console:
        handlers.append(logging.StreamHandler())
    if settings.log_to_file:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                settings.log_dir / "twitch-radio.log",
                maxBytes=_LOG_FILE_BYTES,
                backupCount=_LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
        )
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(formatter)
    root = logging.getLogger()
    for old in list(root.handlers):
        if old is not bus:
            root.removeHandler(old)
    for h in handlers:
        root.addHandler(h)
    if bus is not None and bus not in root.handlers:
        root.addHandler(bus)
    root.setLevel(settings.log_level)
    logging.getLogger("twitchio").setLevel(logging.WARNING)
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def run() -> None:
    # The packaged core re-launches itself as the yt-dlp worker (a frozen exe
    # has no `python -m`); route that before anything else is imported.
    if "--extractor-worker" in sys.argv[1:]:
        from twitch_radio.extractor_worker import main as worker_main

        sys.exit(worker_main())

    parser = argparse.ArgumentParser(prog="twitch-radio")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate .env and exit - doesn't start the bot, spawn ffmpeg, or touch Twitch/yt-dlp.",
    )
    parser.add_argument(
        "--hash-password",
        action="store_true",
        help="Prompt for a /settings password and print the hash to put in TWITCH_SETTINGS_PASSWORD.",
    )
    parser.add_argument("--hash-password-stdin", action="store_true", help="(desktop app) read a password from stdin, print its hash.")
    parser.add_argument("--env-json", action="store_true", help="(desktop app) print the .env values as JSON and exit.")
    parser.add_argument("--env-update-stdin", action="store_true", help="(desktop app) apply a JSON object from stdin to .env.")
    parser.add_argument("--preflight", action="store_true", help="(desktop app) print a JSON health report and exit.")
    parser.add_argument("--headless", action="store_true", help="(desktop app) run with the JSON control channel on stdin/stdout.")
    parser.add_argument("--ytdlp-check", action="store_true", help="(desktop app) look for a newer yt-dlp release, print JSON.")
    parser.add_argument("--ytdlp-install", action="store_true", help="(desktop app) install the latest yt-dlp release, print JSON.")
    parser.add_argument("--ytdlp-rollback", action="store_true", help="(desktop app) go back to the previous yt-dlp copy, print JSON.")
    parser.add_argument("--ytdlp-selftest", nargs="?", const="", metavar="DIR", help="import yt_dlp (from DIR first, if given) and print JSON.")
    parser.add_argument("--resolve-test", metavar="URL", help="resolve one YouTube URL end to end and print JSON (CI smoke test).")
    args = parser.parse_args()

    prepend_bundled_bins_to_path()

    if args.preflight:
        from twitch_radio.preflight import print_report

        sys.exit(print_report())
    if args.ytdlp_check or args.ytdlp_install or args.ytdlp_rollback:
        sys.exit(_ytdlp_update_command(args))
    if args.ytdlp_selftest is not None:
        sys.exit(_ytdlp_selftest(args.ytdlp_selftest))
    if args.resolve_test:
        sys.exit(_resolve_test(args.resolve_test))
    if args.hash_password_stdin:
        sys.exit(_hash_password_stdin())
    if args.env_json:
        sys.exit(_env_json())
    if args.env_update_stdin:
        sys.exit(_env_update_stdin())
    if args.headless:
        import os

        from twitch_radio.headless import run_headless

        code = run_headless()
        # Leave decisively: a wedged yt-dlp thread must not keep a stopped
        # bot's process alive, and everything worth saving is saved by now.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    if args.check_config:
        sys.exit(_check_config())
    if args.hash_password:
        sys.exit(_hash_password())

    settings = _load_settings_or_exit()
    configure_logging(settings)
    try:
        code = asyncio.run(BotRuntime(settings).run())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


def _load_settings_or_exit() -> Settings:
    try:
        return load_settings()
    except RuntimeError as exc:
        sys.exit(f"Config check FAILED: {exc}")


def _hash_password() -> int:
    import getpass

    from twitch_radio.admin.passwords import MAX_PASSWORD_LENGTH, hash_password

    password = getpass.getpass("New /settings password: ")
    if not password:
        print("Nothing entered - aborting.")
        return 1
    if len(password) > MAX_PASSWORD_LENGTH:
        print(f"Too long - the login form accepts at most {MAX_PASSWORD_LENGTH} characters.")
        return 1
    if getpass.getpass("Repeat it: ") != password:
        print("Those didn't match - aborting.")
        return 1
    print("\nPut this line in .env (replacing any existing TWITCH_SETTINGS_PASSWORD), then restart:\n")
    print(f"TWITCH_SETTINGS_PASSWORD={hash_password(password)}")
    return 0


def _hash_password_stdin() -> int:
    """Reads the password from stdin (never argv, which other programs can
    list) and prints only the hash."""
    from twitch_radio.admin.passwords import MAX_PASSWORD_LENGTH, hash_password

    raw = sys.stdin.buffer.readline().decode("utf-8", errors="replace").rstrip("\r\n")
    if not raw:
        print("Nothing entered.", file=sys.stderr)
        return 1
    if len(raw) > MAX_PASSWORD_LENGTH:
        print(f"Too long - at most {MAX_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    print(hash_password(raw))
    return 0


def _ytdlp_update_command(args: argparse.Namespace) -> int:
    import json

    from twitch_radio import ytdlp_update

    if args.ytdlp_install:
        result = ytdlp_update.install()
    elif args.ytdlp_rollback:
        result = ytdlp_update.rollback()
    else:
        result = ytdlp_update.check()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def _ytdlp_selftest(directory: str) -> int:
    import json

    from twitch_radio.ytdlp_loader import purge_yt_dlp_modules

    report: dict[str, object] = {"ok": False}
    if directory:
        # Importing this module already pulled in a yt_dlp; test the staged one instead.
        purge_yt_dlp_modules()
        sys.path.insert(0, directory)
    try:
        import yt_dlp
        import yt_dlp.version

        try:
            import yt_dlp_ejs  # noqa: F401

            ejs_importable = True
        except ImportError:
            ejs_importable = False
        report.update(
            ok=True, version=yt_dlp.version.__version__, file=str(yt_dlp.__file__), yt_dlp_ejs_importable=ejs_importable
        )
    except Exception as exc:  # noqa: BLE001 - the whole point is to report any import failure
        report["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report))
    return 0 if report["ok"] else 1


def _resolve_test(url: str) -> int:
    """Resolves `url` through the real Resolver (worker processes included),
    forcing the full path that needs the JS challenge solver."""
    import json

    from twitch_radio.config import DATA_DIR
    from twitch_radio.extraction import Resolver
    from twitch_radio.ytdlp_loader import CACHE_DIRNAME, solver_status

    settings = _load_settings_or_exit()

    async def go() -> dict[str, object]:
        resolver = Resolver(settings)
        # The fast attempt (no JS) would pass without ever touching the solver.
        resolver._fast_path_enabled = False
        report: dict[str, object] = {"ok": False}
        try:
            track = await resolver.resolve(url, 0)
            report.update(ok=track is not None, title=track.title if track else None, duration=track.duration if track else None)
        except Exception as exc:  # noqa: BLE001
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            await resolver.aclose()
        report["solver"] = solver_status(DATA_DIR / CACHE_DIRNAME)
        return report

    result = asyncio.run(go())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] and result["solver"]["ready"] else 1  # type: ignore[index]


def _env_json() -> int:
    import json

    from twitch_radio import config, envfile

    config.ensure_home()
    print(json.dumps(envfile.read_values(config.ENV_PATH), ensure_ascii=False))
    return 0


def _env_update_stdin() -> int:
    import json

    from twitch_radio import config, envfile

    try:
        updates = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(updates, dict) or not all(isinstance(v, str) for v in updates.values()):
            raise ValueError("expected a JSON object of string values")
        config.ensure_home()
        envfile.update_values(config.ENV_PATH, updates)
    except (ValueError, OSError) as exc:
        print(f"Couldn't save settings: {exc}", file=sys.stderr)
        return 1
    print("ok")
    return 0


def _check_config() -> int:
    import shutil

    try:
        settings = load_settings()
    except RuntimeError as exc:
        print(f"Config check FAILED: {exc}")
        return 1

    if shutil.which("ffmpeg") is None:
        print("Config check FAILED: ffmpeg not found on PATH.")
        return 1

    # Non-fatal. yt-dlp's no-JS fast attempt needs nothing extra, but the
    # fallback resolve needs a real JS runtime on PATH - deno by default, or
    # whatever YTDLP_JS_RUNTIME_NAME/PATH points at instead. (quickjs is NOT
    # bundled with yt-dlp: it is only used if a `qjs` binary is installed.)
    js_runtime = settings.ytdlp_js_runtime_path or settings.ytdlp_js_runtime_name
    if shutil.which(js_runtime) is None:
        print(
            f"WARNING: {js_runtime!r} not found on PATH - yt-dlp needs it to resolve "
            "some YouTube links. Install it, or point YTDLP_JS_RUNTIME_PATH at it in .env."
        )

    # Deliberately never prints client_secret or settings_password.
    print("Config OK:")
    print(f"  Twitch: bot_id={settings.bot_id} owner_id={settings.owner_id} prefix={settings.prefix!r}")
    print(f"  Audio: {settings.audio_bitrate_kbps} kbps, pause_when_no_listeners={settings.pause_when_no_listeners}")
    if settings.settings_password is None:
        login = "no password - /settings is reachable only from this machine, never through a proxy"
    else:
        login = f"password {'hashed' if is_password_hash(settings.settings_password) else 'set (plain text)'}"
    print(f"  HTTP: http://{settings.nowplaying_host}:{settings.nowplaying_port} ({login})")
    print(
        f"  Sessions: {settings.session_hours}h"
        + (f", or {settings.session_remember_days}d with 'keep me signed in'" if settings.session_remember_days else "")
    )
    print(f"  Trusted proxies: {', '.join(str(net) for net in settings.trusted_proxies) or 'none'}")
    token_status = "found" if settings.token_path.exists() else "missing - run OAuth setup before starting"
    print(f"  Token file: {settings.token_path} ({token_status})")
    print(
        f"  yt-dlp: mode={settings.ytdlp_worker_mode} "
        f"concurrency={settings.ytdlp_concurrency} "
        f"timeout={settings.ytdlp_extract_timeout_seconds}s "
        f"cookies={'configured' if settings.ytdlp_cookies_file else 'none'}"
    )
    return 0

