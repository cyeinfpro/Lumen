from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

from app.storage import LocalStorage
from app.tasks import completion
from app.tasks import generation
from app.tasks.completion_parts import tool_images


def _png_bytes(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), color=(12, 34, 56)).save(buf, format="PNG")
    return buf.getvalue()


def test_completion_tool_image_skips_blurhash_for_tiny_images(
    monkeypatch: Any,
) -> None:
    def fail_blurhash(_img: PILImage.Image) -> str:
        raise AssertionError("tiny images must not call blurhash encoder")

    monkeypatch.setattr(completion, "_generation_compute_blurhash", fail_blurhash)

    (
        orig_ext,
        orig_mime,
        width,
        height,
        blurhash_str,
        *_variants,
    ) = completion._image_format_and_meta(_png_bytes(2, 2))

    assert orig_ext == "png"
    assert orig_mime == "image/png"
    assert (width, height) == (2, 2)
    assert blurhash_str is None


def test_tool_image_dedupe_key_uses_b64_sha1_without_item_id() -> None:
    b64_one = " data:image/png;base64,\nQUJDRA== "
    b64_two = "QUJDRA=="

    assert completion._tool_image_dedupe_key({}, b64_one).startswith("b64sha1:")
    assert completion._tool_image_dedupe_key({}, b64_one) == (
        completion._tool_image_dedupe_key({}, b64_two)
    )


def test_tool_image_dedupe_key_prefers_item_id() -> None:
    key = completion._tool_image_dedupe_key(
        {"item": {"id": "img-call-1"}},
        "different-image",
    )

    assert key == "id:img-call-1"


@pytest.mark.asyncio
async def test_tool_image_budget_storage_commit_publish_order_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    expected_payload = {"image_id": "image-1"}

    async def ensure_budget(**_kwargs: Any) -> int:
        events.append("budget")
        return 17

    def decode(_value: str) -> bytes:
        events.append("decode")
        return b"raw-image"

    class Session:
        async def __aenter__(self) -> "Session":
            events.append("session_enter")
            return self

        async def __aexit__(self, *_args: Any) -> None:
            events.append("session_exit")

        async def commit(self) -> None:
            events.append("commit")

    async def store(**kwargs: Any) -> dict[str, Any]:
        events.append("storage_orm_stage")
        assert kwargs["raw_image"] == b"raw-image"
        await kwargs["session"].commit()
        return expected_payload

    async def publish(*_args: Any, **_kwargs: Any) -> None:
        events.append("sse_publish")

    monkeypatch.setattr(
        completion,
        "_ensure_completion_tool_image_wallet_budget",
        ensure_budget,
    )
    monkeypatch.setattr(completion, "_decode_upstream_image_b64", decode)
    monkeypatch.setattr(completion, "SessionLocal", lambda: Session())
    monkeypatch.setattr(completion, "_store_completion_tool_image", store)
    monkeypatch.setattr(completion, "publish_event", publish)

    payload, reserved = await completion._store_and_publish_completion_tool_image(
        redis=object(),
        user_id="user-1",
        channel="task:comp-1",
        task_id="comp-1",
        message_id="message-1",
        attempt=2,
        attempt_epoch=2,
        b64_image="encoded",
        revised_prompt="revised",
        reserved_tool_image_micro=5,
    )

    assert payload is expected_payload
    assert reserved == 17
    assert events == [
        "budget",
        "decode",
        "session_enter",
        "storage_orm_stage",
        "commit",
        "session_exit",
        "sse_publish",
    ]


