from __future__ import annotations

import base64
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image as PILImage

from app.routes import workflows
from app.workflow_services import serialization


def test_serialization_helpers_remain_route_compatible() -> None:
    names = (
        "_http",
        "_now",
        "_dedupe_nonempty",
        "_accessory_preview_request_key",
        "_clean_optional_text",
        "_clean_style_tags",
        "_clean_string_list",
        "_safe_datetime",
        "_dict_or_empty",
        "_encode_workflow_cursor",
        "_decode_workflow_cursor",
        "_iso_now",
        "_storage_root",
        "_storage_path",
        "_showcase_gpt55_reference_data_url",
    )
    for name in names:
        assert getattr(workflows, name) is getattr(serialization, name)
    assert (
        workflows._SHOWCASE_GPT55_REFERENCE_MAX_BYTES
        == serialization._SHOWCASE_GPT55_REFERENCE_MAX_BYTES
    )
    assert workflows._WORKFLOW_CURSOR_VERSION == serialization._WORKFLOW_CURSOR_VERSION


def test_cleaning_helpers_preserve_order_limits_and_truncation() -> None:
    assert serialization._dedupe_nonempty([" a ", "", "a", "b"]) == ["a", "b"]
    assert serialization._clean_optional_text("  abc  ", max_len=2) == "ab"
    assert serialization._clean_optional_text("  ") is None
    assert serialization._clean_style_tags(
        [" one ", "one", "two", *[f"tag-{index}" for index in range(20)]]
    ) == ["one", "two", *[f"tag-{index}" for index in range(10)]]
    assert serialization._clean_string_list(
        [" first ", "first", "second", "third"],
        max_items=2,
        max_len=4,
    ) == ["firs", "seco"]
    assert serialization._dict_or_empty({"ok": True}) == {"ok": True}
    assert serialization._dict_or_empty([]) == {}


def test_datetime_and_cursor_round_trip() -> None:
    naive = datetime(2026, 7, 11, 12, 30)
    run = SimpleNamespace(id="run-1", updated_at=naive)
    cursor = serialization._encode_workflow_cursor(  # type: ignore[arg-type]
        run,
        workflow_type="apparel",
    )
    decoded = serialization._decode_workflow_cursor(
        cursor,
        workflow_type="apparel",
    )
    assert decoded == (naive.replace(tzinfo=timezone.utc), "run-1")
    assert serialization._safe_datetime("2026-07-11T12:30:00Z") == naive.replace(
        tzinfo=timezone.utc
    )
    assert serialization._safe_datetime("invalid") is None


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 999, "type": "", "id": "run-1", "updated_at": "2026-07-11T00:00:00Z"},
        {"v": 1, "type": "wrong", "id": "run-1", "updated_at": "2026-07-11T00:00:00Z"},
        {"v": 1, "type": "", "id": "", "updated_at": "2026-07-11T00:00:00Z"},
        {"v": 1, "type": "", "id": "run-1", "updated_at": "2026-07-11T00:00:00"},
    ],
)
def test_cursor_rejects_invalid_payloads(payload: dict[str, object]) -> None:
    raw = json.dumps(payload).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(HTTPException) as excinfo:
        serialization._decode_workflow_cursor(cursor, workflow_type=None)
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_cursor"


def test_storage_path_stays_inside_configured_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(serialization.settings, "storage_root", str(tmp_path))
    assert (
        serialization._storage_path("nested/file.json")
        == (tmp_path / "nested" / "file.json").resolve()
    )
    for invalid in ("", "\x00", "../escape", str((tmp_path / "absolute").resolve())):
        with pytest.raises(HTTPException):
            serialization._storage_path(invalid)


def test_accessory_preview_key_is_stable() -> None:
    first = serialization._accessory_preview_request_key(
        candidate_id="candidate-1",
        accessory_plan={"items": ["hat"], "enabled": True},
        style_prompt="  casual  ",
    )
    second = serialization._accessory_preview_request_key(
        candidate_id="candidate-1",
        accessory_plan={"enabled": True, "items": ["hat"]},
        style_prompt="casual",
    )
    assert first == second
    assert len(first) == 24


def test_reference_data_url_flattens_alpha_and_rejects_invalid_bytes() -> None:
    buffer = io.BytesIO()
    PILImage.new("RGBA", (1800, 1200), (255, 0, 0, 128)).save(
        buffer,
        format="PNG",
    )
    image = SimpleNamespace(id="image-1")
    data_url = serialization._showcase_gpt55_reference_data_url(  # type: ignore[arg-type]
        image,
        buffer.getvalue(),
    )
    assert data_url is not None
    payload = base64.b64decode(data_url.split(",", 1)[1], validate=True)
    assert len(payload) <= serialization._SHOWCASE_GPT55_REFERENCE_MAX_BYTES
    with PILImage.open(io.BytesIO(payload)) as flattened:
        assert flattened.mode == "RGB"
    assert (
        serialization._showcase_gpt55_reference_data_url(  # type: ignore[arg-type]
            image,
            b"not-an-image",
        )
        is None
    )
