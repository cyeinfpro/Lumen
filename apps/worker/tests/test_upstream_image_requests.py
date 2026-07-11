from __future__ import annotations

import ast
import base64
import hashlib
import inspect
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from app import upstream
from app import upstream_image_requests as image_requests


_POLICY = image_requests.ImageRequestPolicy(
    upstream_model="gpt-image-test",
    default_responses_model="gpt-responses-test",
    default_image_instructions="",
    image_qualities=frozenset({"auto", "low", "medium", "high"}),
    image_output_formats=frozenset({"png", "jpeg", "webp"}),
    image_backgrounds=frozenset({"auto", "opaque", "transparent"}),
    image_moderations=frozenset({"auto", "low"}),
    default_image_quality="high",
    default_image_output_format="png",
    default_image_output_compression=100,
    default_image_background="auto",
    default_image_moderation="low",
    transparent_matte_prompt_note="TEST TRANSPARENT MATTE NOTE",
    partial_images_max_pixels=1_400_000,
    image_job_retention_days=1,
)


def _policy(**changes: Any) -> image_requests.ImageRequestPolicy:
    return replace(_POLICY, **changes)


def _output_hooks(
    policy: image_requests.ImageRequestPolicy,
) -> image_requests.ImageOutputOptionsHooks:
    def normalize_background(value: str | None) -> str:
        return image_requests._normalize_image_background(value, policy=policy)

    def normalize_format(value: str | None) -> str:
        return image_requests._normalize_image_output_format(value, policy=policy)

    def normalize_compression(
        value: int | None,
        *,
        output_format: str,
    ) -> int | None:
        return image_requests._normalize_image_output_compression(
            value,
            output_format=output_format,
            policy=policy,
        )

    def normalize_moderation(value: str | None) -> str:
        return image_requests._normalize_image_moderation(value, policy=policy)

    return image_requests.ImageOutputOptionsHooks(
        normalize_image_background=normalize_background,
        normalize_image_output_format=normalize_format,
        normalize_image_output_compression=normalize_compression,
        normalize_image_moderation=normalize_moderation,
    )


def _transparent_hooks(
    policy: image_requests.ImageRequestPolicy,
) -> image_requests.TransparentMatteHooks:
    def is_transparent(background: str | None) -> bool:
        return image_requests._is_transparent_image_request(
            background,
            normalize_image_background=lambda value: (
                image_requests._normalize_image_background(value, policy=policy)
            ),
        )

    def append_note(prompt: str) -> str:
        return image_requests._append_transparent_matte_prompt(
            prompt,
            policy=policy,
        )

    return image_requests.TransparentMatteHooks(
        is_transparent_image_request=is_transparent,
        append_transparent_matte_prompt=append_note,
    )


def _add_output_options(
    policy: image_requests.ImageRequestPolicy,
) -> image_requests.AddImageOutputOptions:
    hooks = _output_hooks(policy)

    def add(
        body: dict[str, Any],
        *,
        output_format: str | None,
        output_compression: int | None,
        background: str | None,
        moderation: str | None,
    ) -> None:
        image_requests._add_image_output_options(
            body,
            output_format=output_format,
            output_compression=output_compression,
            background=background,
            moderation=moderation,
            hooks=hooks,
        )

    return add


def _transparent_options(
    policy: image_requests.ImageRequestPolicy,
) -> image_requests.TransparentMatteUpstreamOptions:
    hooks = _transparent_hooks(policy)

    def options(
        *,
        prompt: str,
        output_format: str | None,
        background: str | None,
    ) -> tuple[str, str | None, str | None]:
        return image_requests._transparent_matte_upstream_options(
            prompt=prompt,
            output_format=output_format,
            background=background,
            hooks=hooks,
        )

    return options


