import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from twitch_radio import ytdlp_loader as loader
from twitch_radio import ytdlp_update as upd

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TWITCH_RADIO_HOME", str(tmp_path))
    monkeypatch.setattr(loader, "bundled_version", lambda: "2026.08.19")
    return tmp_path


def make_artifact(version: str, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("yt_dlp/__init__.py", "")
        archive.writestr("yt_dlp/version.py", f"__version__ = '{version}'\n")
        archive.writestr("yt_dlp_ejs/__init__.py", "")
        archive.writestr("__main__.py", "")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def make_net(tag: str, artifact: bytes, digest: str | None = None) -> upd.Net:
    sums = f"{digest or hashlib.sha256(artifact).hexdigest()}  yt-dlp\n{'0' * 64}  yt-dlp.exe\n"

    def get(url: str, _max: int) -> bytes:
        upd.check_url(url)
        if url.endswith("SHA2-256SUMS"):
            return sums.encode()
        if url.endswith("/yt-dlp"):
            return artifact
        raise AssertionError(url)

    return upd.Net(get=get, latest_tag=lambda: tag)


def ok_selftest(directory: Path) -> dict[str, object]:
    return {"ok": True, "version": loader.version_in_dir(directory)}


# -- version compare ------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("2026.08.19", "2026.8.19", 0),
        ("2026.08.20", "2026.08.19", 1),
        ("2026.08.19", "2026.08.19.123456", -1),
        ("2026.08.19.2", "2026.08.19.10", -1),
        ("2027.01.01", "2026.12.31", 1),
        ("2026.09.01", "2026.8.31", 1),
    ],
)
def test_compare_versions(a: str, b: str, expected: int) -> None:
    assert loader.compare_versions(a, b) == expected
    assert loader.compare_versions(b, a) == -expected


@pytest.mark.parametrize(
    "bad", ["", "latest", "1.2", "2026.08", "2026.08.19-rc1", "v2026.08.19", "2026.08.19.x"]
)
def test_parse_version_rejects_non_release_strings(bad: str) -> None:
    with pytest.raises(ValueError):
        loader.parse_version(bad)


# -- checksums ------------------------------------------------------------


def test_parse_sums() -> None:
    digest = "a" * 64
    parsed = upd.parse_sums(f"{digest}  yt-dlp\n{'B' * 64} *yt-dlp.exe\nnot a line\nshort  x\n")
    assert parsed == {"yt-dlp": digest, "yt-dlp.exe": "b" * 64}


def test_verify_sha256_accepts_and_rejects() -> None:
    data = b"hello"
    upd.verify_sha256(data, hashlib.sha256(data).hexdigest())
    upd.verify_sha256(data, hashlib.sha256(data).hexdigest().upper())
    with pytest.raises(upd.UpdateError, match="SHA-256"):
        upd.verify_sha256(data, "0" * 64)


def test_checksum_mismatch_installs_nothing_and_unpacks_nothing(home: Path) -> None:
    artifact = make_artifact("2026.09.01")
    result = upd.install(make_net("2026.09.01", artifact, digest="0" * 64), selftest=ok_selftest)
    assert result["ok"] is False
    assert "SHA-256" in result["error"]
    assert not loader.current_dir().exists()
    assert not loader.staging_dir().exists()


def test_missing_checksum_line_is_an_error(home: Path) -> None:
    net = make_net("2026.09.01", make_artifact("2026.09.01"))
    net.get = lambda url, _m: b"" if url.endswith("SHA2-256SUMS") else b"x"
    assert upd.install(net, selftest=ok_selftest)["ok"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x",
        "https://example.com/yt-dlp",
        "https://github.com.evil.example/x",
        "ftp://github.com/x",
    ],
)
def test_only_https_github_urls_are_allowed(url: str) -> None:
    with pytest.raises(upd.UpdateError):
        upd.check_url(url)


def test_github_release_hosts_are_allowed() -> None:
    for url in (
        upd.LATEST_URL,
        "https://release-assets.githubusercontent.com/a",
        "https://objects.githubusercontent.com/a",
    ):
        upd.check_url(url)


# -- extraction safety ----------------------------------------------------


@pytest.mark.parametrize(
    "name", ["yt_dlp/../evil.py", "yt_dlp/a/../../evil.py", "yt_dlp/C:/evil.py", "yt_dlp/a\\..\\evil.py"]
)
def test_unsafe_archive_paths_are_rejected(tmp_path: Path, name: str) -> None:
    artifact = make_artifact("2026.09.01", {name: b"x"})
    with pytest.raises(upd.UpdateError):
        upd.extract_yt_dlp(artifact, tmp_path / "out")
    assert not (tmp_path / "evil.py").exists()


