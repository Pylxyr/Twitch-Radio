from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from twitch_radio.admin.app import run_admin_server
from twitch_radio.admin.passwords import is_password_hash
from twitch_radio.player import RadioPlayer
from twitch_radio.chatbot import TwitchChatBot
from twitch_radio.config import Settings, load_settings
from twitch_radio.extraction import Resolver
from twitch_radio.radio import RadioSuggester
from twitch_radio.store import JsonStore
from twitch_radio.toggles import FeatureToggles
from twitch_radio.tunables import TwitchTunables

_bg_tasks: set[asyncio.Task[object]] = set()


def configure_logging(settings: Settings) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if settings.log_to_file:
        handlers.append(logging.FileHandler(settings.log_dir / "twitch-radio.log", encoding="utf-8"))
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(settings.log_level)
    logging.getLogger("twitchio").setLevel(logging.WARNING)
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def _async_run(settings: Settings) -> None:
    configure_logging(settings)
    log = logging.getLogger(__name__)

    resolver = Resolver(settings)
    tunables_store = JsonStore(settings.tunables_path)
    toggles_store = JsonStore(settings.toggles_path)

    # Fire-and-forget: warms up yt-dlp's worker threads (and, once cached,
    # persists across restarts too) before the first real !sr arrives. Never
    # awaited inline — must not delay startup — and warm_up() is non-fatal
    # on failure, so this is pure upside.
    warmup_task = asyncio.create_task(resolver.warm_up(), name="resolver-warmup")
    _bg_tasks.add(warmup_task)
    warmup_task.add_done_callback(_bg_tasks.discard)

    player = RadioPlayer(
        resolver=resolver.resolve,
        audio_bitrate_kbps=settings.audio_bitrate_kbps,
        pause_when_no_listeners=settings.pause_when_no_listeners,
        prefetch_enabled=settings.ytdlp_cache_ttl_seconds > 0,
    )
    radio_suggester = RadioSuggester(resolver)
    player.set_radio_suggester(radio_suggester.suggest)

    async def _radio_enabled() -> bool:
        toggles = FeatureToggles.from_dict(await toggles_store.read())
        return toggles.radio_autoplay_enabled

    player.set_radio_enabled_getter(_radio_enabled)
    # Nested try/finally per resource (not one big try around just the chat
    # bot) so a failure acquiring a *later* resource still tears down
    # everything already acquired.
    player.start()
    try:
        admin_runner = await run_admin_server(
            player=player,
            tunables_store=tunables_store,
            toggles_store=toggles_store,
            settings_password=settings.settings_password,
            broadcast_info={
                "Audio stream": "/stream.mp3",
                "Overlay": "/overlay",
                "Audio bitrate": f"{settings.audio_bitrate_kbps} kbps",
                "Chat command prefix": settings.prefix,
            },
            host=settings.nowplaying_host,
            port=settings.nowplaying_port,
            trusted_proxies=settings.trusted_proxies,
            session_hours=settings.session_hours,
            session_remember_days=settings.session_remember_days,
            allow_open_settings=settings.settings_allow_open,
        )
        try:
            bot = TwitchChatBot(
                client_id=settings.client_id,
                client_secret=settings.client_secret,
                bot_id=settings.bot_id,
                owner_id=settings.owner_id,
                prefix=settings.prefix,
                resolver=resolver,
                player=player,
                tunables_store=tunables_store,
                toggles_store=toggles_store,
                token_storage_path=settings.token_path,
            )
            player.set_track_failure_notifier(bot.announce)

            async def _duration_limit() -> int:
                tunables = TwitchTunables.from_dict(await tunables_store.read())
                return tunables.max_request_duration_seconds

            player.set_duration_limit_getter(_duration_limit)

            loop = asyncio.get_running_loop()

            shutting_down = False

            def _handle_shutdown_signal(signum: int) -> None:
                # systemd sends SIGTERM and an impatient operator adds
                # Ctrl-C — without this guard, both would spawn their own
                # bot.close() and race over the same teardown.
                nonlocal shutting_down
                if shutting_down:
                    log.info("%s received — shutdown already in progress.", signal.Signals(signum).name)
                    return
                shutting_down = True
                log.info("%s received — initiating graceful shutdown.", signal.Signals(signum).name)
                task = asyncio.create_task(bot.close())
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)

            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, _handle_shutdown_signal, sig)

            async with bot:
                await bot.start()
        finally:
            await admin_runner.cleanup()
    finally:
        # Reverse of setup order: the HTTP surface is already down (inner
        # finally above), so nothing can still be serving a request against
        # the player.
        if not warmup_task.done():
            warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await warmup_task
        await player.stop()
        # Async now: with YTDLP_WORKER_MODE=process this reaps the extraction
        # child processes, and leaving those orphaned would keep a wedged
        # yt-dlp/Deno alive past the service stopping.
        await resolver.aclose()