def _response_hooks(
    policy: image_requests.ImageRequestPolicy,
    *,
    normalize_reference_image: Callable[[bytes], tuple[bytes, str]] | None = None,
    validate_responses_body: Callable[[dict[str, Any]], None] | None = None,
    apply_retry_cache_busters: (
        Callable[[dict[str, Any], int, str, str], None] | None
    ) = None,
) -> image_requests.ResponsesImageBodyHooks:
    def normalize_quality(value: str | None) -> str:
        return image_requests._normalize_image_quality(value, policy=policy)

    return image_requests.ResponsesImageBodyHooks(
        normalize_image_quality=normalize_quality,
        transparent_matte_upstream_options=_transparent_options(policy),
        add_image_output_options=_add_output_options(policy),
        parse_size_pixels=image_requests._parse_size_pixels,
        normalize_reference_image=(
            normalize_reference_image
            if normalize_reference_image is not None
            else lambda raw: (raw, "image/png")
        ),
        stable_sort_tools=image_requests._stable_sort_tools,
        apply_retry_cache_busters=(
            apply_retry_cache_busters
            if apply_retry_cache_busters is not None
            else image_requests._apply_retry_cache_busters
        ),
        validate_responses_body=(
            validate_responses_body
            if validate_responses_body is not None
            else lambda _body: None
        ),
    )


def _build_pure_body(
    *,
    policy: image_requests.ImageRequestPolicy = _POLICY,
    hooks: image_requests.ResponsesImageBodyHooks | None = None,
    retry_attempt: int = 1,
    **changes: Any,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "action": "generate",
        "prompt": "draw a lighthouse",
        "size": "1024x1024",
        "images": None,
        "quality": "high",
        "output_format": None,
        "output_compression": None,
        "background": None,
        "moderation": None,
        "model": None,
        "image_urls": None,
    }
    kwargs.update(changes)
    return image_requests._build_responses_image_body(
        **kwargs,
        retry_attempt=retry_attempt,
        policy=policy,
        hooks=hooks or _response_hooks(policy),
    )


def test_extracted_module_has_no_worker_runtime_imports() -> None:
    tree = ast.parse(inspect.getsource(image_requests))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.add(node.module.split(".", 1)[0])
            imported_names.update(alias.name for alias in node.names)

    assert imported_names.isdisjoint(
        {"upstream", "contextvars", "httpx", "PIL", "settings", "EC"}
    )


def test_pure_normalization_output_options_and_transparency() -> None:
    policy = _policy(default_image_output_compression=73)
    assert image_requests._normalize_image_quality("medium", policy=policy) == "medium"
    assert image_requests._normalize_image_quality("invalid", policy=policy) == "high"
    assert image_requests._normalize_image_output_format(None, policy=policy) == "png"
    assert (
        image_requests._normalize_image_output_compression(
            150,
            output_format="jpeg",
            policy=policy,
        )
        == 100
    )
    assert (
        image_requests._normalize_image_output_compression(
            None,
            output_format="webp",
            policy=policy,
        )
        == 73
    )
    assert (
        image_requests._normalize_image_output_compression(
            50,
            output_format="png",
            policy=policy,
        )
        is None
    )
    assert (
        image_requests._normalize_image_background("invalid", policy=policy) == "auto"
    )
    assert image_requests._normalize_image_moderation(None, policy=policy) == "low"

    body: dict[str, Any] = {}
    image_requests._add_image_output_options(
        body,
        output_format="webp",
        output_compression=15,
        background="transparent",
        moderation=None,
        hooks=_output_hooks(policy),
    )
    assert body == {
        "output_format": "png",
        "background": "transparent",
        "moderation": "low",
    }

    prompt, output_format, background = (
        image_requests._transparent_matte_upstream_options(
            prompt="isolated product  ",
            output_format="webp",
            background="transparent",
            hooks=_transparent_hooks(policy),
        )
    )
    assert prompt == "isolated product\n\nTEST TRANSPARENT MATTE NOTE"
    assert output_format == "png"
    assert background == "opaque"
    assert (
        image_requests._append_transparent_matte_prompt(prompt, policy=policy) == prompt
    )


def test_pure_generate_body_and_partial_image_thresholds() -> None:
    body = _build_pure_body()
    assert body["model"] == "gpt-responses-test"
    assert body["instructions"] == ""
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "draw a lighthouse"}],
        }
    ]
    assert body["tool_choice"] == {"type": "image_generation"}
    assert body["parallel_tool_calls"] is True
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["stream"] is True
    assert body["store"] is False
    tool = body["tools"][0]
    assert tool == {
        "type": "image_generation",
        "model": "gpt-image-test",
        "action": "generate",
        "size": "1024x1024",
        "quality": "high",
        "output_format": "png",
        "background": "auto",
        "moderation": "low",
        "partial_images": 3,
    }

    above_threshold = _build_pure_body(size="1400001x1")
    assert "partial_images" not in above_threshold["tools"][0]

    low_quality = _build_pure_body(quality="low")
    assert "partial_images" not in low_quality["tools"][0]


