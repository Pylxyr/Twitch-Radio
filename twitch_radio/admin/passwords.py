"""Password storage for the /settings login. Stdlib only.

TWITCH_SETTINGS_PASSWORD is either the password or a hash:
    scrypt:<log2 N>:<r>:<p>:<salt b64url>:<hash b64url>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCHEME = "scrypt"
_LOG2_N = 15
_R = 8
_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_MAX_LOG2_N = 16
_MAX_R = 8
_MAX_P = 4

MAX_PASSWORD_LENGTH = 1024


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes, log2_n: int, r: int, p: int, length: int) -> bytes:
    n = 1 << log2_n
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=256 * n * r
    )


def is_password_hash(stored: str) -> bool:
    return stored.startswith(f"{SCHEME}:")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(password, salt, _LOG2_N, _R, _P, _KEY_BYTES)
    return f"{SCHEME}:{_LOG2_N}:{_R}:{_P}:{_b64(salt)}:{_b64(digest)}"


def _parse_hash(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != SCHEME:
        return None
    try:
        log2_n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except ValueError:
        return None
    if not (10 <= log2_n <= _MAX_LOG2_N and 1 <= r <= _MAX_R and 1 <= p <= _MAX_P):
        return None
    if not salt or not digest:
        return None
    return log2_n, r, p, salt, digest


def validate_stored_password(stored: str) -> str | None:
    if is_password_hash(stored) and _parse_hash(stored) is None:
        return (
            "starts with 'scrypt:' but isn't a valid hash — "
            "regenerate it with `python bot.py --hash-password`"
        )
    return None


def verify_password(candidate: str, stored: str) -> bool:
    """Blocking (~100 ms for a hash); call via asyncio.to_thread."""
    if len(candidate) > MAX_PASSWORD_LENGTH:
        return False
    if is_password_hash(stored):
        parsed = _parse_hash(stored)
        if parsed is None:
            return False
        log2_n, r, p, salt, expected = parsed
        actual = _scrypt(candidate, salt, log2_n, r, p, len(expected))
        return hmac.compare_digest(actual, expected)
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode("utf-8")).digest(),
        hashlib.sha256(stored.encode("utf-8")).digest(),
    )
