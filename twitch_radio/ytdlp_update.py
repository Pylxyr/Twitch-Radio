"""Updates the yt-dlp copy under <home>/yt-dlp without a new installer.

Flow: latest release tag -> SHA2-256SUMS -> the `yt-dlp` zipimport artifact ->
checksum verified BEFORE anything is unpacked -> only yt_dlp/ extracted into
staging -> the staged copy imported once in a child process -> staging swapped
in for `current`, the old `current` kept as `previous`.

Network access is HTTPS to github.com (and the CDN hosts its release downloads
redirect to) only, and happens in the core process, never in the renderer.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPMessage
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from twitch_radio import ytdlp_loader as loader
from twitch_radio.paths import hidden_subprocess_kwargs, is_frozen, source_root

LATEST_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest"
DOWNLOAD_URL = "https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/{name}"
ARTIFACT_NAME = "yt-dlp"
SUMS_NAME = "SHA2-256SUMS"
ALLOWED_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
MAX_SUMS_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 40 * 1024 * 1024
MAX_UNPACKED_BYTES = 120 * 1024 * 1024
MAX_FILES = 5000
_TIMEOUT_SECONDS = 30
_SELFTEST_TIMEOUT_SECONDS = 90


class UpdateError(Exception):
    pass


def check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise UpdateError(f"Refusing to use {url!r}: only HTTPS downloads from github.com are allowed.")


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> urllib.request.Request | None:
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass
class Net:
    """The two network operations, replaceable in tests."""

    get: Callable[[str, int], bytes]
    latest_tag: Callable[[], str]


def _http_get(url: str, max_bytes: int) -> bytes:
    check_url(url)
    opener = urllib.request.build_opener(_GuardedRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": "TwitchRadio-ytdlp-updater"})
    try:
        with opener.open(request, timeout=_TIMEOUT_SECONDS) as response:
            check_url(response.geturl())
            data: bytes = response.read(max_bytes + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateError(f"Couldn't download {url}: {exc}") from exc
    if len(data) > max_bytes:
        raise UpdateError(f"{url} is larger than the allowed {max_bytes} bytes.")
    return data


def _http_latest_tag() -> str:
    # /releases/latest answers with a redirect to /releases/tag/<tag>; reading
    # the Location avoids the API's anonymous rate limit.
    opener = urllib.request.build_opener(_NoRedirects)
    request = urllib.request.Request(LATEST_URL, headers={"User-Agent": "TwitchRadio-ytdlp-updater"})
    try:
        opener.open(request, timeout=_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        location = exc.headers.get("Location", "") if exc.code in (301, 302, 303, 307, 308) else ""
        if location:
            check_url(location)
            return location.rstrip("/").rsplit("/", 1)[-1]
        raise UpdateError(f"GitHub answered {exc.code} when asking for the latest release.") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateError(f"Couldn't reach GitHub: {exc}") from exc
    raise UpdateError("GitHub didn't say which release is the latest.")


def default_net() -> Net:
    return Net(get=_http_get, latest_tag=_http_latest_tag)


def parse_sums(text: str) -> dict[str, str]:
    """`<sha256>  <name>` lines (sha256sum format, optional `*` before the name)."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0].lower(), parts[1].lstrip("*").strip()
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            sums[name] = digest
    return sums


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_sha256(data: bytes, expected: str, what: str = "download") -> None:
    actual = sha256_hex(data)
    if actual != expected.strip().lower():
        raise UpdateError(
            f"The {what} failed its SHA-256 check (expected {expected[:12]}..., got {actual[:12]}...). Nothing was installed."
        )


