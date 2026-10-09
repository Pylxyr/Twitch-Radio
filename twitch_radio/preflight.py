"""One-shot health report for the desktop app's setup checklist.

`bot.py --preflight` prints a single JSON object and exits. It never starts
the bot, spawns ffmpeg, or touches the network - it only looks at the config
and at which tools are installed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any

from twitch_radio import config, ytdlp_loader
from twitch_radio.paths import hidden_subprocess_kwargs, is_frozen, prepend_bundled_bins_to_path
from twitch_radio.version import APP_VERSION

_REQUIRED = ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "TWITCH_BOT_ID", "TWITCH_OWNER_ID")


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _first_line(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = (completed.stdout or completed.stderr or "").strip().splitlines()
    return lines[0].strip() if lines else None


def build_report() -> dict[str, Any]:
    bin_dirs = prepend_bundled_bins_to_path()
    config.ensure_home()
    config.reload_env()

    # Settings warnings go through logging once a root handler exists (see
    # config._warn); collect them instead of letting them reach stdout, which
    # carries this report.
    collector = _Collect()
    root = logging.getLogger()
    root.addHandler(collector)
    previous_level = root.level
    root.setLevel(logging.WARNING)

    report: dict[str, Any] = {
        "version": APP_VERSION,
        "frozen": is_frozen(),
        "home": str(config.BASE_DIR),
        "env_path": str(config.ENV_PATH),
        "data_dir": str(config.DATA_DIR),
        "log_dir": str(config.LOG_DIR),
        "bin_dirs": [str(d) for d in bin_dirs],
    }
    try:
        missing = [name for name in _REQUIRED if not os.getenv(name, "").strip()]
        report["missing"] = missing
        settings = None
        error = None
        try:
            settings = config.load_settings()
        except RuntimeError as exc:
            error = str(exc)
        report["config_ok"] = error is None
        report["config_error"] = error

        js_name = (
            settings.ytdlp_js_runtime_name if settings else os.getenv("YTDLP_JS_RUNTIME_NAME", "deno")
        ) or "deno"
        js_path = (
            settings.ytdlp_js_runtime_path if settings else (os.getenv("YTDLP_JS_RUNTIME_PATH") or "").strip()
        )
        ffmpeg = shutil.which("ffmpeg")
        js = shutil.which(js_path) if js_path else shutil.which(js_name)
        report["ffmpeg"] = {"path": ffmpeg, "version": _first_line([ffmpeg, "-version"]) if ffmpeg else None}
        # The desktop app's executable doubles as Node (with ELECTRON_RUN_AS_NODE=1), but asking it
        # for a version would start a whole Electron process (about 100 MB and a third of a CPU-second)
        # just to print a number nobody needs, so it is never probed.
        is_host = bool(js) and js == os.getenv("TWITCH_RADIO_HOST_JS_EXE")
        probe_ok = bool(js) and not is_host
        report["js_runtime"] = {
            "name": js_name,
            "path": js,
            "version": _first_line([js, "--version"]) if js and probe_ok else None,
        }

        bot_id = settings.bot_id if settings else os.getenv("TWITCH_BOT_ID", "").strip() or None
        owner_id = settings.owner_id if settings else os.getenv("TWITCH_OWNER_ID", "").strip() or None
        token_file = settings.token_path if settings else None
        report["tokens"] = config.token_status(bot_id, owner_id, token_file)
        report["ytdlp"] = ytdlp_loader.describe()
        report["js_solver"] = ytdlp_loader.solver_status(config.DATA_DIR / ytdlp_loader.CACHE_DIRNAME)
        if settings:
            report["http"] = {"port": settings.nowplaying_port}

        def _data_file(env_name: str, default: str) -> str:
            return str(config.DATA_DIR / (os.getenv(env_name, default).strip() or default))

        # Where the live limits / toggles live, for the Settings screen to edit
        # directly (the running bot re-reads them on change).
        report["files"] = {
            "tunables": _data_file("TWITCH_TUNABLES_FILE", "tunables.json"),
            "toggles": _data_file("TWITCH_TOGGLES_FILE", "toggles.json"),
            "radio_lookahead": str(config.DATA_DIR / "radio_lookahead.json"),
        }
        # The one definition of the live limits' ranges and defaults, so the desktop
        # app doesn't keep its own copy that can drift.
        from dataclasses import asdict

        from twitch_radio.tunables import TUNABLE_BOUNDS, TwitchTunables

        report["tunables"] = {
            "bounds": {key: list(bounds) for key, bounds in TUNABLE_BOUNDS.items()},
            "defaults": asdict(TwitchTunables()),
        }
        report["warnings"] = collector.messages
        report["ok"] = bool(report["config_ok"] and ffmpeg)
    finally:
        root.removeHandler(collector)
        root.setLevel(previous_level)
    return report


def print_report() -> int:
    print(json.dumps(build_report(), ensure_ascii=False))
    return 0
