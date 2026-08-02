from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

from app.artifact_commit import ArtifactAdoption
from app.storage import LocalStorage
from app.storage_writes import StorageWriteCoordinator
from app.tasks.completion_parts import default_runtime as completion
from app.tasks.completion_parts import tool_images
from app.tasks.completion_parts.image_storage_runtime import (
    CompletionToolImageBudget,
    CompletionToolImageCodec,
    CompletionToolImageEvents,
    CompletionToolImageRepository,
    CompletionToolImageService,
    CompletionToolImageStorage,
)


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
    published_payloads: list[dict[str, Any]] = []
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

    async def store(_self: Any, **kwargs: Any) -> dict[str, Any]:
        events.append("storage_orm_stage")
        assert kwargs["raw_image"] == b"raw-image"
        assert kwargs["attempt_epoch"] == 2
        assert kwargs["execution_epoch"] == 7
        await kwargs["session"].commit()
        return expected_payload

    async def publish(*args: Any, **_kwargs: Any) -> None:
        events.append("sse_publish")
        published_payloads.append(args[-1])

    @asynccontextmanager
    async def cleanup(_keys: list[str]):
        yield

    async def write_files(_files: list[tuple[str, bytes]]) -> list[str]:
        return []

    async def record_usage(**_kwargs: Any) -> None:
        return None

    async def acquire_task_lock(_session: Any, _task_id: str) -> None:
        return None

    async def delete_files(_keys: list[str]) -> None:
        return None

    service = CompletionToolImageService(
        budget=CompletionToolImageBudget(reserve=ensure_budget),
        codec=CompletionToolImageCodec(
            decode=decode,
            format_and_meta=lambda _raw: (),
            sha256=lambda _raw: "",
            upstream_error_type=RuntimeError,
            bad_response_error_code="bad_response",
        ),
        repository=CompletionToolImageRepository(
            session_factory=lambda: Session(),
            new_id=lambda: "image-1",
            acquire_task_lock=acquire_task_lock,
            completion_model=object,
            superseded_error_type=RuntimeError,
            record_usage=record_usage,
            image_model=object,
            image_variant_model=object,
            message_model=object,
            public_url=str,
        ),
        storage=CompletionToolImageStorage(
            write_files=write_files,
            cleanup_on_error=cleanup,
            delete_files=delete_files,
        ),
        events=CompletionToolImageEvents(
            publish=publish,
            image_event="completion.image",
        ),
    )
    monkeypatch.setattr(CompletionToolImageService, "store_tool_image", store)

    payload, reserved = await service.store_and_publish_tool_image(
        redis=object(),
        user_id="user-1",
        channel="task:comp-1",
        task_id="comp-1",
        message_id="message-1",
        attempt=2,
        attempt_epoch=2,
        execution_epoch=7,
        b64_image="encoded",
        revised_prompt="revised",
        reserved_tool_image_micro=5,
    )

    assert payload is expected_payload
    assert reserved == 17
    assert payload["image_id"] == "image-1"
    assert events == [
        "budget",
        "decode",
        "session_enter",
        "storage_orm_stage",
        "commit",
        "session_exit",
        "sse_publish",
    ]
    assert published_payloads == [
        {
            "completion_id": "comp-1",
            "message_id": "message-1",
            "attempt": 2,
            "attempt_epoch": 2,
            "execution_epoch": 7,
            "images": [expected_payload],
        }
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
        execution_epoch=7,
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
        execution_epoch=7,
        budget_micro=100,
        hooks=hooks,
    )
    await tool_images._record_completion_tool_image_usage(
        session=Session(),
        task_id="comp-1",
        attempt_epoch=2,
        execution_epoch=7,
        budget_micro=200,
        hooks=hooks,
    )

    assert row.upstream_request["tool_image_reserved_micro"] == 300
    assert row.upstream_request["completion_usage_execution_epoch"] == 7
    assert row.upstream_request["completion_usage_attempt_epoch"] == 2
    assert row.image_output_tokens == 30
    assert row.tokens_out == 30


