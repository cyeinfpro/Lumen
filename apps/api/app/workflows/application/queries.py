"""Workflow query services."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json

from ..domain.json_types import JsonObject
from ..domain.models import WorkflowRunSnapshot
from ..ports.repositories import WorkflowRepository
from ..ports.run_reads import (
    WorkflowRunCursor,
    WorkflowRunListRecord,
    WorkflowRunReadPort,
)
from .errors import InvalidWorkflowCursorError, WorkflowNotFoundError


_WORKFLOW_CURSOR_VERSION = 1
_HIDDEN_PROJECT_WORKFLOW_TYPES = (
    "apparel_model_library_generate",
    "poster_style_library_generate",
)
_POSTER_WORKFLOW_TYPE = "poster_design"


@dataclass(frozen=True)
class WorkflowRunListItem:
    id: str
    conversation_id: str | None
    type: str
    status: str
    title: str
    user_prompt: str
    product_image_ids: tuple[str, ...]
    current_step: str
    quality_mode: str
    metadata_jsonb: JsonObject
    created_at: datetime
    updated_at: datetime
    output_count: int
    next_action: str


@dataclass(frozen=True)
class WorkflowRunListResult:
    items: tuple[WorkflowRunListItem, ...]
    next_cursor: str | None


def _cursor_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _encode_cursor(
    item: WorkflowRunListRecord,
    *,
    workflow_type: str | None,
) -> str:
    raw = json.dumps(
        {
            "v": _WORKFLOW_CURSOR_VERSION,
            "updated_at": _cursor_timestamp(item.updated_at).isoformat(),
            "id": item.id,
            "type": workflow_type or "",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str | None,
    *,
    workflow_type: str | None,
) -> WorkflowRunCursor | None:
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((cursor + padding).encode("ascii")).decode("utf-8")
        )
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        if payload.get("v") != _WORKFLOW_CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        if payload.get("type") != (workflow_type or ""):
            raise ValueError("cursor filter mismatch")
        row_id = payload.get("id")
        updated_at_raw = payload.get("updated_at")
        if not isinstance(row_id, str) or not row_id or len(row_id) > 128:
            raise ValueError("invalid cursor id")
        if not isinstance(updated_at_raw, str):
            raise ValueError("invalid cursor timestamp")
        updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
        if updated_at.tzinfo is None:
            raise ValueError("cursor timestamp must include timezone")
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise InvalidWorkflowCursorError from exc
    return WorkflowRunCursor(
        updated_at=updated_at.astimezone(timezone.utc),
        run_id=row_id,
    )


def _next_action_for(item: WorkflowRunListRecord) -> str:
    if item.status == "completed":
        return "查看交付"
    if item.type == _POSTER_WORKFLOW_TYPE:
        return {
            "copy_analysis": "确认海报文案",
            "master_generation": "生成母版方案",
            "master_approval": "选定母版",
            "multi_size_generation": "生成/确认多尺寸",
            "delivery": "下载海报成品",
        }.get(item.current_step, "继续海报项目")
    return {
        "product_analysis": "确认商品约束",
        "model_settings": "生成模特候选",
        "model_candidates": "等待模特候选",
        "model_approval": "确认模特",
        "showcase_generation": "开始生成展示图",
        "quality_review": "查看质检",
        "delivery": "下载最终图",
    }.get(item.current_step, "继续项目")


def _list_item(item: WorkflowRunListRecord) -> WorkflowRunListItem:
    return WorkflowRunListItem(
        id=item.id,
        conversation_id=item.conversation_id,
        type=item.type,
        status=item.status,
        title=item.title,
        user_prompt=item.user_prompt,
        product_image_ids=item.product_image_ids,
        current_step=item.current_step,
        quality_mode=item.quality_mode,
        metadata_jsonb=dict(item.metadata_jsonb),
        created_at=item.created_at,
        updated_at=item.updated_at,
        output_count=item.output_count,
        next_action=_next_action_for(item),
    )


@dataclass(frozen=True)
class ListWorkflowRuns:
    read_port: WorkflowRunReadPort

    async def list_runs(
        self,
        *,
        user_id: str,
        workflow_type: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> WorkflowRunListResult:
        if not 1 <= limit <= 100:
            raise ValueError("workflow list limit must be between 1 and 100")
        normalized_type = workflow_type or None
        page = await self.read_port.list_runs(
            user_id=user_id,
            workflow_type=normalized_type,
            excluded_types=(() if normalized_type else _HIDDEN_PROJECT_WORKFLOW_TYPES),
            after=_decode_cursor(cursor, workflow_type=normalized_type),
            limit=limit,
        )
        return WorkflowRunListResult(
            items=tuple(_list_item(item) for item in page.items),
            next_cursor=(
                _encode_cursor(page.items[-1], workflow_type=normalized_type)
                if page.has_more and page.items
                else None
            ),
        )


@dataclass(frozen=True)
class GetWorkflowRun:
    repository: WorkflowRepository

    async def execute(self, *, user_id: str, run_id: str) -> WorkflowRunSnapshot:
        run = await self.repository.get(user_id=user_id, run_id=run_id)
        if run is None:
            raise WorkflowNotFoundError(run_id)
        return run


__all__ = [
    "GetWorkflowRun",
    "ListWorkflowRuns",
    "WorkflowRunListItem",
    "WorkflowRunListResult",
]
