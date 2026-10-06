"""Reads and edits the .env file without destroying it.

The desktop app's Settings tab writes here. Editing in place (rather than
regenerating the file) keeps every explanatory comment in the template and any
line the app doesn't know about. Stdlib + python-dotenv only.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from dotenv import dotenv_values

_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_NEEDS_QUOTES = re.compile(r"[\s#\"'\\$`]")
_APP_SECTION = "# --- Added by the Twitch Radio app ---"


def read_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        # interpolate=False: dotenv would otherwise expand ${VAR} even inside quotes.
        raw = dotenv_values(path, encoding="utf-8", interpolate=False)
    except OSError:
        return {}
    return {k: v for k, v in raw.items() if v is not None}


def _format_value(value: str) -> str:
    if value == "":
        return ""
    if not _NEEDS_QUOTES.search(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        # newline="" so the CRLF / LF choice made below is written untouched.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
        raise


def _write_private(path: Path, text: str) -> None:
    """Writes `text` to a file only the owner can read (the .env holds the Twitch
    client secret, and so does its backup). The mode applies when the file is
    created, so it is also set explicitly for a backup left by an older version.
    Windows ignores POSIX modes; there the user profile's own ACLs apply."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def update_values(path: Path, updates: dict[str, str]) -> None:
    """Sets each key, in place if the file already assigns it, else appended.

    A blank value is written as `KEY=`, which config.py already treats as
    "unset, use the default". A `# KEY=...` example line is left alone (it is
    a comment, not an assignment), so the documentation next to it survives.
    The previous file is kept once as `<name>.bak`."""
    for key in updates:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid setting name: {key!r}")
        if "\n" in updates[key] or "\r" in updates[key]:
            raise ValueError(f"{key}: a value can't contain a line break.")

    original = ""
    if path.is_file():
        # read_bytes, not read_text: text mode would fold CRLF into LF and the
        # file's line-ending style could no longer be detected (or kept).
        original = path.read_bytes().decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()

    pending = dict(updates)
    for index, line in enumerate(lines):
        match = _KEY_RE.match(line)
        if match and match.group(1) in pending:
            key = match.group(1)
            lines[index] = f"{key}={_format_value(pending.pop(key))}"

    # Not assigned yet. Blank means "leave it at the default", which an absent
    # line already is, so there is nothing to write.
    pending = {key: value for key, value in pending.items() if value != ""}
    for key in list(pending):
        # Sit right under the template's commented example, if it has one, so
        # the explanation and the value stay together.
        example = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
        anchor = next((i for i, line in enumerate(lines) if example.match(line)), None)
        if anchor is not None:
            lines.insert(anchor + 1, f"{key}={_format_value(pending.pop(key))}")

    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        if not any(line.strip() == _APP_SECTION for line in lines):
            lines.append(_APP_SECTION)
        for key, value in pending.items():
            lines.append(f"{key}={_format_value(value)}")

    text = newline.join(lines) + newline
    if path.is_file():
        with contextlib.suppress(OSError):
            _write_private(path.with_name(path.name + ".bak"), original)
    _atomic_write(path, text)
