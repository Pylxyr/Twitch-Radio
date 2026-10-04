import json
import os
from pathlib import Path

import pytest

from twitch_radio import config
from twitch_radio.extraction import Resolver


def test_token_status_reports_which_accounts_authorized(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"111": {"token": "t", "refresh": "r"}, "222": {}}), encoding="utf-8")
    status = config.token_status("111", "222", path)
    assert (status["bot"], status["owner"], status["readable"], status["file_exists"]) == (
        True,
        True,
        True,
        True,
    )
    only_bot = config.token_status("111", "999", path)
    assert (only_bot["bot"], only_bot["owner"]) == (True, False)


def test_token_status_never_reads_tokens_out(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"111": {"token": "SECRET-TOKEN"}}), encoding="utf-8")
    assert "SECRET-TOKEN" not in json.dumps(config.token_status("111", "222", path))


def test_token_status_missing_file(tmp_path: Path) -> None:
    status = config.token_status("1", "2", tmp_path / "none.json")
    assert (status["file_exists"], status["readable"], status["bot"], status["owner"]) == (
        False,
        True,
        False,
        False,
    )


@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", '"text"'])
def test_token_status_unreadable_or_wrong_shape(tmp_path: Path, content: str) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(content, encoding="utf-8")
    status = config.token_status("1", "2", path)
    assert status["readable"] is False
    assert status["bot"] is False