def _load_settings_or_exit() -> Settings:
    import sys

    try:
        return load_settings()
    except RuntimeError as exc:
        sys.exit(f"Config check FAILED: {exc}")


def run() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="twitch-radio-sr-bot")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate .env and exit — doesn't start the bot, spawn ffmpeg, or touch Twitch/yt-dlp.",
    )
    parser.add_argument(
        "--hash-password",
        action="store_true",
        help="Prompt for a /settings password and print the hash to put in TWITCH_SETTINGS_PASSWORD.",
    )
    args = parser.parse_args()

    if args.check_config:
        sys.exit(_check_config())
    if args.hash_password:
        sys.exit(_hash_password())
    asyncio.run(_async_run(_load_settings_or_exit()))


def _hash_password() -> int:
    import getpass

    from twitch_radio.admin.passwords import MAX_PASSWORD_LENGTH, hash_password

    password = getpass.getpass("New /settings password: ")
    if not password:
        print("Nothing entered — aborting.")
        return 1
    if len(password) > MAX_PASSWORD_LENGTH:
        print(f"Too long — the login form accepts at most {MAX_PASSWORD_LENGTH} characters.")
        return 1
    if getpass.getpass("Repeat it: ") != password:
        print("Those didn't match — aborting.")
        return 1
    print("\nPut this line in .env (replacing any existing TWITCH_SETTINGS_PASSWORD), then restart:\n")
    print(f"TWITCH_SETTINGS_PASSWORD={hash_password(password)}")
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

    # Deliberately never prints client_secret or settings_password.
    print("Config OK:")
    print(f"  Twitch: bot_id={settings.bot_id} owner_id={settings.owner_id} prefix={settings.prefix!r}")
    print(f"  Audio: {settings.audio_bitrate_kbps} kbps, pause_when_no_listeners={settings.pause_when_no_listeners}")
    if settings.settings_password is None:
        login = "no password — /settings is reachable only from this machine, never through a proxy"
    else:
        login = f"password {'hashed' if is_password_hash(settings.settings_password) else 'set (plain text)'}"
    print(f"  HTTP: http://{settings.nowplaying_host}:{settings.nowplaying_port} ({login})")
    print(
        f"  Sessions: {settings.session_hours}h"
        + (f", or {settings.session_remember_days}d with 'keep me signed in'" if settings.session_remember_days else "")
    )
    print(f"  Trusted proxies: {', '.join(str(net) for net in settings.trusted_proxies) or 'none'}")
    token_status = "found" if settings.token_path.exists() else "missing — run OAuth setup before starting"
    print(f"  Token file: {settings.token_path} ({token_status})")
    print(
        f"  yt-dlp: mode={settings.ytdlp_worker_mode} "
        f"concurrency={settings.ytdlp_concurrency} "
        f"timeout={settings.ytdlp_extract_timeout_seconds}s "
        f"cookies={'configured' if settings.ytdlp_cookies_file else 'none'}"
    )
    return 0
