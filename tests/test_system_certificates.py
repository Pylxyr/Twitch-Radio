"""The bundled ffmpeg checks HTTPS certificates with OpenSSL, whose built-in root folder is the
build machine's. paths.point_tls_at_system_certificates() points it at this machine's roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from twitch_radio import paths


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(paths.sys, "platform", "linux")
    return monkeypatch


def test_uses_the_first_bundle_that_exists(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "nope.crt"
    second = tmp_path / "second.crt"
    third = tmp_path / "third.crt"
    second.write_text("x")
    third.write_text("x")
    clean_env.setattr(paths, "_SYSTEM_CA_BUNDLES", (str(missing), str(second), str(third)))
    assert paths.point_tls_at_system_certificates() == str(second)
    assert paths.os.environ["SSL_CERT_FILE"] == str(second)


def test_respects_what_the_user_already_set(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bundle = tmp_path / "ca.crt"
    bundle.write_text("x")
    clean_env.setattr(paths, "_SYSTEM_CA_BUNDLES", (str(bundle),))
    clean_env.setenv("SSL_CERT_DIR", "/somewhere/else")
    assert paths.point_tls_at_system_certificates() is None
    assert "SSL_CERT_FILE" not in paths.os.environ
    clean_env.delenv("SSL_CERT_DIR")
    clean_env.setenv("SSL_CERT_FILE", "/mine.pem")
    assert paths.point_tls_at_system_certificates() is None
    assert paths.os.environ["SSL_CERT_FILE"] == "/mine.pem"


def test_nothing_to_do_without_a_bundle_or_off_linux(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_env.setattr(paths, "_SYSTEM_CA_BUNDLES", (str(tmp_path / "absent.crt"),))
    assert paths.point_tls_at_system_certificates() is None
    assert "SSL_CERT_FILE" not in paths.os.environ

    bundle = tmp_path / "ca.crt"
    bundle.write_text("x")
    clean_env.setattr(paths, "_SYSTEM_CA_BUNDLES", (str(bundle),))
    clean_env.setattr(paths.sys, "platform", "win32")
    assert paths.point_tls_at_system_certificates() is None
    assert "SSL_CERT_FILE" not in paths.os.environ  # Windows' ffmpeg uses the Windows certificate store