@pytest.mark.asyncio
async def test_tool_image_usage_rejects_superseded_attempt() -> None:
    row = SimpleNamespace(
        attempt=3,
        execution_epoch=7,
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
            execution_epoch=7,
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
async def test_tool_image_usage_rejects_committed_cancel_intent() -> None:
    row = SimpleNamespace(
        attempt=2,
        execution_epoch=7,
        status=completion.CompletionStatus.STREAMING.value,
        cancel_requested_at=object(),
        upstream_request={},
    )

    class Session:
        async def get(self, _model: Any, _task_id: str) -> Any:
            return row

    async def acquire_lock(_session: Any, _task_id: str) -> None:
        return None

    async def fallback_tokens(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("cancelled completion must fail before usage calculation")

    with pytest.raises(completion._CompletionEpochSuperseded):
        await tool_images._record_completion_tool_image_usage(
            session=Session(),
            task_id="comp-1",
            attempt_epoch=2,
            execution_epoch=7,
            budget_micro=100,
            hooks=tool_images.ToolImageUsageHooks(
                acquire_lock=acquire_lock,
                completion_model=completion.Completion,
                running_statuses=(completion.CompletionStatus.STREAMING.value,),
                superseded_error_type=completion._CompletionEpochSuperseded,
                fallback_image_tokens=fallback_tokens,
            ),
        )

    assert row.upstream_request == {}


@pytest.mark.asyncio
async def test_tool_image_usage_rejects_same_attempt_from_old_execution() -> None:
    row = SimpleNamespace(
        attempt=2,
        execution_epoch=8,
        status=completion.CompletionStatus.STREAMING.value,
        cancel_requested_at=None,
        upstream_request={},
    )

    class Session:
        async def get(self, _model: Any, _task_id: str) -> Any:
            return row

    async def acquire_lock(_session: Any, _task_id: str) -> None:
        return None

    async def fallback_tokens(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("old execution must fail before usage calculation")

    with pytest.raises(completion._CompletionEpochSuperseded):
        await tool_images._record_completion_tool_image_usage(
            session=Session(),
            task_id="comp-1",
            attempt_epoch=2,
            execution_epoch=7,
            budget_micro=100,
            hooks=tool_images.ToolImageUsageHooks(
                acquire_lock=acquire_lock,
                completion_model=completion.Completion,
                running_statuses=(completion.CompletionStatus.STREAMING.value,),
                superseded_error_type=completion._CompletionEpochSuperseded,
                fallback_image_tokens=fallback_tokens,
            ),
        )

    assert row.upstream_request == {}


def _tool_image_commit_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome: ArtifactAdoption,
) -> tuple[Any, type[Any], list[int], Any]:
    local_storage = LocalStorage(tmp_path)
    message = SimpleNamespace(content={})
    reserved: list[int] = []

    class Lease:
        released = 0

        async def renew(self) -> bool:
            return True

        async def release(self) -> None:
            self.released += 1

    lease = Lease()

    class Capacity:
        async def reserve(self, bytes_required: int) -> Lease:
            reserved.append(bytes_required)
            return lease

    class Session:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.rolled_back = False

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def get(self, model: Any, _row_id: str, **_kwargs: Any) -> Any:
            return message if model is completion.Message else None

        async def commit(self) -> None:
            raise RuntimeError("commit failed")

        async def rollback(self) -> None:
            self.rolled_back = True

    async def record_usage(**_kwargs: Any) -> None:
        return None

    async def probe(
        _self: CompletionToolImageService,
        **_kwargs: Any,
    ) -> ArtifactAdoption:
        return outcome

    monkeypatch.setattr(
        tool_images,
        "_record_completion_tool_image_usage",
        record_usage,
    )
    monkeypatch.setattr(
        CompletionToolImageService,
        "_probe_tool_image_adoption",
        probe,
    )
    coordinator = StorageWriteCoordinator(
        storage=local_storage,
        capacity=Capacity(),  # type: ignore[arg-type]
        lease_ttl_seconds=60,
    )
    service = completion._build_completion_tool_image_service(  # noqa: SLF001
        coordinator
    )
    return service, Session, reserved, lease


@pytest.mark.asyncio
async def test_tool_image_confirmed_non_adoption_removes_storage_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session_type, reserved, lease = _tool_image_commit_fixture(
        tmp_path,
        monkeypatch,
        outcome=ArtifactAdoption.NOT_ADOPTED,
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await service.store_tool_image(
            session=session_type(),
            task_id="comp-commit-failure",
            attempt_epoch=1,
            execution_epoch=4,
            user_id="user-1",
            message_id="message-1",
            raw_image=_png_bytes(32, 24),
            revised_prompt=None,
            billing_budget_micro=100,
        )

    assert len(reserved) == 1
    assert reserved[0] > len(_png_bytes(32, 24))
    assert lease.released == 1
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


@pytest.mark.asyncio
async def test_tool_image_cancel_intent_rolls_back_and_removes_storage_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session_type, _reserved, lease = _tool_image_commit_fixture(
        tmp_path,
        monkeypatch,
        outcome=ArtifactAdoption.NOT_ADOPTED,
    )

    async def reject_cancelled_usage(**_kwargs: Any) -> None:
        raise completion._CompletionEpochSuperseded(
            "completion tool image superseded by cancel intent"
        )

    service = replace(
        service,
        repository=replace(
            service.repository,
            record_usage=reject_cancelled_usage,
        ),
    )
    session = session_type()
    with pytest.raises(
        completion._CompletionEpochSuperseded,
        match="cancel intent",
    ):
        await service.store_tool_image(
            session=session,
            task_id="comp-cancelled",
            attempt_epoch=2,
            execution_epoch=4,
            user_id="user-1",
            message_id="message-1",
            raw_image=_png_bytes(32, 24),
            revised_prompt=None,
            billing_budget_micro=100,
        )

    assert session.rolled_back is True
    assert lease.released == 1
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


@pytest.mark.asyncio
async def test_tool_image_lost_commit_ack_keeps_adopted_storage_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session_type, _reserved, _lease = _tool_image_commit_fixture(
        tmp_path,
        monkeypatch,
        outcome=ArtifactAdoption.ADOPTED,
    )

    payload = await service.store_tool_image(
        session=session_type(),
        task_id="comp-commit-adopted",
        attempt_epoch=2,
        execution_epoch=4,
        user_id="user-1",
        message_id="message-1",
        raw_image=_png_bytes(32, 24),
        revised_prompt=None,
        billing_budget_micro=100,
    )

    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert payload["image_id"]
    assert len(files) == 4
    assert all("/executions/4/attempts/2/" in str(path) for path in files)


@pytest.mark.asyncio
async def test_tool_image_unknown_commit_keeps_files_for_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, session_type, _reserved, _lease = _tool_image_commit_fixture(
        tmp_path,
        monkeypatch,
        outcome=ArtifactAdoption.UNKNOWN,
    )

    with pytest.raises(
        completion._CompletionEpochSuperseded,
        match="commit outcome unknown",
    ):
        await service.store_tool_image(
            session=session_type(),
            task_id="comp-commit-unknown",
            attempt_epoch=3,
            execution_epoch=5,
            user_id="user-1",
            message_id="message-1",
            raw_image=_png_bytes(32, 24),
            revised_prompt=None,
            billing_budget_micro=100,
        )

    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(files) == 4
    assert all("/executions/5/attempts/3/" in str(path) for path in files)


@pytest.mark.asyncio
async def test_tool_image_adoption_rejects_same_attempt_from_old_execution() -> None:
    completion_row = SimpleNamespace(
        attempt=2,
        execution_epoch=8,
        user_id="user-1",
    )
    image_row = SimpleNamespace(
        user_id="user-1",
        storage_key="key-orig",
        sha256="sha-1",
        metadata_jsonb={
            "completion_id": "comp-1",
            "completion_attempt_epoch": 2,
            "completion_execution_epoch": 7,
        },
    )

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, model: Any, _row_id: str, **_kwargs: Any) -> Any:
            if model is completion.Completion:
                return completion_row
            return image_row

    async def acquire_task_lock(_session: Any, _task_id: str) -> None:
        return None

    service = SimpleNamespace(
        repository=SimpleNamespace(
            session_factory=lambda: Session(),
            acquire_task_lock=acquire_task_lock,
            completion_model=completion.Completion,
            image_model=object(),
        )
    )

    result = await CompletionToolImageService._probe_tool_image_adoption(
        service,  # type: ignore[arg-type]
        task_id="comp-1",
        attempt_epoch=2,
        execution_epoch=7,
        image_id="image-1",
        key_orig="key-orig",
        sha="sha-1",
    )

    assert result is ArtifactAdoption.UNKNOWN
