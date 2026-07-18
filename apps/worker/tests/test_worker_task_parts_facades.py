from __future__ import annotations

# ruff: noqa: SLF001

import pytest

from app.tasks import completion, context_summary, video_generation, volcano_assets
from app.tasks.completion_parts import request_metadata
from app.tasks.context_summary_parts import results as context_summary_results
from app.tasks.video_generation_parts import errors as video_errors
from app.tasks.volcano_assets_parts import receipts as volcano_receipts


def test_extracted_helpers_remain_available_from_task_facades() -> None:
    assert completion._split_csv_ids is request_metadata._split_csv_ids
    assert (
        completion._merge_completion_upstream_metadata
        is request_metadata._merge_completion_upstream_metadata
    )
    assert context_summary._SummaryRequest is context_summary_results.SummaryRequest
    assert (
        context_summary._worker_compact_summary_payload
        is context_summary_results.worker_compact_summary_payload
    )
    assert video_generation._video_exception_code is video_errors.video_exception_code
    assert (
        video_generation._append_bounded_history is video_errors.append_bounded_history
    )
    assert volcano_assets._receipt_result is volcano_receipts.receipt_result
    assert (
        volcano_assets._validated_receipt_result
        is volcano_receipts.validated_receipt_result
    )


@pytest.mark.asyncio
async def test_volcano_receipt_facade_injects_current_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = object()
    fence = object()
    calls: list[tuple[str, object, object]] = []

    async def read_impl(
        operation: dict[str, object],
        *,
        fence: object,
        session_factory: object,
    ) -> dict[str, object]:
        calls.append(("read", fence, session_factory))
        return operation

    async def write_impl(
        operation: dict[str, object],
        result: dict[str, object],
        *,
        fence: object,
        session_factory: object,
    ) -> None:
        del operation, result
        calls.append(("write", fence, session_factory))

    monkeypatch.setattr(volcano_assets, "SessionLocal", session_factory)
    monkeypatch.setattr(volcano_assets, "_read_success_receipt_impl", read_impl)
    monkeypatch.setattr(volcano_assets, "_write_success_receipt_impl", write_impl)

    operation = {"id": "operation-1"}
    assert (
        await volcano_assets._read_success_receipt(operation, fence=fence) == operation
    )
    await volcano_assets._write_success_receipt(
        operation, {"id": "asset-1"}, fence=fence
    )

    assert calls == [
        ("read", fence, session_factory),
        ("write", fence, session_factory),
    ]
