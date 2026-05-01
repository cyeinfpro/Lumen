"""密码哈希、session cookie 签名、CSRF token 生成（DESIGN §9.1/§9.2）。

V1 简化实现：
- 密码用 argon2id。
- Session 不签 JWT；而是用 HMAC-SHA256 签名 `auth_sessions.id`，cookie 存 `{sid}.{sig}`。
  Worker 不需要验证，API 单边校验即可。
- CSRF 用 double-submit：cookie `csrf` 与 header `X-CSRF-Token` 需相等。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import settings


_ph = PasswordHasher()


# 32-bit signed UNIX timestamp upper bound. Any session `exp` >= this value is
# refused to defend against Y2038 truncation and pathological client-supplied
# values that would otherwise make `exp <= int(time.time())` perpetually false
# (i.e. cookies that never expire).
_MAX_SESSION_TIMESTAMP = 2**31 - 1


# ---------- password ----------

def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(hashed: str | None, plain: str) -> bool:
    if not hashed:
        return False
    try:
        return _ph.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False


# ---------- session cookie ----------

def _derive_key(label: str) -> bytes:
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        label.encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _hmac(value: str, *, label: str) -> str:
    return hmac.new(
        _derive_key(label),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_session_cookie(session_id: str) -> str:
    """Produce `<sid>.<exp>.<hmac>`. `sid` is the auth_sessions.id."""
    exp = int(time.time()) + settings.session_ttl_min * 60
    # Cap to a 32-bit signed timestamp so that misconfigured `session_ttl_min`
    # values can never push `exp` past the Y2038 boundary.
    if exp > _MAX_SESSION_TIMESTAMP:
        exp = _MAX_SESSION_TIMESTAMP
    payload = f"{session_id}.{exp}"
    return f"{payload}.{_hmac(payload, label='session-cookie')}"


def parse_session_cookie(raw: str | None) -> str | None:
    """Return `session_id` if signature valid, else None."""
    if not raw or "." not in raw:
        return None
    payload, _, sig = raw.rpartition(".")
    if not payload or not sig:
        return None
    sid, sep, exp_raw = payload.rpartition(".")
    if not sid or not sep or not exp_raw:
        return None
    try:
        exp = int(exp_raw)
    except ValueError:
        return None
    # Reject negative/overflow values so malicious clients cannot supply a
    # huge `exp` (e.g. 99999999999999) that turns the freshness check into a
    # tautology (`exp <= int(time.time())` is always False for such values).
    if exp <= 0 or exp > _MAX_SESSION_TIMESTAMP:
        return None
    if exp <= int(time.time()):
        return None
    expected = _hmac(payload, label="session-cookie")
    if not hmac.compare_digest(sig, expected):
        return None
    return sid


# ---------- csrf ----------

def make_csrf_token(session_id: str, nonce: str | None = None) -> str:
    nonce = nonce or secrets.token_urlsafe(32)
    sig = _hmac(f"{session_id}:{nonce}", label="csrf-token")
    return f"{nonce}.{sig}"


def verify_csrf_token(session_id: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    nonce, _, sig = token.rpartition(".")
    # `secrets.token_urlsafe(32)` produces ~43 chars. Require a generous lower
    # bound so degenerate inputs (empty / very short nonce) cannot collide on
    # a fixed signature across sessions.
    if not nonce or len(nonce) < 20 or not sig:
        return False
    expected = _hmac(f"{session_id}:{nonce}", label="csrf-token")
    return hmac.compare_digest(sig, expected)


def generate_csrf_token(session_id: str) -> str:
    if not session_id:
        raise ValueError("session_id is required for csrf token generation")
    return make_csrf_token(session_id)


# ---------- refresh / session ids ----------

def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """For storing `auth_sessions.refresh_token_hash`. SHA-256 is fine here;
    the token itself is already 48 bytes of entropy."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