@pytest.mark.asyncio
async def test_cancel_after_tool_image_settles_partial_image_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion_row = SimpleNamespace()
    charged: list[Any] = []
    released: list[str] = []

    async def fallback_image_tokens(
        _session: Any,
        _completion: Any,
        *,
        budget_micro: int,
    ) -> int:
        assert budget_micro == 250
        return 23

    async def charge(_session: Any, row: Any) -> None:
        charged.append(row)

    async def release(_session: Any, _row: Any, *, reason: str) -> None:
        released.append(reason)

    monkeypatch.setattr(
        completion.completion_billing,
        "fallback_completion_tool_image_tokens",
        fallback_image_tokens,
    )
    monkeypatch.setattr(completion.worker_billing, "charge_completion", charge)
    monkeypatch.setattr(completion.worker_billing, "release_completion", release)

    await completion._settle_cancelled_completion_billing(
        object(),
        completion_row,
        has_partial=True,
        input_list=[],
        accumulated_text="",
        tokens_in=7,
        tokens_out=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
        tool_images=[{"image_id": "image-1"}],
        reserved_tool_image_budget_micro=250,
        reason="cancelled",
    )

    assert charged == [completion_row]
    assert released == []
    assert completion_row.tokens_in == 7
    assert completion_row.tokens_out == 23
    assert completion_row.image_output_tokens == 23


@pytest.mark.asyncio
async def test_cancel_before_first_delta_charges_sent_request_input_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion_row = SimpleNamespace()
    charged: list[Any] = []
    released: list[str] = []
    input_list = [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "render a plan"}],
        }
    ]
    instructions = "Keep the response concise."

    async def charge(_session: Any, row: Any) -> None:
        charged.append(row)

    async def release(_session: Any, _row: Any, *, reason: str) -> None:
        released.append(reason)

    monkeypatch.setattr(tool_images, "count_tokens", len)
    monkeypatch.setattr(completion.worker_billing, "charge_completion", charge)
    monkeypatch.setattr(completion.worker_billing, "release_completion", release)

    await completion._settle_cancelled_completion_billing(
        object(),
        completion_row,
        has_partial=False,
        input_list=input_list,
        instructions=instructions,
        accumulated_text="",
        tokens_in=0,
        tokens_out=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
        tool_images=[],
        reserved_tool_image_budget_micro=0,
        reason="cancelled",
    )

    expected = tool_images._estimate_completion_request_input_tokens(
        input_list,
        instructions=instructions,
    )
    assert charged == [completion_row]
    assert released == []
    assert completion_row.tokens_in == expected
    assert completion_row.tokens_out == 0


@pytest.mark.asyncio
async def test_cancelled_tool_image_fallback_counts_top_level_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion_row = SimpleNamespace()
    charged: list[Any] = []
    instructions = "Keep every constraint visible to billing. " * 200

    async def fallback_image_tokens(
        _session: Any,
        _completion: Any,
        *,
        budget_micro: int,
    ) -> int:
        assert budget_micro == 250
        return 23

    async def charge(_session: Any, row: Any) -> None:
        charged.append(row)

    async def release(_session: Any, _row: Any, *, reason: str) -> None:
        raise AssertionError(f"partial tool usage must be charged: {reason}")

    monkeypatch.setattr(tool_images, "count_tokens", len)
    monkeypatch.setattr(
        completion.completion_billing,
        "fallback_completion_tool_image_tokens",
        fallback_image_tokens,
    )
    monkeypatch.setattr(completion.worker_billing, "charge_completion", charge)
    monkeypatch.setattr(completion.worker_billing, "release_completion", release)

    await completion._settle_cancelled_completion_billing(
        object(),
        completion_row,
        has_partial=True,
        input_list=[],
        instructions=instructions,
        accumulated_text="",
        tokens_in=0,
        tokens_out=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
        tool_images=[{"image_id": "image-1"}],
        reserved_tool_image_budget_micro=250,
        reason="cancelled",
    )

    expected_input_tokens = len(
        json.dumps(
            {
                "input": [],
                "instructions": instructions,
            },
            ensure_ascii=False,
        )
    )
    assert charged == [completion_row]
    assert completion_row.tokens_in == expected_input_tokens
    assert completion_row.tokens_out == 23
    assert completion_row.image_output_tokens == 23


