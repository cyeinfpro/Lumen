from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

import pytest

os.environ.setdefault(
    "STORAGE_ROOT", f"{tempfile.gettempdir()}/lumen-worker-test-storage"
)

from app.tasks import context_image_caption


@dataclass
class _ImageRecord:
    id: str
    storage_key: str = "u/1/image.png"
    mime: str = "image/png"
    metadata_jsonb: dict[str, Any] = field(default_factory=dict)


class _Session:
    def __init__(self) -> None:
        self.executed: list[tuple[Any, dict[str, Any]]] = []
        self.flushed = False
        self.commits = 0

    async def execute(self, stmt: Any, params: dict[str, Any]) -> None:
        self.executed.append((stmt, params))

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_ensure_caption_for_image_uses_metadata_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def must_not_read(_: str) -> bytes:
        raise AssertionError("storage should not be read on cache hit")

    monkeypatch.setattr(context_image_caption.storage, "aget_bytes", must_not_read)
    record = _ImageRecord(id="img1", metadata_jsonb={"caption": "cached caption"})

    caption = await context_image_caption.ensure_caption_for_image(
        _Session(), record, model="gpt-vision"
    )

    assert caption == "cached caption"


@pytest.mark.asyncio
async def test_ensure_caption_for_image_writes_metadata_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_read(_: str) -> bytes:
        return b"png-bytes"

    async def fake_upstream(record: Any, image_url: str, *, model: str) -> str:
        assert record.id == "img1"
        assert image_url.startswith("data:image/png;base64,")
        assert model == "gpt-vision"
        return "  描述：一张蓝色产品图  "

    monkeypatch.setattr(context_image_caption.storage, "aget_bytes", fake_read)
    monkeypatch.setattr(context_image_caption, "_call_upstream", fake_upstream)
    session = _Session()
    record = _ImageRecord(id="img1")

    caption = await context_image_caption.ensure_caption_for_image(
        session, record, model="gpt-vision"
    )

    assert caption == "一张蓝色产品图"
    assert record.metadata_jsonb["caption"] == "一张蓝色产品图"
    assert session.executed[0][1] == {
        "caption": "一张蓝色产品图",
        "image_id": "img1",
    }
    assert session.flushed is True


@pytest.mark.asyncio
async def test_ensure_caption_for_image_returns_none_when_image_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_read(_: str) -> bytes:
        raise FileNotFoundError("missing")

    async def must_not_call(*_: Any, **__: Any) -> str:
        raise AssertionError("upstream should not be called")

    monkeypatch.setattr(context_image_caption.storage, "aget_bytes", fake_read)
    monkeypatch.setattr(context_image_caption, "_call_upstream", must_not_call)

    caption = await context_image_caption.ensure_caption_for_image(
        _Session(), _ImageRecord(id="img1"), model="gpt-vision"
    )

    assert caption is None


@pytest.mark.asyncio
async def test_batch_caption_images_stops_starting_new_work_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancelled: list[str] = []
    calls: list[str] = []

    async def fake_image_data_url(record: _ImageRecord) -> str:
        return f"data:image/png;base64,{record.id}"

    async def fake_upstream(
        record: _ImageRecord, _image_url: str, *, model: str
    ) -> str | None:
        assert model == "gpt-vision"
        calls.append(record.id)
        if record.id == "slow":
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                cancelled.append(record.id)
                raise
        return f"caption-{record.id}"

    monkeypatch.setattr(context_image_caption, "_image_data_url", fake_image_data_url)
    monkeypatch.setattr(context_image_caption, "_call_upstream", fake_upstream)
    monkeypatch.setattr(context_image_caption, "_CAPTION_HTTP_TIMEOUT_S", 1.0)

    results = await context_image_caption.batch_caption_images(
        _Session(),
        [_ImageRecord(id="slow"), _ImageRecord(id="late")],
        model="gpt-vision",
        max_concurrency=1,
        total_timeout=0.01,
    )

    assert results == {"slow": "caption-slow"}
    assert calls == ["slow"]
    assert cancelled == []


