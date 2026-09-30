"""Text enhancement input and backwards-compatible request fingerprints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .upstream import EnhancementModel


class EnhanceIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    enhancement_model: EnhancementModel | None = None


def enhance_request_payload(body: BaseModel) -> dict[str, Any]:
    payload = body.model_dump(mode="json")
    # Old requests retain their exact pre-upgrade idempotency fingerprint.
    if payload.get("enhancement_model") is None:
        payload.pop("enhancement_model", None)
    return payload