@pytest.mark.asyncio
async def test_cancel_retry_uses_persisted_tool_image_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion_row = SimpleNamespace(
        tokens_in=0,
        tokens_out=19,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=19,
    )
    charged: list[Any] = []
    released: list[str] = []

    async def charge(_session: Any, row: Any) -> None:
        charged.append(row)

    async def release(_session: Any, _row: Any, *, reason: str) -> None:
        released.append(reason)

    monkeypatch.setattr(completion.worker_billing, "charge_completion", charge)
    monkeypatch.setattr(completion.worker_billing, "release_completion", release)

    await completion._settle_cancelled_completion_billing(
        object(),
        completion_row,
        has_partial=False,
        input_list=None,
        accumulated_text="",
        tokens_in=0,
        tokens_out=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        cache_creation_5m_tokens=0,
        cache_creation_1h_tokens=0,
        reasoning_tokens=0,
        image_output_tokens=0,
        tool_images=[],
        reserved_tool_image_budget_micro=0,
        reason="cancelled",
    )

    assert charged == [completion_row]
    assert released == []
    assert completion_row.image_output_tokens == 19


@pytest.mark.asyncio
async def test_tool_image_usage_is_persisted_under_current_attempt_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        attempt=2,
        status=completion.CompletionStatus.STREAMING.value,
        upstream_request={},
        tokens_out=0,
        image_output_tokens=0,
    )

    class Session:
        async def get(self, _model: Any, _task_id: str) -> Any:
            return row

    async def acquire_lock(_session: Any, _task_id: str) -> None:
        return None

    async def fallback_tokens(
        _session: Any,
        _completion: Any,
        *,
        budget_micro: int,
    ) -> int:
        return budget_micro // 10

    hooks = tool_images.ToolImageUsageHooks(
        acquire_lock=acquire_lock,
        completion_model=completion.Completion,
        running_statuses=(completion.CompletionStatus.STREAMING.value,),
        superseded_error_type=completion._CompletionEpochSuperseded,
        fallback_image_tokens=fallback_tokens,
    )
    await tool_images._record_completion_tool_image_usage(
        session=Session(),
        task_id="comp-1",
        attempt_epoch=2,
        budget_micro=100,
        hooks=hooks,
    )
    await tool_images._record_completion_tool_image_usage(
        session=Session(),
        task_id="comp-1",
        attempt_epoch=2,
        budget_micro=200,
        hooks=hooks,
    )

    assert row.upstream_request["tool_image_reserved_micro"] == 300
    assert row.image_output_tokens == 30
    assert row.tokens_out == 30


@pytest.mark.asyncio
async def test_tool_image_usage_rejects_superseded_attempt() -> None:
    row = SimpleNamespace(
        attempt=3,
        status=completion.CompletionStatus.STREAMING.value,
        upstream_request={},
    )

    class Session:
        async def get(self, _model: Any, _task_id: str) -> Any:
            return row

    async def acquire_lock(_session: Any, _task_id: str) -> None:
        return None

    async def fallback_tokens(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("stale attempt must fail before usage calculation")

    with pytest.raises(completion._CompletionEpochSuperseded):
        await tool_images._record_completion_tool_image_usage(
            session=Session(),
            task_id="comp-1",
            attempt_epoch=2,
            budget_micro=100,
            hooks=tool_images.ToolImageUsageHooks(
                acquire_lock=acquire_lock,
                completion_model=completion.Completion,
                running_statuses=(completion.CompletionStatus.STREAMING.value,),
                superseded_error_type=completion._CompletionEpochSuperseded,
                fallback_image_tokens=fallback_tokens,
            ),
        )


@pytest.mark.asyncio
async def test_tool_image_commit_failure_removes_real_local_storage_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_storage = LocalStorage(tmp_path)
    message = SimpleNamespace(content={})

    class Session:
        def __init__(self) -> None:
            self.added: list[Any] = []

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def get(self, model: Any, _row_id: str) -> Any:
            return message if model is completion.Message else None

        async def commit(self) -> None:
            raise RuntimeError("commit failed")

    async def record_usage(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(completion, "storage", local_storage)
    monkeypatch.setattr(generation, "storage", local_storage)
    monkeypatch.setattr(
        tool_images,
        "_record_completion_tool_image_usage",
        record_usage,
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await completion._store_completion_tool_image(
            session=Session(),
            task_id="comp-commit-failure",
            attempt_epoch=1,
            user_id="user-1",
            message_id="message-1",
            raw_image=_png_bytes(32, 24),
            revised_prompt=None,
            billing_budget_micro=100,
        )

    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