def test_pure_edit_body_prefers_url_and_data_url_references() -> None:
    def normalize_must_not_run(_raw: bytes) -> tuple[bytes, str]:
        raise AssertionError("URL references must bypass byte normalization")

    hooks = _response_hooks(
        _POLICY,
        normalize_reference_image=normalize_must_not_run,
    )
    body = _build_pure_body(
        hooks=hooks,
        action="edit",
        images=[b"unused"],
        image_urls=[
            "https://refs.example/ref.webp",
            "",
            "data:image/png;base64,QUJD",
        ],
    )
    assert body["input"][0]["content"] == [
        {"type": "input_text", "text": "draw a lighthouse"},
        {"type": "input_image", "image_url": "https://refs.example/ref.webp"},
        {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
    ]


def test_pure_edit_body_base64_encodes_normalized_reference() -> None:
    calls: list[bytes] = []

    def normalize(raw: bytes) -> tuple[bytes, str]:
        calls.append(raw)
        return b"clean-reference", "image/webp"

    body = _build_pure_body(
        hooks=_response_hooks(_POLICY, normalize_reference_image=normalize),
        action="edit",
        images=[b"raw-reference"],
    )
    expected = base64.b64encode(b"clean-reference").decode("ascii")
    assert calls == [b"raw-reference"]
    assert body["input"][0]["content"][1] == {
        "type": "input_image",
        "image_url": f"data:image/webp;base64,{expected}",
    }


def test_pure_retry_cache_busting_is_deterministic_and_removes_partials() -> None:
    first = _build_pure_body(retry_attempt=1)
    second = _build_pure_body(retry_attempt=2)
    second_again = _build_pure_body(retry_attempt=2)
    third = _build_pure_body(retry_attempt=3)

    assert "prompt_cache_key" not in first
    assert first["tools"][0]["partial_images"] == 3
    assert second["prompt_cache_key"] == second_again["prompt_cache_key"]
    assert second["reasoning"] == {"effort": "minimal", "summary": "auto"}
    assert "partial_images" not in second["tools"][0]
    assert third["reasoning"] == {"effort": "high", "summary": "auto"}
    assert third["prompt_cache_key"] != second["prompt_cache_key"]


def test_pure_image_job_body_payload_retention_and_transport() -> None:
    policy = _policy(image_job_retention_days=7)

    def normalize_quality(value: str | None) -> str:
        return image_requests._normalize_image_quality(value, policy=policy)

    body = image_requests._image_job_body_base(
        prompt="product badge",
        size="2048x2048",
        n=2,
        quality="medium",
        output_format="webp",
        output_compression=80,
        background="transparent",
        moderation="auto",
        policy=policy,
        hooks=image_requests.ImageJobBodyHooks(
            transparent_matte_upstream_options=_transparent_options(policy),
            normalize_image_quality=normalize_quality,
            add_image_output_options=_add_output_options(policy),
        ),
    )
    assert body["model"] == "gpt-image-test"
    assert body["prompt"].endswith("TEST TRANSPARENT MATTE NOTE")
    assert body["output_format"] == "png"
    assert "output_compression" not in body
    assert body["background"] == "opaque"

    payload = image_requests._image_job_payload(
        request_type="edits",
        endpoint="/v1/images/edits",
        body=body,
        image_edit_input_transport="file",
        policy=policy,
    )
    assert payload == {
        "request_type": "edits",
        "endpoint": "/v1/images/edits",
        "body": body,
        "retention_days": 7,
        "image_edit_input_transport": "file",
    }


def test_pure_sort_inpaint_and_size_parsing() -> None:
    tools: list[Any] = [
        {"type": "zeta"},
        "unknown",
        {"name": "alpha", "type": "function"},
        {},
    ]
    assert image_requests._stable_sort_tools(tools) == [
        {"name": "alpha", "type": "function"},
        {"type": "zeta"},
        "unknown",
        {},
    ]
    assert tools[0] == {"type": "zeta"}

    wrapped = image_requests._wrap_inpaint_prompt("  remove the label  ")
    assert wrapped.startswith("Inside the masked region, remove the label.")
    assert "Preserve everything outside the mask exactly" in wrapped
    assert "Blend the result seamlessly" in wrapped

    assert image_requests._parse_size_pixels("1024x1536") == 1_572_864
    assert image_requests._parse_size_pixels("0x10") is None
    assert image_requests._parse_size_pixels("auto") is None
    assert image_requests._parse_size_pixels("10xinvalid") is None


def test_pure_idempotency_helpers_follow_current_monkeypatch_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fingerprints(
        files: image_requests.ImageRequestFiles,
    ) -> list[dict[str, Any]]:
        events.append("fingerprints")
        assert files is not None
        return [{"patched": len(files)}]

    def stable_dumps(value: Any) -> str:
        events.append("json")
        assert value["files"] == [{"patched": 1}]
        return "patched-stable-json"

    monkeypatch.setattr(image_requests, "_image_file_fingerprints", fingerprints)
    monkeypatch.setattr(image_requests, "_json_dumps_stable", stable_dumps)

    key = image_requests._image_idempotency_key(
        trace_id="trace-direct",
        endpoint="images/edits",
        body={"prompt": "edit"},
        files=[("image[]", ("ref.png", b"raw", "image/png"))],
    )

    expected_digest = hashlib.sha256(b"patched-stable-json").hexdigest()
    assert key == f"lumen-image2-{expected_digest[:32]}"
    assert events == ["fingerprints", "json"]

    computed_headers: list[dict[str, Any]] = []

    def computed_key(**kwargs: Any) -> str:
        computed_headers.append(kwargs)
        return "computed-direct-key"

    monkeypatch.setattr(image_requests, "_image_idempotency_key", computed_key)

    empty_headers: dict[str, str] = {}
    image_requests._attach_image_idempotency_key(
        empty_headers,
        trace_id="trace-direct",
        endpoint="images/generations",
    )
    existing_headers = {"Idempotency-Key": "caller-supplied-key"}
    image_requests._attach_image_idempotency_key(
        existing_headers,
        trace_id="trace-direct",
        endpoint="images/generations",
    )

    assert empty_headers["Idempotency-Key"] == "computed-direct-key"
    assert existing_headers["Idempotency-Key"] == "caller-supplied-key"
    assert len(computed_headers) == 2


def test_idempotency_facades_pass_current_monkeypatched_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def fingerprints(
        files: image_requests.ImageRequestFiles,
    ) -> list[dict[str, Any]]:
        events.append("facade-fingerprints")
        assert files is None
        return [{"facade": True}]

    def stable_dumps(value: Any) -> str:
        events.append("facade-json")
        assert value["files"] == [{"facade": True}]
        return "facade-stable-json"

    monkeypatch.setattr(upstream, "_image_file_fingerprints", fingerprints)
    monkeypatch.setattr(upstream, "_json_dumps_stable", stable_dumps)

    key = upstream._image_idempotency_key(
        trace_id="trace-facade",
        endpoint="image-jobs",
        body={"request_type": "generations"},
    )

    expected_digest = hashlib.sha256(b"facade-stable-json").hexdigest()
    assert key == f"lumen-image2-{expected_digest[:32]}"
    assert events == ["facade-fingerprints", "facade-json"]

    key_calls: list[dict[str, Any]] = []

    def computed_key(**kwargs: Any) -> str:
        key_calls.append(kwargs)
        return "computed-facade-key"

    monkeypatch.setattr(upstream, "_image_idempotency_key", computed_key)
    headers = {"Idempotency-Key": "caller-supplied-key"}
    upstream._attach_image_idempotency_key(
        headers,
        trace_id="trace-facade",
        endpoint="images/generations",
    )

    assert headers["Idempotency-Key"] == "caller-supplied-key"
    assert len(key_calls) == 1


def test_facades_read_current_policy_globals_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(upstream, "UPSTREAM_MODEL", "patched-image-model")
    monkeypatch.setattr(
        upstream,
        "DEFAULT_IMAGE_RESPONSES_MODEL",
        "patched-responses-model",
    )
    monkeypatch.setattr(upstream, "_DEFAULT_IMAGE_OUTPUT_FORMAT", "jpeg")
    monkeypatch.setattr(upstream, "_DEFAULT_IMAGE_OUTPUT_COMPRESSION", 42)
    monkeypatch.setattr(upstream, "_TRANSPARENT_MATTE_PROMPT_NOTE", "PATCHED NOTE")
    monkeypatch.setattr(upstream, "_IMAGE_JOB_RETENTION_DAYS", 9)
    monkeypatch.setattr(upstream, "_PARTIAL_IMAGES_MAX_PIXELS", 1)

    assert upstream._normalize_image_output_format(None) == "jpeg"
    assert (
        upstream._normalize_image_output_compression(
            None,
            output_format="jpeg",
        )
        == 42
    )
    assert upstream._append_transparent_matte_prompt("prompt") == (
        "prompt\n\nPATCHED NOTE"
    )
    assert (
        upstream._image_job_payload(
            request_type="generations",
            endpoint="/v1/images/generations",
            body={},
        )["retention_days"]
        == 9
    )

    kwargs: dict[str, Any] = {
        "action": "generate",
        "prompt": "test",
        "size": "1x1",
        "images": None,
        "quality": "high",
        "output_format": None,
        "output_compression": None,
        "background": None,
        "moderation": None,
        "model": None,
    }
    body = upstream._build_responses_image_body(**kwargs)
    assert body["model"] == "patched-responses-model"
    assert body["tools"][0]["model"] == "patched-image-model"
    assert body["tools"][0]["partial_images"] == 3
    assert body["tools"][0]["output_format"] == "jpeg"
    assert body["tools"][0]["output_compression"] == 42

    monkeypatch.setattr(upstream, "_PARTIAL_IMAGES_MAX_PIXELS", 0)
    body_after_policy_change = upstream._build_responses_image_body(**kwargs)
    assert "partial_images" not in body_after_policy_change["tools"][0]


def test_facade_builder_uses_monkeypatched_helpers_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def normalize_quality(_value: str | None) -> str:
        events.append("quality")
        return "low"

    def matte_options(
        *,
        prompt: str,
        output_format: str | None,
        background: str | None,
    ) -> tuple[str, str | None, str | None]:
        _ = output_format, background
        events.append("matte")
        return f"{prompt}|matte", "webp", "opaque"

    def add_output(
        body: dict[str, Any],
        *,
        output_format: str | None,
        output_compression: int | None,
        background: str | None,
        moderation: str | None,
    ) -> None:
        _ = output_compression
        events.append("output")
        body.update(
            {
                "output_format": output_format,
                "background": background,
                "moderation": moderation,
            }
        )

    def parse_pixels(_size: str) -> int | None:
        events.append("pixels")
        return 1

    def normalize_reference(raw: bytes) -> tuple[bytes, str]:
        assert raw == b"raw"
        events.append("reference")
        return b"normalized", "image/webp"

    def sort_tools(tools: list[Any]) -> list[Any]:
        events.append("sort")
        return tools

    def retry_busters(
        body: dict[str, Any],
        retry_attempt: int,
        _prompt: str,
        _size: str,
    ) -> None:
        events.append(f"retry:{retry_attempt}")
        body["retry_attempt_seen"] = retry_attempt

    def validate(body: dict[str, Any]) -> None:
        assert body["retry_attempt_seen"] == 3
        events.append("validate")

    monkeypatch.setattr(upstream, "_normalize_image_quality", normalize_quality)
    monkeypatch.setattr(
        upstream,
        "_transparent_matte_upstream_options",
        matte_options,
    )
    monkeypatch.setattr(upstream, "_add_image_output_options", add_output)
    monkeypatch.setattr(upstream, "_parse_size_pixels", parse_pixels)
    monkeypatch.setattr(upstream, "_normalize_reference_image", normalize_reference)
    monkeypatch.setattr(upstream, "_stable_sort_tools", sort_tools)
    monkeypatch.setattr(upstream, "_apply_retry_cache_busters", retry_busters)
    monkeypatch.setattr(upstream, "_validate_responses_body", validate)

    token = upstream.push_image_retry_attempt(3)
    try:
        body = upstream._build_responses_image_body(
            action="edit",
            prompt="edit",
            size="10x10",
            images=[b"raw"],
            quality="high",
            output_format="jpeg",
            output_compression=10,
            background="transparent",
            moderation="auto",
            model=None,
        )
    finally:
        upstream.pop_image_retry_attempt(token)

    assert events == [
        "quality",
        "matte",
        "output",
        "pixels",
        "reference",
        "sort",
        "retry:3",
        "validate",
    ]
    assert body["tools"][0]["quality"] == "low"
    assert body["input"][0]["content"][0]["text"] == "edit|matte"
    assert body["input"][0]["content"][1]["image_url"] == (
        "data:image/webp;base64,bm9ybWFsaXplZA=="
    )


def test_nested_retry_context_restores_outer_and_initial_values() -> None:
    kwargs: dict[str, Any] = {
        "action": "generate",
        "prompt": "retry context",
        "size": "1024x1024",
        "images": None,
        "quality": "high",
        "output_format": None,
        "output_compression": None,
        "background": None,
        "moderation": None,
        "model": None,
    }
    initial_attempt = upstream._image_retry_attempt_ctx.get()
    outer_token = upstream.push_image_retry_attempt(2)
    try:
        outer_body = upstream._build_responses_image_body(**kwargs)
        inner_token = upstream.push_image_retry_attempt(5)
        try:
            inner_body = upstream._build_responses_image_body(**kwargs)
        finally:
            upstream.pop_image_retry_attempt(inner_token)
        restored_outer_body = upstream._build_responses_image_body(**kwargs)
    finally:
        upstream.pop_image_retry_attempt(outer_token)

    assert outer_body["reasoning"]["effort"] == "minimal"
    assert inner_body["reasoning"]["effort"] == "medium"
    assert restored_outer_body["prompt_cache_key"] == outer_body["prompt_cache_key"]
    assert upstream._image_retry_attempt_ctx.get() == initial_attempt


@pytest.mark.asyncio
async def test_responses_stream_routes_body_through_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    images = [b"raw-reference"]
    image_urls = ["https://refs.example/ref.webp"]
    expected = upstream._build_responses_image_body(
        action="edit",
        prompt="edit via stream",
        size="1024x1024",
        images=images,
        image_urls=image_urls,
        quality="high",
        output_format="webp",
        output_compression=80,
        background="auto",
        moderation="low",
        model=None,
    )
    original_builder = upstream._build_responses_image_body
    captured: dict[str, Any] = {}

    def facade_wrapper(**kwargs: Any) -> dict[str, Any]:
        captured["builder_kwargs"] = kwargs
        body = original_builder(**kwargs)
        body["facade_marker"] = True
        captured["built_body"] = body
        return body

    async def resolve_job_base() -> str:
        return "https://image-job.example"

    async def resolve_refs(
        raw_images: list[bytes] | None,
        **_kwargs: Any,
    ) -> list[str]:
        assert raw_images == images
        return image_urls

    async def resolve_proxy(_proxy: Any) -> str | None:
        return None

    async def iter_sse(**kwargs: Any):
        captured["sent_body"] = kwargs["json_body"]
        yield {
            "type": "response.output_item.done",
            "item": {
                "type": "image_generation_call",
                "result": "ZmFrZS1pbWFnZQ==",
            },
        }

    monkeypatch.setattr(upstream, "_build_responses_image_body", facade_wrapper)
    monkeypatch.setattr(upstream, "_resolve_image_job_base_url", resolve_job_base)
    monkeypatch.setattr(upstream, "_resolve_reference_image_urls", resolve_refs)
    monkeypatch.setattr(upstream, "resolve_provider_proxy_url", resolve_proxy)
    monkeypatch.setattr(upstream, "_iter_sse_curl", iter_sse)
    monkeypatch.setattr(upstream, "_select_image_read_timeout", lambda _size: 1.0)

    result = await upstream._responses_image_stream(
        prompt="edit via stream",
        size="1024x1024",
        action="edit",
        images=images,
        quality="high",
        output_format="webp",
        output_compression=80,
        background="auto",
        moderation="low",
        use_httpx=False,
        base_url_override="https://upstream.example/v1",
        api_key_override="test-key",
    )

    assert result == ("ZmFrZS1pbWFnZQ==", None)
    assert captured["builder_kwargs"]["image_urls"] == image_urls
    assert captured["sent_body"] is captured["built_body"]
    assert captured["sent_body"]["facade_marker"] is True
    actual_without_marker = dict(captured["sent_body"])
    actual_without_marker.pop("facade_marker")
    assert actual_without_marker == expected