def test_token_status_blank_ids_never_match(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"": {}}), encoding="utf-8")
    status = config.token_status(None, "", path)
    assert (status["bot"], status["owner"]) == (False, False)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> config.Settings:
    for name, value in {
        "TWITCH_CLIENT_ID": "id",
        "TWITCH_CLIENT_SECRET": "secret",
        "TWITCH_BOT_ID": "1",
        "TWITCH_OWNER_ID": "2",
    }.items():
        monkeypatch.setenv(name, value)
    for name in (
        "YTDLP_CONCURRENCY",
        "YTDLP_COOKIES_FILE",
        "YTDLP_PLAYER_CLIENT",
        "YTDLP_JS_RUNTIME_PATH",
        "YTDLP_JS_RUNTIME_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "reload_env", lambda: None)
    return config.load_settings()


def test_ytdlp_concurrency_defaults_to_two(settings: config.Settings) -> None:
    assert settings.ytdlp_concurrency == 2


def test_remote_components_reach_every_option_set(settings: config.Settings) -> None:
    # The same options dict is sent to the worker processes, so this covers them too.
    resolver = Resolver(settings)
    for options in (
        resolver._get_ytdl_options(fast=False),
        resolver._get_ytdl_options(fast=True),
        resolver._build_options(),
    ):
        assert options["remote_components"] == ["ejs:github"]
        json.dumps(options)  # must survive the JSON hop to a worker
    assert Path(resolver._build_options()["cachedir"]).parent == settings.token_path.parent


def test_source_install_does_not_get_a_bundled_runtime_path(settings: config.Settings) -> None:
    assert settings.ytdlp_js_runtime_path is None
    assert settings.ytdlp_js_runtime_name == "deno"


def test_explicit_js_runtime_path_wins(monkeypatch: pytest.MonkeyPatch, settings: config.Settings) -> None:
    monkeypatch.setenv("YTDLP_JS_RUNTIME_PATH", "/opt/deno")
    assert config.load_settings().ytdlp_js_runtime_path == "/opt/deno"


def test_packaged_app_prefers_a_bundled_deno_when_one_ships(
    monkeypatch: pytest.MonkeyPatch, settings: config.Settings, tmp_path: Path
) -> None:
    deno = tmp_path / "deno"
    host = tmp_path / "Twitch Radio.exe"
    host.write_text("")
    monkeypatch.setattr(config, "is_frozen", lambda: True)
    monkeypatch.setattr(config, "bundled_tool_path", lambda name: deno if name == "deno" else None)
    monkeypatch.setenv(config.HOST_JS_ENV, str(host))
    loaded = config.load_settings()
    assert loaded.ytdlp_js_runtime_path == str(deno)
    options = Resolver(loaded)._build_options()
    assert options["js_runtimes"]["deno"]["path"] == str(deno)
    monkeypatch.setenv("YTDLP_JS_RUNTIME_NAME", "node")  # another runtime: don't force the bundled Deno on it
    assert config.load_settings().ytdlp_js_runtime_path is None


def test_packaged_app_without_deno_uses_the_desktop_app_as_node(
    monkeypatch: pytest.MonkeyPatch, settings: config.Settings, tmp_path: Path
) -> None:
    host = tmp_path / "Twitch Radio.exe"
    host.write_text("")
    monkeypatch.delenv("ELECTRON_RUN_AS_NODE", raising=False)  # so monkeypatch restores it afterwards
    monkeypatch.setattr(config, "is_frozen", lambda: True)
    monkeypatch.setattr(config, "bundled_tool_path", lambda name: None)
    monkeypatch.setenv(config.HOST_JS_ENV, str(host))
    loaded = config.load_settings()
    assert (loaded.ytdlp_js_runtime_name, loaded.ytdlp_js_runtime_path) == ("node", str(host))
    assert os.environ["ELECTRON_RUN_AS_NODE"] == "1"
    assert Resolver(loaded)._build_options()["js_runtimes"] == {"node": {"path": str(host)}}


def test_desktop_app_is_not_used_as_node_when_it_should_not_be(
    monkeypatch: pytest.MonkeyPatch, settings: config.Settings, tmp_path: Path
) -> None:
    host = tmp_path / "Twitch Radio.exe"
    host.write_text("")
    monkeypatch.delenv("ELECTRON_RUN_AS_NODE", raising=False)
    monkeypatch.setattr(config, "bundled_tool_path", lambda name: None)
    monkeypatch.setenv(config.HOST_JS_ENV, str(host))
    # a source install ignores it, even if the variable leaked into the environment
    monkeypatch.setattr(config, "is_frozen", lambda: False)
    assert config.load_settings().ytdlp_js_runtime_path is None
    monkeypatch.setattr(config, "is_frozen", lambda: True)
    # a path that does not exist is ignored rather than trusted
    monkeypatch.setenv(config.HOST_JS_ENV, str(tmp_path / "missing.exe"))
    assert config.load_settings().ytdlp_js_runtime_path is None
    # an explicit choice of runtime or path is never overridden
    monkeypatch.setenv(config.HOST_JS_ENV, str(host))
    monkeypatch.setenv("YTDLP_JS_RUNTIME_PATH", "/opt/deno")
    assert config.load_settings().ytdlp_js_runtime_path == "/opt/deno"
    monkeypatch.delenv("YTDLP_JS_RUNTIME_PATH")
    monkeypatch.setenv("YTDLP_JS_RUNTIME_NAME", "bun")
    assert config.load_settings().ytdlp_js_runtime_path is None
    assert "ELECTRON_RUN_AS_NODE" not in os.environ


def test_solver_hint_only_for_matching_failures_and_only_once(
    settings: config.Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twitch_radio import extraction

    monkeypatch.setattr(extraction.ytdlp_loader, "solver_status", lambda _dir: {"ready": False})
    resolver = Resolver(settings)
    with caplog.at_level("INFO", logger=extraction.log.name):
        resolver._hint_if_solver_missing(Exception("Video unavailable"))
        assert not caplog.records
        resolver._hint_if_solver_missing(Exception("Requested format is not available"))
        resolver._hint_if_solver_missing(Exception("n challenge solving failed"))
    hints = [r for r in caplog.records if "JS solver" in r.getMessage()]
    assert len(hints) == 1 and hints[0].levelname == "WARNING"


def test_solver_hint_is_silent_when_the_solver_is_ready(
    settings: config.Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twitch_radio import extraction

    monkeypatch.setattr(extraction.ytdlp_loader, "solver_status", lambda _dir: {"ready": True})
    with caplog.at_level("INFO", logger=extraction.log.name):
        Resolver(settings)._hint_if_solver_missing(Exception("Requested format is not available"))
    assert not caplog.records


def test_loudness_mode_defaults_to_static(settings: config.Settings) -> None:
    assert settings.loudness_mode == "static"


@pytest.mark.parametrize("raw,expected", [("dynamic", "dynamic"), (" OFF ", "off"), ("Static", "static")])
def test_loudness_mode_accepts_the_three_modes(
    monkeypatch: pytest.MonkeyPatch, settings: config.Settings, raw: str, expected: str
) -> None:
    monkeypatch.setenv("LOUDNESS_MODE", raw)
    assert config.load_settings().loudness_mode == expected


def test_unknown_loudness_mode_falls_back_to_static(
    monkeypatch: pytest.MonkeyPatch, settings: config.Settings
) -> None:
    monkeypatch.setenv("LOUDNESS_MODE", "loud")
    assert config.load_settings().loudness_mode == "static"


def test_worker_idle_seconds_default_and_clamp(
    monkeypatch: pytest.MonkeyPatch, settings: config.Settings
) -> None:
    assert settings.ytdlp_worker_idle_seconds == 120
    monkeypatch.setenv("YTDLP_WORKER_IDLE_SECONDS", "1")
    assert config.load_settings().ytdlp_worker_idle_seconds == 15
    monkeypatch.setenv("YTDLP_WORKER_IDLE_SECONDS", "999999")
    assert config.load_settings().ytdlp_worker_idle_seconds == 3600


def test_removed_hosting_settings_are_gone(settings: config.Settings) -> None:
    for name in (
        "nowplaying_host",
        "settings_password",
        "trusted_proxies",
        "session_hours",
        "ytdlp_worker_mode",
    ):
        assert not hasattr(settings, name), name


def test_env_template_ships_next_to_the_code() -> None:
    from twitch_radio import paths

    assert paths.env_template_path().name == ".env.example"
    assert paths.env_template_path().exists()
