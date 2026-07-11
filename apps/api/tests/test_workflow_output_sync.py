from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.routes import workflows
from app.workflow_services import output_sync, output_values


class _Result:
    def __init__(self, rows: Sequence[Any]) -> None:
        self._rows = list(rows)

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _Db:
    def __init__(self, responses: Sequence[Sequence[Any]]) -> None:
        self._responses = [list(response) for response in responses]
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self._responses.pop(0))

    def add(self, row: Any) -> None:
        raise AssertionError(f"unexpected row mutation: {row!r}")

    async def flush(self) -> None:
        raise AssertionError("output synchronization must not flush")

    async def commit(self) -> None:
        raise AssertionError("output synchronization must not commit")


def test_route_private_output_sync_exports_remain_compatible() -> None:
    names = (
        "MODEL_CANDIDATE_COUNT",
        "PRODUCT_ANALYSIS_FIELDS",
        "_candidate_generated_image_ids",
        "_clamp_score",
        "_coerce_string_list",
        "_extract_jsonish_value",
        "_failed_generation_output",
        "_generation_batch_outcome",
        "_load_quality_reports",
        "_lock_workflow_run_for_sync",
        "_merge_quality_summary_payload",
        "_normalize_product_analysis_payload",
        "_quality_payload_from_text",
        "_quality_summary_payload",
        "_showcase_expected_image_count",
        "_sync_quality_reports_from_tasks",
        "_sync_workflow_outputs",
        "_task_error_summary",
        "_try_parse_json_text",
    )

    for name in names:
        assert getattr(workflows, name) is getattr(output_sync, name)

    value_names = (
        "MODEL_CANDIDATE_COUNT",
        "PRODUCT_ANALYSIS_FIELDS",
        "_candidate_generated_image_ids",
        "_clamp_score",
        "_coerce_string_list",
        "_extract_jsonish_value",
        "_failed_generation_output",
        "_generation_batch_outcome",
        "_merge_quality_summary_payload",
        "_normalize_product_analysis_payload",
        "_quality_payload_from_text",
        "_quality_summary_payload",
        "_showcase_expected_image_count",
        "_task_error_summary",
        "_try_parse_json_text",
    )
    for name in value_names:
        assert getattr(output_sync, name) is getattr(output_values, name)


@pytest.mark.asyncio
async def test_sync_reads_locked_steps_without_owning_transaction_boundary() -> None:
    run = SimpleNamespace(
        id="run-1",
        user_id="user-1",
        current_step="upload_product",
        status="draft",
    )
    db = _Db([[], []])

    await output_sync._sync_workflow_outputs(  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        run,
    )

    assert len(db.statements) == 2
    rendered_steps_query = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "workflow_steps" in rendered_steps_query
    assert "FOR UPDATE" in rendered_steps_query
