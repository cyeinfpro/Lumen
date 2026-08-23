"""Canonical HMAC capability tokens for Agent tool callbacks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agent_events import AGENT_TOOL_CREATE_IMAGE


AGENT_CAPABILITY_AUDIENCE = "lumen-agent-tools"
AGENT_CAPABILITY_VERSION = 1
AGENT_CAPABILITY_MIN_SECRET_BYTES = 32
AGENT_CAPABILITY_MAX_TOKEN_BYTES = 8192
AGENT_CAPABILITY_MAX_CLOCK_SKEW_SECONDS = 30
AGENT_CAPABILITY_MAX_TTL_SECONDS = 3600
AGENT_CAPABILITY_MAX_REFERENCE_LABELS = 64


class AgentCapabilityClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = AGENT_CAPABILITY_VERSION
    audience: str = AGENT_CAPABILITY_AUDIENCE
    capability_id: str = Field(min_length=16, max_length=96)
    nonce: str = Field(min_length=16, max_length=96)
    run_id: str = Field(min_length=1, max_length=64)
    user_id: str = Field(min_length=1, max_length=64)
    agent_session_id: str = Field(min_length=1, max_length=64)
    execution_epoch: int = Field(ge=0)
    allowed_tools: list[str] = Field(min_length=1, max_length=4)
    allowed_reference_labels: list[str] = Field(
        default_factory=list,
        max_length=AGENT_CAPABILITY_MAX_REFERENCE_LABELS,
    )
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_claim_bindings(self) -> "AgentCapabilityClaims":
        if self.version != AGENT_CAPABILITY_VERSION:
            raise ValueError("unsupported capability version")
        if self.audience != AGENT_CAPABILITY_AUDIENCE:
            raise ValueError("invalid capability audience")
        if self.expires_at <= self.issued_at:
            raise ValueError("capability expiry must follow issuance")
        if self.expires_at - self.issued_at > AGENT_CAPABILITY_MAX_TTL_SECONDS:
            raise ValueError("capability lifetime exceeds the maximum")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools must be unique")
        if any(tool != AGENT_TOOL_CREATE_IMAGE for tool in self.allowed_tools):
            raise ValueError("capability contains an unsupported tool")
        if len(set(self.allowed_reference_labels)) != len(
            self.allowed_reference_labels
        ):
            raise ValueError("allowed_reference_labels must be unique")
        allowed_labels = {
            f"ref_{index}"
            for index in range(1, AGENT_CAPABILITY_MAX_REFERENCE_LABELS + 1)
        }
        if any(label not in allowed_labels for label in self.allowed_reference_labels):
            raise ValueError("allowed_reference_labels contains an invalid label")
        return self


class AgentCapabilityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _secret_bytes(secret: str | bytes) -> bytes:
    value = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    if len(value) < AGENT_CAPABILITY_MIN_SECRET_BYTES:
        raise AgentCapabilityError(
            "agent_capability_unconfigured",
            "agent capability secret is not configured",
        )
    return value


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or any(
        ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for ch in value
    ):
        raise AgentCapabilityError("agent_capability_invalid", "invalid capability")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, base64.binascii.Error) as exc:
        raise AgentCapabilityError(
            "agent_capability_invalid", "invalid capability"
        ) from exc


def _canonical_payload(claims: AgentCapabilityClaims) -> bytes:
    return json.dumps(
        claims.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def issue_agent_capability(
    secret: str | bytes,
    claims: AgentCapabilityClaims,
) -> str:
    key = _secret_bytes(secret)
    payload = _canonical_payload(claims)
    encoded_payload = _b64encode(payload)
    signing_input = f"v1.{encoded_payload}".encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"v1.{encoded_payload}.{_b64encode(signature)}"


def new_agent_capability_nonce() -> str:
    return secrets.token_urlsafe(24)


def verify_agent_capability(
    secret: str | bytes,
    token: str,
    *,
    now: int | None = None,
) -> AgentCapabilityClaims:
    key = _secret_bytes(secret)
    if (
        not isinstance(token, str)
        or len(token.encode("utf-8")) > AGENT_CAPABILITY_MAX_TOKEN_BYTES
    ):
        raise AgentCapabilityError("agent_capability_invalid", "invalid capability")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise AgentCapabilityError("agent_capability_invalid", "invalid capability")
    encoded_payload, encoded_signature = parts[1], parts[2]
    supplied_signature = _b64decode(encoded_signature)
    expected_signature = hmac.new(
        key,
        f"v1.{encoded_payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    if len(supplied_signature) != len(expected_signature) or not hmac.compare_digest(
        supplied_signature,
        expected_signature,
    ):
        raise AgentCapabilityError("agent_capability_invalid", "invalid capability")
    try:
        raw: Any = json.loads(_b64decode(encoded_payload))
        claims = AgentCapabilityClaims.model_validate(raw)
    except AgentCapabilityError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentCapabilityError(
            "agent_capability_invalid", "invalid capability"
        ) from exc
    current = int(time.time()) if now is None else int(now)
    if claims.issued_at > current + AGENT_CAPABILITY_MAX_CLOCK_SKEW_SECONDS:
        raise AgentCapabilityError(
            "agent_capability_not_yet_valid", "capability is not yet valid"
        )
    if claims.expires_at <= current:
        raise AgentCapabilityError("agent_capability_expired", "capability expired")
    return claims


__all__ = [
    "AGENT_CAPABILITY_AUDIENCE",
    "AGENT_CAPABILITY_MAX_CLOCK_SKEW_SECONDS",
    "AGENT_CAPABILITY_MAX_TTL_SECONDS",
    "AGENT_CAPABILITY_MIN_SECRET_BYTES",
    "AGENT_CAPABILITY_VERSION",
    "AgentCapabilityClaims",
    "AgentCapabilityError",
    "issue_agent_capability",
    "new_agent_capability_nonce",
    "verify_agent_capability",
]