@pytest.mark.asyncio
async def test_batch_caption_images_reuses_provided_session_for_uncached_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_image_data_url(record: _ImageRecord) -> str:
        return f"data:image/png;base64,{record.id}"

    async def fake_upstream(record: _ImageRecord, image_url: str, *, model: str) -> str:
        assert image_url.endswith(record.id)
        assert model == "gpt-vision"
        return f"caption-{record.id}"

    monkeypatch.setattr(context_image_caption, "_image_data_url", fake_image_data_url)
    monkeypatch.setattr(context_image_caption, "_call_upstream", fake_upstream)
    session = _Session()

    results = await context_image_caption.batch_caption_images(
        session,
        [_ImageRecord(id="a"), _ImageRecord(id="b")],
        model="gpt-vision",
        max_concurrency=2,
    )

    assert results == {"a": "caption-a", "b": "caption-b"}
    assert len(session.executed) == 2
    assert session.commits == 1


def test_extract_response_text_supports_top_level_and_nested_output() -> None:
    assert (
        context_image_caption._extract_response_text({"output_text": "top"})
        == "top"
    )
    assert (
        context_image_caption._extract_response_text(
            {"output": [{"content": [{"text": "nested"}]}]}
        )
        == "nested"
    )


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.base_url = f"https://{name}.example"
        self.api_key = f"sk-{name}"


class _Pool:
    def __init__(self, providers: list[_Provider]) -> None:
        self.providers = providers

    async def select(self, *, route: str = "text") -> list[_Provider]:
        assert route == "text"
        return self.providers


@pytest.mark.asyncio
async def test_call_upstream_stops_provider_chain_on_terminal_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_pool() -> _Pool:
        return _Pool([_Provider("acc1"), _Provider("acc2")])

    from app import provider_pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(context_image_caption, "_PER_PROVIDER_RETRY_ATTEMPTS", 2)

    calls: list[str] = []

    async def fake_one(
        record: _ImageRecord,
        image_url: str,
        *,
        model: str,
        base_url: str,
        api_key: str,
    ) -> str:
        assert record.id == "img-terminal"
        assert image_url == "data:image/png;base64,abc"
        assert model == "gpt-vision"
        calls.append(api_key)
        raise context_image_caption.UpstreamError(
            "bad request",
            error_code="invalid_request_error",
            status_code=400,
        )

    monkeypatch.setattr(context_image_caption, "_call_upstream_one", fake_one)

    result = await context_image_caption._call_upstream(
        _ImageRecord(id="img-terminal"),
        "data:image/png;base64,abc",
        model="gpt-vision",
    )

    assert result is None
    assert calls == ["sk-acc1"]


@pytest.mark.asyncio
async def test_call_upstream_failovers_after_retriable_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_pool() -> _Pool:
        return _Pool([_Provider("acc1"), _Provider("acc2")])

    from app import provider_pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)
    monkeypatch.setattr(context_image_caption, "_PER_PROVIDER_RETRY_ATTEMPTS", 1)

    calls: list[str] = []

    async def fake_one(
        record: _ImageRecord,
        image_url: str,
        *,
        model: str,
        base_url: str,
        api_key: str,
    ) -> str:
        _ = (record, image_url, model, base_url)
        calls.append(api_key)
        if api_key == "sk-acc1":
            raise context_image_caption.UpstreamError(
                "temporary",
                error_code="upstream_error",
                status_code=503,
            )
        return "caption from acc2"

    monkeypatch.setattr(context_image_caption, "_call_upstream_one", fake_one)

    result = await context_image_caption._call_upstream(
        _ImageRecord(id="img-retry"),
        "data:image/png;base64,abc",
        model="gpt-vision",
    )

    assert result == "caption from acc2"
    assert calls == ["sk-acc1", "sk-acc2"]