def test_entries_outside_yt_dlp_are_ignored_not_extracted(tmp_path: Path) -> None:
    artifact = make_artifact("2026.09.01", {"../outside.py": b"x", "yt_dlp\\odd.py": b"x"})
    upd.extract_yt_dlp(artifact, tmp_path / "out")
    assert not (tmp_path / "outside.py").exists()
    assert not any("odd" in path.name for path in (tmp_path / "out").rglob("*"))


def test_only_the_yt_dlp_package_is_extracted(tmp_path: Path) -> None:
    count = upd.extract_yt_dlp(make_artifact("2026.09.01"), tmp_path / "out")
    assert count == 2
    assert not (tmp_path / "out" / "yt_dlp_ejs").exists()
    assert not (tmp_path / "out" / "__main__.py").exists()


def test_not_a_zip_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(upd.UpdateError):
        upd.extract_yt_dlp(b"not a zip", tmp_path / "out")


# -- install, keep previous, rollback ------------------------------------


def test_install_then_update_keeps_one_previous(home: Path) -> None:
    first = upd.install(make_net("2026.09.01", make_artifact("2026.09.01")), selftest=ok_selftest)
    assert first["ok"] and first["installed"] == "2026.09.01"
    assert loader.version_in_dir(loader.current_dir()) == "2026.09.01"
    assert not loader.previous_dir().exists()

    second = upd.install(make_net("2026.10.01", make_artifact("2026.10.01")), selftest=ok_selftest)
    assert second["ok"] and second["previous"] == "2026.09.01"
    assert loader.version_in_dir(loader.current_dir()) == "2026.10.01"
    assert loader.version_in_dir(loader.previous_dir()) == "2026.09.01"

    third = upd.install(make_net("2026.11.01", make_artifact("2026.11.01")), selftest=ok_selftest)
    assert third["ok"]
    assert loader.version_in_dir(loader.previous_dir()) == "2026.10.01"  # only one generation kept
    state = loader.read_state()
    assert state["version"] == "2026.11.01" and state["previous_version"] == "2026.10.01"


def test_failed_selftest_leaves_the_working_copy_untouched(home: Path) -> None:
    upd.install(make_net("2026.09.01", make_artifact("2026.09.01")), selftest=ok_selftest)
    result = upd.install(
        make_net("2026.10.01", make_artifact("2026.10.01")),
        selftest=lambda _d: {"ok": False, "error": "ImportError: boom"},
    )
    assert result["ok"] is False and "boom" in result["error"]
    assert loader.version_in_dir(loader.current_dir()) == "2026.09.01"
    assert not loader.staging_dir().exists()


