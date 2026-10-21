"""Password hashing with scrypt from the standard library (no extra dependency).

Stored form: ``scrypt$<log2 N>$<r>$<p>$<salt hex>$<hash hex>`` - the parameters travel with the hash,
so they can be raised later without invalidating existing passwords.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_LOG2_N, _R, _P = 14, 8, 1  # ~16 MiB and ~50 ms per hash
_DKLEN = 32
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256  # scrypt cost does not grow with it, but there is no reason to accept megabytes


def _derive(password: str, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << log2_n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=_DKLEN)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt, _LOG2_N, _R, _P)
    return f"scrypt${_LOG2_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        scheme, log2_n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = _derive(password, bytes.fromhex(salt), int(log2_n), int(r), int(p))
        return hmac.compare_digest(candidate, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


# Verified against when the username does not exist, so "no such user" and "wrong password" take equally long.
DUMMY_HASH = hash_password(secrets.token_hex(16))


def check_policy(password: str) -> str | None:
    """None if the password is acceptable, else the reason it is not."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"password must be at most {MAX_PASSWORD_LENGTH} characters"
    return None
