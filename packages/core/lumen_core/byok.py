"""BYOK helpers shared by API and Worker.

This module deliberately contains only deterministic helpers: encryption,
key hashing, arithmetic challenge generation, and Responses output parsing.
HTTP calls and database transactions stay in the API/Worker packages.

Hash output format: ``hash_api_key`` / ``hash_verification_token`` return a
versioned string ``"<version>:<hex-digest>"`` (e.g. ``"v1:abc..."``). The
version prefix lets future master-secret / KDF rotations identify legacy
hashes without re-hashing every row.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


BYOK_DEFAULT_VALIDATION_MODEL = "gpt-5.4"
BYOK_DEFAULT_CHAT_MODEL = "gpt-5.4"
BYOK_DEFAULT_FAST_MODEL = "gpt-5.4-mini"
BYOK_DEFAULT_VALIDATION_TIMEOUT_MS = 15_000
BYOK_DEFAULT_PENDING_TOKEN_TTL_SECONDS = 900
BYOK_ENCRYPTION_VERSION = "v1"
BYOK_MAX_API_KEY_LEN = 512
_PROVIDER_PROBE_MODEL = "gpt-5.4-mini"
_PROVIDER_PROBE_INSTRUCTIONS = (
    "You are a precise calculator. Return only the final integer."
)
_PROVIDER_PROBE_INPUT = (
    "What is 99 times 99? Reply with only the integer result, no words, no explanation."
)

# Accept: pure 0, or non-zero-leading 1-3 digits + optional thousands groups
# (each exactly 3 digits, separated by ',' or ' '), or any plain non-zero
# multi-digit integer. Rejects "1,2,3", "1 2 3", "12 34", "1,23".
_ANSWER_RE = re.compile(
    r"^[+-]?(?:0|[1-9]\d{0,2}(?:[, ]\d{3})*|[1-9]\d+)$"
)


class ByokCryptoError(RuntimeError):
    """Raised when a BYOK secret cannot be encrypted or decrypted."""


@dataclass(frozen=True)
class ArithmeticChallenge:
    expression: str
    expected: int
    operands: tuple[int, ...]
    operator: str
    created_at: str

    def as_json(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "expected": self.expected,
            "operands": list(self.operands),
            "operator": self.operator,
            "created_at": self.created_at,
        }


def _derive_key(
    master_secret: str,
    purpose: str,
    version: str = BYOK_ENCRYPTION_VERSION,
) -> bytes:
    secret = (master_secret or "").strip()
    if len(secret) < 32:
        raise ByokCryptoError("BYOK API key master secret must be at least 32 characters")
    return hmac.new(
        secret.encode("utf-8"),
        f"lumen-byok:{version}:{purpose}".encode("utf-8"),
        hashlib.sha256,
    ).digest()


def _new_aesgcm(key: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ByokCryptoError("cryptography package is required for BYOK encryption") from exc
    return AESGCM(key)


def encrypt_api_key(api_key: str, master_secret: str) -> str:
    key = _derive_key(master_secret, "aes-gcm")
    nonce = secrets.token_bytes(12)
    ciphertext = _new_aesgcm(key).encrypt(nonce, api_key.encode("utf-8"), None)
    return (
        f"{BYOK_ENCRYPTION_VERSION}:"
        f"{base64.urlsafe_b64encode(nonce).decode('ascii')}:"
        f"{base64.urlsafe_b64encode(ciphertext).decode('ascii')}"
    )


def decrypt_api_key(ciphertext: str, master_secret: str) -> str:
    try:
        version, raw_nonce, raw_ciphertext = ciphertext.split(":", 2)
    except ValueError as exc:
        raise ByokCryptoError("invalid BYOK ciphertext format") from exc
    if version != BYOK_ENCRYPTION_VERSION:
        raise ByokCryptoError("unsupported BYOK encryption version")
    try:
        nonce = base64.urlsafe_b64decode(raw_nonce.encode("ascii"))
        encrypted = base64.urlsafe_b64decode(raw_ciphertext.encode("ascii"))
        key = _derive_key(master_secret, "aes-gcm", version=version)
        plaintext = _new_aesgcm(key).decrypt(nonce, encrypted, None)
    except ByokCryptoError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ByokCryptoError("failed to decrypt BYOK API key") from exc
    return plaintext.decode("utf-8")


def hash_api_key(api_key: str, master_secret: str) -> str:
    key = _derive_key(master_secret, "hmac")
    digest = hmac.new(key, api_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{BYOK_ENCRYPTION_VERSION}:{digest}"


def api_key_hint(api_key: str) -> str:
    value = api_key.strip()
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def new_verification_token() -> str:
    return secrets.token_urlsafe(32)


def hash_verification_token(token: str, master_secret: str) -> str:
    key = _derive_key(master_secret, "verification-token")
    digest = hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{BYOK_ENCRYPTION_VERSION}:{digest}"


def generate_arithmetic_challenge(
    *,
    now: datetime | None = None,
) -> ArithmeticChallenge:
    """Generate a fresh integer arithmetic challenge.

    The expression is intentionally small enough for cheap validation but random
    enough to avoid fixed-prompt replay or gateway hardcoding.
    """
    op = ("*", "+", "-")[secrets.randbelow(3)]
    if op == "*":
        # 50% two-digit × two-digit, 50% one-digit × three-digit, for
        # broader entropy than a single (11..99) × (11..99) bucket.
        if secrets.randbelow(2) == 0:
            a = 11 + secrets.randbelow(89)
            b = 11 + secrets.randbelow(89)
        else:
            a = 2 + secrets.randbelow(8)
            b = 100 + secrets.randbelow(900)
        expected = a * b
    elif op == "+":
        a = 1_000 + secrets.randbelow(8_000)
        b = 100 + secrets.randbelow(900)
        expected = a + b
    else:
        a = 1_000 + secrets.randbelow(8_000)
        # Avoid b=0 ("1234 - 0") and ensure 1 ≤ b ≤ min(a-1, 8_999).
        b = 1 + secrets.randbelow(min(a - 1, 8_999))
        expected = a - b
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return ArithmeticChallenge(
        expression=f"{a} {op} {b}",
        expected=expected,
        operands=(a, b),
        operator=op,
        created_at=created.isoformat(),
    )


def build_validation_request(
    challenge: ArithmeticChallenge | dict[str, Any],
    *,
    model: str = BYOK_DEFAULT_VALIDATION_MODEL,
) -> dict[str, Any]:
    expression = (
        challenge.expression
        if isinstance(challenge, ArithmeticChallenge)
        else str(challenge.get("expression") or "")
    )
    return {
        "model": model,
        "instructions": (
            "You are a precise calculator. Return only the final integer. "
            "No words, no punctuation, no explanation."
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Calculate {expression}. Return only the integer.",
                    }
                ],
            }
        ],
        "stream": False,
        "store": False,
        "max_output_tokens": 16,
    }


def build_provider_probe_request() -> dict[str, Any]:
    """Build the fixed Responses payload used for provider health probes."""
    return {
        "model": _PROVIDER_PROBE_MODEL,
        "instructions": _PROVIDER_PROBE_INSTRUCTIONS,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": _PROVIDER_PROBE_INPUT,
                    }
                ],
            }
        ],
        "stream": False,
        "store": False,
    }


def extract_response_output_text(payload: object) -> str:
    """Extract text from common OpenAI Responses-compatible JSON shapes."""
    if not isinstance(payload, dict):
        return ""

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    chunks: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text") or part.get("output_text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    if chunks:
        return "".join(chunks)

    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return ""


def extract_sse_output_text(raw: str) -> str:
    chunks: list[str] = []
    buffer = raw.replace("\r\n", "\n")
    for raw_event in buffer.split("\n\n"):
        data_lines: list[str] = []
        for line in raw_event.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        delta = obj.get("delta")
        if isinstance(delta, str) and delta:
            chunks.append(delta)
            continue

        text = obj.get("text") or obj.get("output_text")
        if isinstance(text, str) and text:
            chunks.append(text)
            continue

        for key in ("response", "item", "part"):
            nested = obj.get(key)
            nested_text = extract_response_output_text(nested)
            if nested_text:
                chunks.append(nested_text)
                break
    return "".join(chunks)


def normalize_integer_answer(text: str) -> str | None:
    value = text.strip()
    if not value or not _ANSWER_RE.fullmatch(value):
        return None
    normalized = value.replace(",", "").replace(" ", "")
    if normalized in {"", "+", "-"}:
        return None
    return normalized


def answer_matches_expected(text: str, expected: int) -> bool:
    normalized = normalize_integer_answer(text)
    if normalized is None:
        return False
    try:
        return int(normalized) == int(expected)
    except ValueError:
        return False


def validate_api_key_shape(api_key: str) -> str:
    value = api_key.strip()
    if not value:
        raise ValueError("api_key is required")
    if len(value) > BYOK_MAX_API_KEY_LEN:
        raise ValueError(f"api_key exceeds {BYOK_MAX_API_KEY_LEN} characters")
    return value