def test_swap_failure_puts_the_old_copy_back(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upd.install(make_net("2026.09.01", make_artifact("2026.09.01")), selftest=ok_selftest)
    real_replace = os.replace

    def flaky(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(src) == loader.staging_dir():
            raise OSError("disk says no")
        real_replace(src, dst)

    monkeypatch.setattr(upd.os, "replace", flaky)
    result = upd.install(make_net("2026.10.01", make_artifact("2026.10.01")), selftest=ok_selftest)
    assert result["ok"] is False
    assert loader.version_in_dir(loader.current_dir()) == "2026.09.01"
    assert loader.read_state()["version"] == "2026.09.01"


def test_already_up_to_date_does_nothing(home: Path) -> None:
    result = upd.install(make_net("2026.08.19", make_artifact("2026.08.19")), selftest=ok_selftest)
    assert result["ok"] is True
    assert not loader.current_dir().exists()


def test_version_mismatch_inside_the_package_is_rejected(home: Path) -> None:
    result = upd.install(make_net("2026.10.01", make_artifact("2026.09.15")), selftest=ok_selftest)
    assert result["ok"] is False
    assert not loader.current_dir().exists()


def test_rollback_swaps_current_and_previous(home: Path) -> None:
    upd.install(make_net("2026.09.01", make_artifact("2026.09.01")), selftest=ok_selftest)
    upd.install(make_net("2026.10.01", make_artifact("2026.10.01")), selftest=ok_selftest)
    result = upd.rollback()
    assert result["ok"] and result["active"] == "2026.09.01"
    assert loader.version_in_dir(loader.current_dir()) == "2026.09.01"
    assert loader.version_in_dir(loader.previous_dir()) == "2026.10.01"
    again = upd.rollback()  # and back
    assert again["ok"] and loader.version_in_dir(loader.current_dir()) == "2026.10.01"


def test_rollback_without_a_previous_copy_fails_cleanly(home: Path) -> None:
    result = upd.rollback()
    assert result["ok"] is False and "previous" in result["error"]


def test_check_reports_availability(home: Path) -> None:
    newer = upd.check(upd.Net(get=lambda *_: b"", latest_tag=lambda: "2026.09.01"))
    assert newer["ok"] and newer["update_available"] and newer["current"] == "2026.08.19"
    same = upd.check(upd.Net(get=lambda *_: b"", latest_tag=lambda: "2026.08.19"))
    assert same["ok"] and not same["update_available"]


def test_check_survives_network_and_garbage_errors(home: Path) -> None:
    def down() -> str:
        raise upd.UpdateError("offline")

    assert upd.check(upd.Net(get=lambda *_: b"", latest_tag=down))["ok"] is False
    garbage = upd.check(upd.Net(get=lambda *_: b"", latest_tag=lambda: "<html>"))
    assert garbage["ok"] is False and garbage["error"]


# -- which copy gets used -------------------------------------------------


def write_override(version: str) -> None:
    package = loader.current_dir() / "yt_dlp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "version.py").write_text(f"__version__ = '{version}'\n", encoding="utf-8")


def test_plan_uses_only_a_newer_override(home: Path) -> None:
    assert loader.plan()["source"] == "bundled"
    write_override("2026.09.01")
    assert (loader.plan()["source"], loader.plan()["version"]) == ("override", "2026.09.01")


@pytest.mark.parametrize("version", ["2026.08.19", "2026.8.19", "2025.01.01"])
def test_plan_ignores_an_override_that_is_not_newer(home: Path, version: str) -> None:
    write_override(version)
    assert loader.plan()["source"] == "bundled"


def test_plan_skips_a_version_that_failed_to_load(home: Path) -> None:
    write_override("2026.09.01")
    loader.write_state({"failed_version": "2026.09.01", "failed_error": "boom"})
    assert loader.plan()["source"] == "bundled"
    assert loader.describe()["override_error"] == "boom"
    write_override_newer = loader.current_dir() / "yt_dlp" / "version.py"
    write_override_newer.write_text("__version__ = '2026.10.01'\n", encoding="utf-8")
    assert loader.plan()["source"] == "override"  # a newer install gets its own chance


def test_can_be_switched_off_by_environment(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_override("2026.09.01")
    monkeypatch.setenv("TWITCH_RADIO_NO_YTDLP_OVERRIDE", "1")
    assert loader.plan()["source"] == "bundled"


def run_import(home: Path) -> dict[str, object]:
    code = "import json; from twitch_radio import ytdlp_loader as l; s=l.import_with_fallback(); import yt_dlp.version as v; print(json.dumps([s, v.__version__]))"
    env = {**os.environ, "TWITCH_RADIO_HOME": str(home), "PYTHONPATH": str(ROOT)}
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True, cwd=ROOT
    )
    status, version = json.loads(completed.stdout.strip().splitlines()[-1])
    return {**status, "version": version}


def test_a_broken_override_falls_back_to_the_bundled_copy(home: Path) -> None:
    pytest.importorskip("yt_dlp")
    bundled = run_import(home)["version"]
    package = loader.current_dir() / "yt_dlp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("raise RuntimeError('broken override')\n", encoding="utf-8")
    (package / "version.py").write_text("__version__ = '2999.01.01'\n", encoding="utf-8")
    # The subprocess' bundled_version() is the real one; 2999 is newer than anything installed.
    outcome = run_import(home)
    assert outcome["source"] == "bundled" and outcome["version"] == bundled
    assert "broken override" in str(outcome["error"])
    assert loader.read_state()["failed_version"] == "2999.01.01"
    assert run_import(home)["source"] == "bundled"  # remembered: not retried every start


def test_a_working_override_is_imported_in_preference(home: Path) -> None:
    yt_dlp = pytest.importorskip("yt_dlp")
    source_pkg = Path(yt_dlp.__file__ or "").parent
    import shutil

    shutil.copytree(source_pkg, loader.current_dir() / "yt_dlp", ignore=shutil.ignore_patterns("__pycache__"))
    (loader.current_dir() / "yt_dlp" / "version.py").write_text(
        (source_pkg / "version.py")
        .read_text(encoding="utf-8")
        .replace(
            next(
                line
                for line in (source_pkg / "version.py").read_text(encoding="utf-8").splitlines()
                if line.startswith("__version__")
            ),
            "__version__ = '2999.01.01'",
        ),
        encoding="utf-8",
    )
    outcome = run_import(home)
    assert outcome["source"] == "override" and outcome["version"] == "2999.01.01"
