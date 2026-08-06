"""Authentication mapping without FastAPI dependencies."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass

from ..config import ImageJobSettings
from ..domain.identity import CallerIdentity, UpstreamCredential


@dataclass(frozen=True)
class AuthFailure(Exception):
    status_code: int
    detail: str


def _credential(value: str) -> str:
    parts = value.strip().split(None, 1)
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        raise ValueError
    credential = parts[1].strip()
    if not credential or any(char.isspace() for char in credential):
        raise ValueError
    return credential


def credential_hash(authorization: str) -> str:
    return hashlib.sha256(_credential(authorization).encode()).hexdigest()


def authenticate(
    headers: Mapping[str, str],
    settings: ImageJobSettings,
) -> CallerIdentity:
    try:
        incoming = _credential(headers.get("authorization", ""))
    except ValueError:
        raise AuthFailure(401, "Missing Authorization: Bearer token") from None

    expected = settings.sidecar_token.get_secret_value()
    if not expected:
        raise AuthFailure(
            503,
            "image-job service authentication is not configured",
        )
    if hmac.compare_digest(incoming.encode(), expected.encode()):
        authorization = f"Bearer {expected}"
        return CallerIdentity(
            service_id="lumen-worker",
            owner_hash=credential_hash(authorization),
            authorization=authorization,
        )
    raise AuthFailure(401, "Invalid service credentials")


def upstream_credential(
    headers: Mapping[str, str],
) -> UpstreamCredential:
    try:
        credential = _credential(headers.get("x-lumen-upstream-authorization", ""))
    except ValueError:
        raise AuthFailure(
            400,
            "Missing x-lumen-upstream-authorization Bearer credential",
        ) from None
    return UpstreamCredential(f"Bearer {credential}")
