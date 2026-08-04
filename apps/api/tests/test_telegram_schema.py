from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.routes.telegram import GenerateIn
from lumen_core.constants import MAX_MESSAGE_ATTACHMENTS


def test_telegram_generate_accepts_shared_attachment_limit() -> None:
    body = GenerateIn(
        idempotency_key="tg:attachments",
        prompt="retry with many references",
        attachment_image_ids=[
            f"img-{index}" for index in range(MAX_MESSAGE_ATTACHMENTS)
        ],
    )

    assert len(body.attachment_image_ids) == 16


def test_telegram_generate_rejects_too_many_attachments() -> None:
    with pytest.raises(ValidationError):
        GenerateIn(
            idempotency_key="tg:too-many-attachments",
            prompt="too many references",
            attachment_image_ids=[
                f"img-{index}" for index in range(MAX_MESSAGE_ATTACHMENTS + 1)
            ],
        )


def test_telegram_generate_requires_idempotency_key() -> None:
    with pytest.raises(ValidationError):
        GenerateIn(prompt="missing key")  # type: ignore[call-arg]
