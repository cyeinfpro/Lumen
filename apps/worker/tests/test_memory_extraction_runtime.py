from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest

from app.provider_runtime.upstream_services import ImageUpstreamRuntime
from app.tasks import memory_extraction
from lumen_core.memory import ExtractedMemory


@pytest.mark.asyncio
async def test_llm_extraction_passes_explicit_image_upstream_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool
    from app.upstream_parts import entrypoints as upstream

    class Provider:
        name = "memory-provider"
        base_url = "https://memory.example"
        api_key = "sk-memory"
        proxy = None

    class Pool:
        async def select(self, *, purpose: str) -> list[Provider]:
            assert purpose == "chat"
            return [Provider()]

    class Attempt:
        def __enter__(self) -> Attempt:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def report_exception(self, _exc: Exception) -> None:
            return None

        def report_success(self) -> None:
            return None

    async def get_pool() -> Pool:
        return Pool()

    image_upstream_runtime = ImageUpstreamRuntime(
        services=cast(Any, object()),
    )
    seen_runtime: list[ImageUpstreamRuntime] = []

    async def responses_call(
        _body: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        seen_runtime.append(kwargs["runtime"])
        return {
            "output_text": (
                '{"items":[{"type":"preference","content":"concise replies",'
                '"confidence":0.9,"source_excerpt":"concise replies",'
                '"intent_kind":"statement"}]}'
            )
        }

    monkeypatch.setattr(provider_pool, "get_pool", get_pool)
    monkeypatch.setattr(
        provider_pool,
        "text_provider_attempt",
        lambda _pool, _provider: Attempt(),
    )
    monkeypatch.setattr(upstream, "responses_call", responses_call)

    items = await memory_extraction._try_llm_extract(
        "I prefer concise replies",
        explicit_only=False,
        image_upstream_runtime=image_upstream_runtime,
    )

    assert [item.content for item in items] == ["concise replies"]
    assert seen_runtime == [image_upstream_runtime]


@pytest.mark.asyncio
async def test_prepare_memory_extraction_reads_runtime_from_arq_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = ExtractedMemory(
        type="preference",
        content="concise replies",
        confidence=0.9,
        source_excerpt="concise replies",
        intent_kind="statement",
    )
    claim = memory_extraction._MemoryExtractionClaim(
        run_id="run-1",
        conversation_id="conv-1",
        user_id="user-1",
        source_message_id="message-1",
        assistant_message_id="message-2",
        event_id="event-1",
        owner="worker-1",
        job_id="job-1",
        fence=1,
        text="I prefer concise replies",
        extraction_threshold=0.7,
        scope_hint=None,
    )
    image_upstream_runtime = ImageUpstreamRuntime(
        services=cast(Any, object()),
    )
    seen_runtime: list[ImageUpstreamRuntime | None] = []

    async def try_llm_extract(
        _text: str,
        *,
        explicit_only: bool,
        scope_hint: str | None,
        image_upstream_runtime: ImageUpstreamRuntime | None,
        fence: Callable[[], Awaitable[bool]] | None = None,
    ) -> list[ExtractedMemory]:
        assert explicit_only is False
        assert scope_hint is None
        seen_runtime.append(image_upstream_runtime)
        return []

    async def embedding_literal(
        _ctx: dict[str, Any] | None,
        content: str,
        *,
        fence: Callable[[], Awaitable[bool]] | None = None,
    ) -> str:
        assert content == candidate.content
        return "[1.0]"

    monkeypatch.setattr(
        memory_extraction,
        "extract_memories",
        lambda _text, *, explicit_only: ([candidate], False),
    )
    monkeypatch.setattr(memory_extraction, "_try_llm_extract", try_llm_extract)
    monkeypatch.setattr(
        memory_extraction,
        "_embedding_literal_async",
        embedding_literal,
    )

    prepared, rejected_pii = await memory_extraction._prepare_memory_extraction(
        {"image_upstream_runtime": image_upstream_runtime},
        claim,
    )

    assert rejected_pii is False
    assert [item.candidate for item in prepared] == [candidate]
    assert seen_runtime == [image_upstream_runtime]
