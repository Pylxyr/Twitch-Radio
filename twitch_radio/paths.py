"""Where things live, for both a source checkout and a packaged app.

Stdlib only, and imported by config.py before anything else, so it must not
import any other twitch_radio module.

Two different roots exist once the bot is frozen into an executable:

* resource_dir() - read-only files shipped inside the app (logo, .env
  template, the overlay's static files). In a PyInstaller build this is the
  bundle directory.
* home_dir()     - the writable folder holding .env, data/ and logs/. It must
  survive updates and be writable without admin rights, so a packaged build
  uses %APPDATA%\\TwitchRadio rather than the install folder.

From a source checkout both are the project root, exactly as before, so
run.bat / setup.bat keep working unchanged. TWITCH_RADIO_HOME overrides the
writable root in either case (the desktop app sets it).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

APP_DIR_NAME = "TwitchRadio"

# subprocess.CREATE_NO_WINDOW, spelled out so this module imports on any OS.
_CREATE_NO_WINDOW = 0x08000000


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def source_root() -> Path:
    """The project checkout (only meaningful when not frozen)."""
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return source_root()


def home_dir() -> Path:
    override = os.environ.get("TWITCH_RADIO_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        base = os.environ.get("APPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home())
        return Path(base) / APP_DIR_NAME
    return source_root()


def env_template_path() -> Path:
    return resource_dir() / ".env.example"


def bin_dirs() -> list[Path]:
    """Folders that may hold bundled ffmpeg / deno, most specific first."""
    candidates: list[Path] = []
    override = os.environ.get("TWITCH_RADIO_BIN", "").strip()
    if override:
        candidates.append(Path(override))
    candidates.append(resource_dir() / "bin")
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "bin")
        candidates.append(exe_dir.parent / "bin")
        candidates.append(exe_dir)
    seen: set[str] = set()
    result: list[Path] = []
    for path in candidates:
        key = os.path.normcase(str(path))
        if key not in seen and path.is_dir():
            seen.add(key)
            result.append(path)
    return result


def bundled_tool_path(name: str) -> Path | None:
    """The bundled `<name>.exe` (or `<name>` off Windows), if a bin folder has it."""
    filename = f"{name}.exe" if os.name == "nt" else name
    for directory in bin_dirs():
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


# Where Linux distributions keep the system's trusted root certificates (one PEM bundle).
_SYSTEM_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Alpine, Gentoo
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # Fedora, RHEL (extracted)
    "/etc/ssl/cert.pem",  # Alpine, macOS-style
)


def point_tls_at_system_certificates() -> str | None:
    """Linux only: tells the bundled ffmpeg where this machine's trusted root
    certificates are, through SSL_CERT_FILE, and returns the file used.

    The bundled ffmpeg has OpenSSL linked in and checks the certificate of every
    HTTPS stream it opens. OpenSSL looks for roots in a folder fixed when it was
    built (the build machine's, /usr/lib/ssl), which does not exist on every
    distribution - on Arch, say, every stream would fail to verify. Setting
    SSL_CERT_FILE (which OpenSSL honours) fixes that without turning checking
    off. Left alone when the user already set SSL_CERT_FILE or SSL_CERT_DIR, and a
    no-op off Linux (Windows' ffmpeg uses the Windows certificate store)."""
    if not sys.platform.startswith("linux"):
        return None
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return None
    for candidate in _SYSTEM_CA_BUNDLES:
        if os.path.isfile(candidate):
            os.environ["SSL_CERT_FILE"] = candidate
            return candidate
    return None


def prepend_bundled_bins_to_path() -> list[Path]:
    """Prepares the environment for the bundled tools: puts their folders at the
    front of PATH for this process and every child it spawns (ffmpeg, yt-dlp's
    deno lookup), and on Linux points TLS at the system certificates (see
    point_tls_at_system_certificates). Idempotent."""
    point_tls_at_system_certificates()
    dirs = bin_dirs()
    if not dirs:
        return []
    current = os.environ.get("PATH", "")
    existing = {os.path.normcase(p) for p in current.split(os.pathsep) if p}
    fresh = [str(d) for d in dirs if os.path.normcase(str(d)) not in existing]
    if fresh:
        os.environ["PATH"] = os.pathsep.join([*fresh, current]) if current else os.pathsep.join(fresh)
    return dirs


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Keyword arguments that stop a console child from flashing a window.

    Needed because the packaged core runs with no console of its own (the
    desktop app starts it hidden), and a console program launched from a
    console-less parent otherwise opens its own visible window - one flash per
    ffmpeg / worker spawn. A no-op everywhere but Windows."""
    if os.name == "nt":
        return {"creationflags": _CREATE_NO_WINDOW}
    return {}
