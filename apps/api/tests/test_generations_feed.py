from __future__ import annotations

import base64
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.images.application import (
    ImageVisibilityCandidate,
    visible_reference_image_ids_statement,
)
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
    rendered = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "JOIN messages" in rendered
    assert "JOIN conversations" in rendered
    assert "messages.deleted_at IS NULL" in rendered
    assert "conversations.deleted_at IS NULL" in rendered
    assert "conversations.archived IS false" in rendered
    assert "EXISTS (SELECT images.id" in rendered
    assert "images.owner_generation_id = generations.id" in rendered
    assert "images.user_id = 'user-1'" in rendered
    assert "images.deleted_at IS NULL" in rendered
    assert "(generations.upstream_request ->> 'workflow_run_id') IS NULL" in rendered


@pytest.mark.asyncio
async def test_generation_feed_first_image_query_is_scoped_to_feed_user() -> None:
    generation = SimpleNamespace(id="gen-1")

    class Db:
        async def execute(self, statement: Any):
            rendered = str(
                statement.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
            assert "images.user_id = 'user-1'" in rendered

            class Result:
                def scalars(self):
                    return self

                def all(self) -> list[Any]:
                    return []

            return Result()

    images = await generations._feed_images(
        Db(),  # type: ignore[arg-type]
        [generation],  # type: ignore[list-item]
        user_id="user-1",
        visible_after=None,
    )

    assert images == {}


def test_generation_feed_fast_filter_accepts_legacy_true_values() -> None:
    stmt = generations._apply_filters(
        select(Generation),
        user_id="user-1",
        ratio=None,
        has_ref=False,
        fast=True,
        q=None,
    )
    rendered = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert (
        "lower((generations.upstream_request ->> 'fast')) IN ('true', '1')" in rendered
    )


def test_generation_feed_output_treats_string_false_fast_as_disabled() -> None:
    assert generations._bool_option("false") is False
    assert generations._bool_option("0") is False
    assert generations._bool_option("true") is True


def test_generation_feed_cursor_carries_total_and_filter_signature_for_next_page() -> (
    None
):
    created_at = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    filter_sig = generations._feed_filter_signature(
        user_id="user-1",
        ratio="1:1",
        has_ref=True,
        fast=False,
        q="cat",
        visible_after=None,
    )

    cursor = generations._encode_cursor(
        created_at,
        "gen-1",
        total=123,
        filter_sig=filter_sig,
    )

    decoded_at, decoded_id, decoded_total, decoded_filter_sig = (
        generations._decode_cursor(cursor)
    )
    assert decoded_at == created_at
    assert decoded_id == "gen-1"
    assert decoded_total == 123
    assert decoded_filter_sig == filter_sig
    assert (
        generations._validated_cursor_total(
            cursor_total=decoded_total,
            cursor_filter_sig=decoded_filter_sig,
            current_filter_sig=filter_sig,
        )
        == 123
    )


def test_generation_feed_cursor_decodes_legacy_v1_without_total() -> None:
    created_at = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    raw = f"v1|{created_at.isoformat()}|gen-1".encode("utf-8")
    cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    decoded_at, decoded_id, decoded_total, decoded_filter_sig = (
        generations._decode_cursor(cursor)
    )
    assert decoded_at == created_at
    assert decoded_id == "gen-1"
    assert decoded_total is None
    assert decoded_filter_sig is None


def test_generation_feed_legacy_v2_total_is_not_trusted_without_filter_signature() -> (
    None
):
    created_at = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    raw = f"v2|{created_at.isoformat()}|gen-1|123".encode("utf-8")
    cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    _decoded_at, _decoded_id, decoded_total, decoded_filter_sig = (
        generations._decode_cursor(cursor)
    )

    assert decoded_total == 123
    assert decoded_filter_sig is None
    assert (
        generations._validated_cursor_total(
            cursor_total=decoded_total,
            cursor_filter_sig=decoded_filter_sig,
            current_filter_sig="current",
        )
        is None
    )


def test_generation_feed_cursor_rejects_filter_mismatch() -> None:
    with pytest.raises(HTTPException) as excinfo:
        generations._validated_cursor_total(
            cursor_total=123,
            cursor_filter_sig="old-filter",
            current_filter_sig="new-filter",
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_cursor"


def test_generation_feed_cursor_rejects_empty_id() -> None:
    created_at = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    raw = f"v3|{created_at.isoformat()}||123|sig".encode("utf-8")
    cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    with pytest.raises(HTTPException) as excinfo:
        generations._decode_cursor(cursor)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_cursor"


def test_generation_feed_v3_cursor_requires_filter_signature() -> None:
    created_at = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    raw = f"v3|{created_at.isoformat()}|gen-1|123|".encode("utf-8")
    cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    with pytest.raises(HTTPException) as excinfo:
        generations._decode_cursor(cursor)

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_cursor"


def test_generation_feed_image_schema_exposes_original_mime() -> None:
    image = generations.GenerationImageOut(
        id="img-1",
        url="/api/images/img-1/binary",
        mime="image/jpeg",
        display_url="/api/images/img-1/variants/display2048",
        thumb_url="/api/images/img-1/variants/thumb256",
        variant_version="version-1",
        width=2560,
        height=3200,
    )

    assert image.model_dump()["mime"] == "image/jpeg"


def test_generation_feed_variant_state_contract_never_uses_binary_for_grid() -> None:
    generation = SimpleNamespace(
        id="gen-1",
        created_at=datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
        message_id="msg-1",
        prompt="cat",
        aspect_ratio="1:1",
        primary_input_image_id=None,
        input_image_ids=[],
        upstream_request={},
    )
    image = SimpleNamespace(
        id="img-1",
        mime="image/png",
        width=4096,
        height=4096,
        sha256="a" * 64,
    )

    item = generations._feed_item(
        generation,  # type: ignore[arg-type]
        image=image,  # type: ignore[arg-type]
        variant_states=generations.GenerationVariantStates(
            thumb256="missing",
            preview1024="ready",
            display2048="pending",
        ),
        conversation_id="conv-1",
    )

    assert item.image.url == "/api/images/img-1/binary"
    assert item.image.thumb_url is None
    assert item.image.preview_url == "/api/images/img-1/variants/preview1024"
    assert item.image.display_ready is False
    assert item.image.variants.model_dump() == {
        "thumb256": "missing",
        "preview1024": "ready",
        "display2048": "pending",
    }
    assert all(
        "/binary" not in url
        for url in (item.image.thumb_url, item.image.preview_url)
        if url is not None
    )


def test_generation_feed_variant_state_query_includes_claim_readiness() -> None:
    rendered = str(
        generations._feed_variant_states_statement(["img-1"]).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "FROM image_variants" in rendered
    assert "UNION ALL" in rendered
    assert "FROM image_variant_claims" in rendered
    assert "image_variant_claims.error_code IS NOT NULL" in rendered
    assert "NOT (EXISTS" in rendered


def test_byok_visibility_batch_uses_sql_references_without_loading_history() -> None:
    statement = visible_reference_image_ids_statement(
        [
            ImageVisibilityCandidate(
                image_id="img-1",
                owner_generation_id="gen-1",
                created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            ),
            ImageVisibilityCandidate(
                image_id="img-2",
                owner_generation_id=None,
                created_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
            ),
        ],
        user_id="user-1",
        visible_after=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    rendered = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    outer_select = rendered.split("WHERE (EXISTS", maxsplit=1)[0]

    assert "VALUES ('img-1', 'gen-1'), ('img-2', NULL)" in rendered
    assert "= ANY (generations.input_image_ids)" in rendered
    assert "jsonb_path_exists(messages.content" in rendered
    assert "messages.content" not in outer_select
    assert "generations.input_image_ids" not in outer_select


@pytest.mark.asyncio
async def test_generation_feed_empty_race_page_keeps_next_cursor() -> None:
    created_at = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    first = SimpleNamespace(
        id="gen-2",
        created_at=created_at,
        message_id="msg-2",
        prompt="first",
        aspect_ratio="1:1",
        primary_input_image_id=None,
        input_image_ids=[],
        upstream_request={},
    )
    second = SimpleNamespace(
        id="gen-1",
        created_at=created_at.replace(hour=11),
        message_id="msg-1",
        prompt="second",
        aspect_ratio="1:1",
        primary_input_image_id=None,
        input_image_ids=[],
        upstream_request={},
    )

    class Result:
        def __init__(
            self,
            *,
            scalar_value: Any = None,
            rows: list[Any] | None = None,
        ) -> None:
            self.scalar_value = scalar_value
            self.rows = rows or []

        def scalar(self) -> Any:
            return self.scalar_value

        def scalars(self) -> Result:
            return self

        def all(self) -> list[Any]:
            return self.rows

    class Db:
        def __init__(self) -> None:
            self.results = [
                Result(scalar_value=2),
                Result(rows=[first, second]),
                Result(rows=[]),
                Result(rows=[("msg-2", "conv-2")]),
            ]

        async def execute(self, _statement: Any) -> Result:
            return self.results.pop(0)

    out = await generations.list_generation_feed(
        SimpleNamespace(id="user-1", account_mode="wallet"),  # type: ignore[arg-type]
        Db(),  # type: ignore[arg-type]
        limit=1,
    )

    assert out.items == []
    assert out.total == 2
    assert out.next_cursor is not None
    _created_at, generation_id, cursor_total, _filter_sig = generations._decode_cursor(
        out.next_cursor
    )
    assert generation_id == "gen-2"
    assert cursor_total == 2
