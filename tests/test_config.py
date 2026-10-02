import json
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


def test_source_install_does_not_get_a_bundled_deno_path(settings: config.Settings) -> None:
    assert settings.ytdlp_js_runtime_path is None
    assert settings.ytdlp_js_runtime_name == "deno"


def test_explicit_js_runtime_path_wins(monkeypatch: pytest.MonkeyPatch, settings: config.Settings) -> None:
    monkeypatch.setenv("YTDLP_JS_RUNTIME_PATH", "/opt/deno")
    assert config.load_settings().ytdlp_js_runtime_path == "/opt/deno"


def test_packaged_app_defaults_to_the_bundled_deno(
    monkeypatch: pytest.MonkeyPatch, settings: config.Settings, tmp_path: Path
) -> None:
    deno = tmp_path / "deno"
    monkeypatch.setattr(config, "is_frozen", lambda: True)
    monkeypatch.setattr(config, "bundled_tool_path", lambda name: deno if name == "deno" else None)
    loaded = config.load_settings()
    assert loaded.ytdlp_js_runtime_path == str(deno)
    options = Resolver(loaded)._build_options()
    assert options["js_runtimes"]["deno"]["path"] == str(deno)
    monkeypatch.setenv("YTDLP_JS_RUNTIME_NAME", "node")  # another runtime: don't force the bundled Deno on it
    assert config.load_settings().ytdlp_js_runtime_path is None
