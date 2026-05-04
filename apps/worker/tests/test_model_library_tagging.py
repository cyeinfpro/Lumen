"""模特库 vision 自动打标签 helper 单测。

不打真实上游：仅测试 JSON 解析、字段规整、graceful 失败语义。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

import pytest

os.environ.setdefault(
    "STORAGE_ROOT", f"{tempfile.gettempdir()}/lumen-worker-test-storage"
)

from app.tasks import model_library_tagging as mlt
from app.tasks.model_library_tagging import (
    AutoTagResult,
    _normalize_age_segment,
    _normalize_gender,
    _parse_tagging_payload,
    _strip_markdown_fences,
    auto_tag_image_record,
)


@dataclass
class _Image:
    id: str
    storage_key: str = "u/1/image.png"
    mime: str = "image/png"
    metadata_jsonb: dict[str, Any] = field(default_factory=dict)


def test_strip_markdown_fences_handles_code_block() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert _strip_markdown_fences(raw) == '{"a": 1}'


def test_strip_markdown_fences_passes_through_plain() -> None:
    assert _strip_markdown_fences('{"a": 1}') == '{"a": 1}'


def test_normalize_age_segment_recognizes_aliases() -> None:
    assert _normalize_age_segment("young_adult") == "young_adult"
    assert _normalize_age_segment("YOUNG") == "young_adult"
    assert _normalize_age_segment("kids") == "child"
    assert _normalize_age_segment("middleaged") == "middle_aged"
    assert _normalize_age_segment("garbage") is None
    assert _normalize_age_segment(None) is None


def test_normalize_gender_recognizes_aliases() -> None:
    assert _normalize_gender("Female") == "female"
    assert _normalize_gender("WOMAN") == "female"
    assert _normalize_gender("M") == "male"
    assert _normalize_gender("girl") == "female"
    assert _normalize_gender("xyz") is None
    assert _normalize_gender(123) is None


def test_parse_tagging_payload_full_object() -> None:
    raw = (
        '{"appearance_direction": "european", '
        '"style_tags": ["minimal", "studio"], '
        '"age_segment": "young_adult", '
        '"gender": "female", '
        '"notes": "高级感"}'
    )
    out = _parse_tagging_payload("img-1", raw)
    assert out.image_id == "img-1"
    assert out.appearance_direction == "european"
    assert out.style_tags == ["minimal", "studio"]
    assert out.age_segment == "young_adult"
    assert out.gender == "female"
    assert out.notes == "高级感"


def test_parse_tagging_payload_handles_camel_case_keys() -> None:
    raw = '{"styleTags": ["a"], "ageSegment": "ADULT", "gender": "Male"}'
    out = _parse_tagging_payload("img-2", raw)
    assert out.style_tags == ["a"]
    assert out.age_segment == "adult"
    assert out.gender == "male"


def test_parse_tagging_payload_extracts_json_from_noisy_text() -> None:
    raw = 'OK: {"style_tags": ["x"], "gender": "female"} thanks'
    out = _parse_tagging_payload("img-3", raw)
    assert out.style_tags == ["x"]
    assert out.gender == "female"


def test_parse_tagging_payload_regex_fallback() -> None:
    raw = '"appearance_direction": "asian"\n"gender": "female"'
    out = _parse_tagging_payload("img-4", raw)
    # 没有合法 JSON 包围 -> 走 regex 兜底
    assert out.appearance_direction == "asian"
    assert out.gender == "female"


def test_parse_tagging_payload_returns_empty_on_invalid() -> None:
    out = _parse_tagging_payload("img-5", "completely random unstructured text")
    assert out.image_id == "img-5"
    assert out.style_tags == []
    assert out.appearance_direction is None
    assert out.age_segment is None


def test_parse_tagging_payload_handles_empty_input() -> None:
    out = _parse_tagging_payload("img-6", "")
    assert out == AutoTagResult(image_id="img-6")


@pytest.mark.asyncio
async def test_auto_tag_image_record_returns_empty_when_no_storage_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _Image(id="img-7", storage_key="")
    out = await auto_tag_image_record(record)
    assert out.image_id == "img-7"
    assert out.style_tags == []


@pytest.mark.asyncio
async def test_auto_tag_image_record_returns_empty_when_storage_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_fail(_: str) -> bytes:
        raise OSError("disk gone")

    monkeypatch.setattr(mlt.storage, "aget_bytes", must_fail)
    out = await auto_tag_image_record(_Image(id="img-8"))
    # 读图失败 -> graceful 返回空 result，不 raise
    assert out.image_id == "img-8"
    assert out.style_tags == []


@pytest.mark.asyncio
async def test_auto_tag_image_record_parses_upstream_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_read(_: str) -> bytes:
        return b"png-bytes"

    async def fake_upstream(record: Any, image_url: str, *, model: str) -> str:
        assert record.id == "img-9"
        assert image_url.startswith("data:image/png;base64,")
        return (
            '{"appearance_direction": "asian", "style_tags": ["natural"], '
            '"age_segment": "young_adult", "gender": "female"}'
        )

    monkeypatch.setattr(mlt.storage, "aget_bytes", fake_read)
    monkeypatch.setattr(mlt, "_call_upstream", fake_upstream)
    out = await auto_tag_image_record(_Image(id="img-9"))
    assert out.appearance_direction == "asian"
    assert out.style_tags == ["natural"]
    assert out.age_segment == "young_adult"
    assert out.gender == "female"


@pytest.mark.asyncio
async def test_auto_tag_image_record_graceful_when_upstream_returns_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_read(_: str) -> bytes:
        return b"png-bytes"

    async def fake_upstream(record: Any, image_url: str, *, model: str) -> str:
        return "I cannot parse this image."

    monkeypatch.setattr(mlt.storage, "aget_bytes", fake_read)
    monkeypatch.setattr(mlt, "_call_upstream", fake_upstream)
    out = await auto_tag_image_record(_Image(id="img-10"))
    # 上游成功但输出非 JSON -> 返回空 fields，不 raise
    assert out.image_id == "img-10"
    assert out.style_tags == []
    assert out.appearance_direction is None
