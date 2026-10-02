from pathlib import Path

import pytest

from twitch_radio import config, envfile


def test_dollar_brace_round_trips(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    values = {
        "A": "x${HOME}y",
        "B": "$HOME and ${NOPE:-dflt}",
        "C": "${A}",
        "D": "${",
        "E": 'q"uote\\back',
        "F": "a b # c",
    }
    envfile.update_values(path, values)
    assert envfile.read_values(path) == values


def test_format_value_quotes_dollar() -> None:
    assert envfile._format_value("x${HOME}y") == '"x${HOME}y"'
    assert envfile._format_value("plain") == "plain"
    assert envfile._format_value("") == ""


def test_reload_env_does_not_interpolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".env"
    envfile.update_values(path, {"TR_TEST_VALUE": "x${HOME}y"})
    monkeypatch.setattr(config, "ENV_PATH", path)
    monkeypatch.delenv("TR_TEST_VALUE", raising=False)
    config.reload_env()
    try:
        assert config.os.environ["TR_TEST_VALUE"] == "x${HOME}y"
    finally:
        config._FROM_DOTENV.discard("TR_TEST_VALUE")
        config._REAL_ENV.pop("TR_TEST_VALUE", None)
        config.os.environ.pop("TR_TEST_VALUE", None)


def test_crlf_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"# comment\r\nA=1\r\nB=2\r\n")
    envfile.update_values(path, {"A": "10", "C": "3"})
    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert envfile.read_values(path) == {"A": "10", "B": "2", "C": "3"}


def test_lf_stays_lf(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"A=1\n")
    envfile.update_values(path, {"A": "2"})
    assert b"\r" not in path.read_bytes()


def test_bak_keeps_the_previous_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    envfile.update_values(path, {"A": "2"})
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == "A=1\n"
    envfile.update_values(path, {"A": "3"})
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == "A=2\n"


def test_no_bak_for_a_new_file(tmp_path: Path) -> None:
    envfile.update_values(tmp_path / ".env", {"A": "1"})
    assert not (tmp_path / ".env.bak").exists()


def test_value_goes_under_its_commented_example(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# AUDIO_BITRATE_KBPS=160\nOTHER=1\n", encoding="utf-8")
    envfile.update_values(path, {"AUDIO_BITRATE_KBPS": "192"})
    assert path.read_text(encoding="utf-8").splitlines()[:2] == [
        "# AUDIO_BITRATE_KBPS=160",
        "AUDIO_BITRATE_KBPS=192",
    ]


def test_blank_value_for_an_unknown_key_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\n", encoding="utf-8")
    envfile.update_values(path, {"NEW": ""})
    assert "NEW" not in path.read_text(encoding="utf-8")


def test_blank_value_clears_an_existing_assignment(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("A=1\nB=2\n", encoding="utf-8")
    envfile.update_values(path, {"A": ""})
    assert path.read_text(encoding="utf-8").splitlines() == ["A=", "B=2"]


def test_unknown_lines_and_comments_survive(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("# keep me\nCUSTOM=yes\nA=1\n", encoding="utf-8")
    envfile.update_values(path, {"A": "2"})
    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "CUSTOM=yes" in text


@pytest.mark.parametrize("key", ["", "1A", "A B", "A=B", "A\n"])
def test_invalid_names_are_rejected(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError):
        envfile.update_values(tmp_path / ".env", {key: "x"})


def test_line_breaks_in_values_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        envfile.update_values(tmp_path / ".env", {"A": "one\ntwo"})


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    assert envfile.read_values(tmp_path / "nope.env") == {}


def test_bom_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfA=1\n")
    envfile.update_values(path, {"B": "2"})
    assert envfile.read_values(path) == {"A": "1", "B": "2"}