def extract_yt_dlp(data: bytes, destination: Path) -> int:
    """Unpacks only the yt_dlp/ package from the zipimport artifact. Returns the
    number of files. Rejects path tricks, symlinks and oversized archives."""
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UpdateError("The yt-dlp download isn't a valid zip file.") from exc
    root = destination.resolve()
    members = [m for m in archive.infolist() if m.filename.startswith("yt_dlp/") and not m.is_dir()]
    if not members or len(members) > MAX_FILES:
        raise UpdateError(f"Unexpected yt-dlp archive layout ({len(members)} files under yt_dlp/).")
    if sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
        raise UpdateError("The unpacked yt-dlp would be unreasonably large.")
    for member in members:
        name = member.filename
        parts = name.split("/")
        # A ":" in any component would be a drive letter or an NTFS stream name on Windows.
        if "\\" in name or name.startswith("/") or ".." in parts or any(":" in part for part in parts):
            raise UpdateError(f"Unsafe path in the yt-dlp archive: {name!r}")
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise UpdateError(f"Symbolic link in the yt-dlp archive: {name!r}")
        target = (root / name).resolve()
        if not target.is_relative_to(root):
            raise UpdateError(f"Unsafe path in the yt-dlp archive: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, open(target, "wb") as sink:
            shutil.copyfileobj(source, sink)
    return len(members)


def selftest_command(directory: Path) -> list[str]:
    if is_frozen():
        return [sys.executable, "--ytdlp-selftest", str(directory)]
    return [sys.executable, str(source_root() / "bot.py"), "--ytdlp-selftest", str(directory)]


def run_selftest(directory: Path) -> dict[str, Any]:
    """Imports the staged yt_dlp in a child process (so a broken one can't take
    this process down) and reports what got imported."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            selftest_command(directory),
            capture_output=True,
            text=True,
            timeout=_SELFTEST_TIMEOUT_SECONDS,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"Couldn't test the downloaded yt-dlp: {exc}") from exc
    for line in reversed((completed.stdout or "").strip().splitlines()):
        try:
            report = json.loads(line)
        except ValueError:
            continue
        if isinstance(report, dict):
            return report
    detail = (completed.stderr or "").strip()[-300:]
    raise UpdateError(
        f"The downloaded yt-dlp didn't start (exit code {completed.returncode}). {detail}".strip()
    )


def _swap_in(staging: Path, current: Path, previous: Path) -> None:
    """staging -> current, current -> previous (one generation kept). If the
    final rename fails, the old current is put back."""
    had_current = current.exists()
    if had_current:
        if previous.exists():
            shutil.rmtree(previous)
        os.replace(current, previous)
    try:
        os.replace(staging, current)
    except OSError:
        if had_current and not current.exists():
            os.replace(previous, current)
        raise


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Log:
    def __init__(self) -> None:
        self.lines: list[dict[str, str]] = []

    def __call__(self, level: str, message: str) -> None:
        self.lines.append({"level": level, "msg": message})


def check(net: Net | None = None) -> dict[str, Any]:
    log = _Log()
    net = net or default_net()
    info = loader.describe()
    result: dict[str, Any] = {
        "ok": False,
        "current": info["version"],
        "source": info["source"],
        "bundled": info["bundled"],
        "previous": info["previous"],
        "override_error": info["override_error"],
        "latest": None,
        "update_available": False,
        "error": None,
    }
    try:
        tag = net.latest_tag()
        loader.parse_version(tag)
        result["latest"] = tag
        current = info["version"]
        result["update_available"] = current is None or loader.compare_versions(tag, current) > 0
        result["ok"] = True
        log("INFO", f"yt-dlp update check: running {current}, latest release {tag}.")
    except (UpdateError, ValueError) as exc:
        result["error"] = str(exc)
        log("WARNING", f"yt-dlp update check failed: {exc}")
    result["log"] = log.lines
    return result


def install(
    net: Net | None = None,
    *,
    selftest: Callable[[Path], dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    log = _Log()
    net = net or default_net()
    selftest = selftest or run_selftest
    staging = loader.staging_dir()
    result: dict[str, Any] = {"ok": False, "installed": None, "previous": None, "error": None}
    try:
        tag = net.latest_tag()
        loader.parse_version(tag)
        current = loader.plan()["version"]
        if not force and current is not None and loader.compare_versions(tag, current) <= 0:
            log("INFO", f"yt-dlp is already up to date ({current}).")
            result.update(ok=True, installed=current)
            return result
        log("INFO", f"Updating yt-dlp from {current} to {tag}.")

        sums = parse_sums(
            net.get(DOWNLOAD_URL.format(tag=tag, name=SUMS_NAME), MAX_SUMS_BYTES).decode("utf-8", "replace")
        )
        expected = sums.get(ARTIFACT_NAME)
        if not expected:
            raise UpdateError(f"{SUMS_NAME} for {tag} doesn't list {ARTIFACT_NAME!r}.")
        data = net.get(DOWNLOAD_URL.format(tag=tag, name=ARTIFACT_NAME), MAX_ARTIFACT_BYTES)
        verify_sha256(data, expected, f"yt-dlp {tag} download")
        log("INFO", f"Checksum OK ({expected[:12]}...).")

        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        count = extract_yt_dlp(data, staging)
        staged = loader.version_in_dir(staging)
        if staged is None or loader.compare_versions(staged, tag) != 0:
            raise UpdateError(f"The package says it is version {staged!r}, not {tag}.")
        report = selftest(staging)
        if not report.get("ok") or report.get("version") != staged:
            raise UpdateError(
                f"The downloaded yt-dlp failed its import test: {report.get('error') or report}"
            )
        log("INFO", f"Unpacked {count} files; import test passed.")

        previous_version = loader.version_in_dir(loader.current_dir())
        _swap_in(staging, loader.current_dir(), loader.previous_dir())
        loader.write_state(
            {
                "version": staged,
                "previous_version": previous_version,
                "installed_at": _now(),
                "failed_version": None,
            }
        )
        result.update(ok=True, installed=staged, previous=previous_version)
        log(
            "INFO",
            f"yt-dlp {staged} installed. The previous version is kept for rollback. Restart the bot to use it.",
        )
    except (UpdateError, OSError, ValueError) as exc:
        result["error"] = str(exc)
        log("ERROR", f"yt-dlp update failed: {exc} The previous version is unchanged.")
    finally:
        with contextlib.suppress(OSError):
            if staging.exists():
                shutil.rmtree(staging)
    result["log"] = log.lines
    return result


def rollback() -> dict[str, Any]:
    """Swaps current and previous: the older copy becomes active again, and the
    one rolled back from stays as `previous` so the move can be undone."""
    log = _Log()
    root = loader.override_root()
    current, previous, spare = loader.current_dir(), loader.previous_dir(), root / "swap"
    result: dict[str, Any] = {"ok": False, "active": None, "error": None}
    try:
        if not previous.exists():
            raise UpdateError("There is no previous yt-dlp version to go back to.")
        old_current = loader.version_in_dir(current)
        restored = loader.version_in_dir(previous)
        if spare.exists():
            shutil.rmtree(spare)
        if current.exists():
            os.replace(current, spare)
        try:
            os.replace(previous, current)
        except OSError:
            if spare.exists() and not current.exists():
                os.replace(spare, current)
            raise
        if spare.exists():
            os.replace(spare, previous)
        loader.write_state(
            {
                "version": restored,
                "previous_version": old_current,
                "installed_at": _now(),
                "failed_version": None,
            }
        )
        result.update(ok=True, active=restored)
        log("INFO", f"Rolled yt-dlp back to {restored}. Restart the bot to use it.")
    except (UpdateError, OSError) as exc:
        result["error"] = str(exc)
        log("ERROR", f"yt-dlp rollback failed: {exc}")
    result["log"] = log.lines
    return result
