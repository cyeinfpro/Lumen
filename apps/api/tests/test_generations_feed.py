from __future__ import annotations

import base64
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.routes import generations
from lumen_core.models import Generation


def test_generation_prompt_search_escapes_like_wildcards() -> None:
    term = "100%_\\path"

    stmt = generations._apply_filters(
        select(Generation),
        user_id="user-1",
        ratio=None,
        has_ref=False,
        fast=False,
        q=term,
    )
    compiled = stmt.compile(dialect=postgresql.dialect())

    assert generations._escape_like_pattern(term) == "100\\%\\_\\\\path"
    assert f"%{generations._escape_like_pattern(term)}%" in compiled.params.values()
    assert " ESCAPE " in str(compiled)


def test_generation_feed_filters_out_deleted_or_archived_conversations() -> None:
    stmt = generations._apply_filters(
        select(Generation),
        user_id="user-1",
        ratio=None,
        has_ref=False,
        fast=False,
        q=None,
    )
    rendered = str(stmt.compile(dialect=postgresql.dialect()))

    assert "JOIN messages" in rendered
    assert "JOIN conversations" in rendered
    assert "messages.deleted_at IS NULL" in rendered
    assert "conversations.deleted_at IS NULL" in rendered
    assert "conversations.archived IS false" in rendered


def test_generation_feed_cursor_carries_total_for_next_page() -> None:
    created_at = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)

    cursor = generations._encode_cursor(created_at, "gen-1", total=123)

    decoded_at, decoded_id, decoded_total = generations._decode_cursor(cursor)
    assert decoded_at == created_at
    assert decoded_id == "gen-1"
    assert decoded_total == 123


def test_generation_feed_cursor_decodes_legacy_v1_without_total() -> None:
    created_at = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    raw = f"v1|{created_at.isoformat()}|gen-1".encode("utf-8")
    cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    decoded_at, decoded_id, decoded_total = generations._decode_cursor(cursor)
    assert decoded_at == created_at
    assert decoded_id == "gen-1"
    assert decoded_total is None


def test_generation_feed_image_schema_exposes_original_mime() -> None:
    image = generations.GenerationImageOut(
        id="img-1",
        url="/api/images/img-1/binary",
        mime="image/jpeg",
        display_url="/api/images/img-1/variants/display2048",
        thumb_url="/api/images/img-1/variants/thumb256",
        width=2560,
        height=3200,
    )

    assert image.model_dump()["mime"] == "image/jpeg"
