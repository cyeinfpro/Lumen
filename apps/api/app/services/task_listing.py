from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import CompletionStatus, GenerationStatus
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
)
from lumen_core.schemas import TaskItemOut, TaskListOut


TaskCursor = tuple[datetime, Literal["generation", "completion"], str] | None


@dataclass(frozen=True, slots=True)
class TaskListRequest:
    user_id: str
    status: str | None
    kind: Literal["all", "generation", "completion"]
    source: str | None
    conversation_id: str | None
    project_id: str | None
    date_filter: str | None
    cursor: TaskCursor
    error_code: str | None
    retryable: bool | None
    limit: int


@dataclass(frozen=True)
class TaskListingRuntime:
    apply_cursor: Callable[..., Any]
    apply_date_filter: Callable[..., Any]
    apply_item_filters: Callable[..., Any]
    build_item: Callable[..., TaskItemOut]
    encode_cursor: Callable[[datetime, str, str], str]
    json_dict: Callable[[Any], dict[str, Any]]
    kind_rank: Callable[[str], int]
    sort_at: Callable[[Generation | Completion], datetime]
    sort_expr: Callable[[Any], Any]
    variant_thumb_url: Callable[[str, set[str]], str]


def _status_statements(
    gen_stmt: Select,  # type: ignore[type-arg]
    comp_stmt: Select,  # type: ignore[type-arg]
    status: str | None,
) -> tuple[Select, Select]:  # type: ignore[type-arg]
    if status == "active":
        return (
            gen_stmt.where(
                Generation.status.in_(
                    [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
                )
            ),
            comp_stmt.where(
                Completion.status.in_(
                    [CompletionStatus.QUEUED.value, CompletionStatus.STREAMING.value]
                )
            ),
        )
    if status == "terminal":
        return (
            gen_stmt.where(
                Generation.status.in_(
                    [
                        GenerationStatus.SUCCEEDED.value,
                        GenerationStatus.FAILED.value,
                        GenerationStatus.CANCELED.value,
                    ]
                )
            ),
            comp_stmt.where(
                Completion.status.in_(
                    [
                        CompletionStatus.SUCCEEDED.value,
                        CompletionStatus.FAILED.value,
                        CompletionStatus.CANCELED.value,
                    ]
                )
            ),
        )
    if status in {
        GenerationStatus.RUNNING.value,
        CompletionStatus.STREAMING.value,
    }:
        return (
            gen_stmt.where(Generation.status == GenerationStatus.RUNNING.value),
            comp_stmt.where(Completion.status == CompletionStatus.STREAMING.value),
        )
    if status:
        return (
            gen_stmt.where(Generation.status == status),
            comp_stmt.where(Completion.status == status),
        )
    return gen_stmt, comp_stmt


def _query_statements(
    runtime: TaskListingRuntime,
    *,
    user_id: str,
    status: str | None,
    error_code: str | None,
    date_filter: str | None,
    cursor: TaskCursor,
    request: TaskListRequest,
) -> tuple[Select, Select]:  # type: ignore[type-arg]
    gen_stmt: Select = select(Generation).where(  # type: ignore[assignment]
        Generation.user_id == user_id
    )
    comp_stmt: Select = select(Completion).where(  # type: ignore[assignment]
        Completion.user_id == user_id
    )
    gen_stmt, comp_stmt = _status_statements(gen_stmt, comp_stmt, status)
    if error_code:
        gen_stmt = gen_stmt.where(Generation.error_code == error_code)
        comp_stmt = comp_stmt.where(Completion.error_code == error_code)
    gen_stmt = runtime.apply_date_filter(gen_stmt, Generation, date_filter)
    comp_stmt = runtime.apply_date_filter(comp_stmt, Completion, date_filter)
    # 可精确求值的 item 过滤条件在 SQL 层下推(runtime.apply_item_filters):
    # 零匹配 filter 首轮即返回空集,深挖循环立刻终止,不会把整个用户任务表
    # 逐批拉完;推导值(source 回退等)仍由内存 _item_matches 兜底过滤。
    gen_stmt = runtime.apply_item_filters(gen_stmt, Generation, request)
    comp_stmt = runtime.apply_item_filters(comp_stmt, Completion, request)
    return (
        runtime.apply_cursor(
            gen_stmt,
            Generation,
            cursor,
            model_kind="generation",
        ),
        runtime.apply_cursor(
            comp_stmt,
            Completion,
            cursor,
            model_kind="completion",
        ),
    )


async def _task_rows(
    db: AsyncSession,
    runtime: TaskListingRuntime,
    *,
    gen_stmt: Select,  # type: ignore[type-arg]
    comp_stmt: Select,  # type: ignore[type-arg]
    kind: Literal["all", "generation", "completion"],
    query_limit: int,
) -> tuple[list[Generation], list[Completion]]:
    generations: list[Generation] = []
    completions: list[Completion] = []
    if kind in {"all", "generation"}:
        generations = list(
            (
                await db.execute(
                    gen_stmt.order_by(
                        runtime.sort_expr(Generation).desc(),
                        Generation.id.desc(),
                    ).limit(query_limit)
                )
            )
            .scalars()
            .all()
        )
    if kind in {"all", "completion"}:
        completions = list(
            (
                await db.execute(
                    comp_stmt.order_by(
                        runtime.sort_expr(Completion).desc(),
                        Completion.id.desc(),
                    ).limit(query_limit)
                )
            )
            .scalars()
            .all()
        )
    return generations, completions


async def _message_meta(
    db: AsyncSession,
    runtime: TaskListingRuntime,
    generations: list[Generation],
    completions: list[Completion],
) -> dict[str, tuple[str, dict[str, Any]]]:
    message_ids = [
        *[generation.message_id for generation in generations],
        *[completion.message_id for completion in completions],
    ]
    if not message_ids:
        return {}
    rows = (
        await db.execute(
            select(Message.id, Message.conversation_id, Message.content).where(
                Message.id.in_(message_ids)
            )
        )
    ).all()
    return {
        message_id: (conversation_id, runtime.json_dict(content))
        for message_id, conversation_id, content in rows
    }


async def _conversation_defaults(
    db: AsyncSession,
    runtime: TaskListingRuntime,
    message_meta: dict[str, tuple[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    conversation_ids = {
        conversation_id
        for conversation_id, _content in message_meta.values()
        if conversation_id
    }
    if not conversation_ids:
        return {}
    rows = (
        await db.execute(
            select(Conversation.id, Conversation.default_params).where(
                Conversation.id.in_(conversation_ids)
            )
        )
    ).all()
    return {
        conversation_id: runtime.json_dict(params) for conversation_id, params in rows
    }


async def _generation_media(
    db: AsyncSession,
    generations: list[Generation],
) -> tuple[dict[str, Image], dict[str, set[str]]]:
    if not generations:
        return {}, {}
    image_rows = (
        (
            await db.execute(
                select(Image)
                .where(
                    Image.owner_generation_id.in_(
                        [generation.id for generation in generations]
                    ),
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at.asc(), Image.id.asc())
            )
        )
        .scalars()
        .all()
    )
    image_by_generation: dict[str, Image] = {}
    for image in image_rows:
        owner_id = image.owner_generation_id
        if owner_id and owner_id not in image_by_generation:
            image_by_generation[owner_id] = image
    if not image_by_generation:
        return {}, {}
    rows = (
        await db.execute(
            select(ImageVariant.image_id, ImageVariant.kind).where(
                ImageVariant.image_id.in_(
                    [image.id for image in image_by_generation.values()]
                )
            )
        )
    ).all()
    variant_kinds: dict[str, set[str]] = {}
    for image_id, variant_kind in rows:
        variant_kinds.setdefault(image_id, set()).add(variant_kind)
    return image_by_generation, variant_kinds


async def _queue_positions(
    db: AsyncSession,
    runtime: TaskListingRuntime,
    user_id: str,
) -> dict[str, int]:
    queued_ids = (
        (
            await db.execute(
                select(Generation.id)
                .where(
                    Generation.user_id == user_id,
                    Generation.status == GenerationStatus.QUEUED.value,
                )
                .order_by(runtime.sort_expr(Generation).asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return {generation_id: index + 1 for index, generation_id in enumerate(queued_ids)}


def _item_matches(
    item: TaskItemOut,
    *,
    source: str | None,
    conversation_id: str | None,
    project_id: str | None,
    retryable: bool | None,
) -> bool:
    if source and item.source != source:
        return False
    if conversation_id and item.conversation_id != conversation_id:
        return False
    if project_id and item.project_id != project_id:
        return False
    return retryable is None or item.retryable is retryable


def _sortable_item(
    runtime: TaskListingRuntime,
    task: Generation | Completion,
    item_kind: Literal["generation", "completion"],
    *,
    message_meta: dict[str, tuple[str, dict[str, Any]]],
    conversation_defaults: dict[str, dict[str, Any]],
    images: dict[str, Image],
    variant_kinds: dict[str, set[str]],
    queue_positions: dict[str, int],
    source: str | None,
    conversation_id: str | None,
    project_id: str | None,
    retryable: bool | None,
) -> tuple[datetime, str, str, TaskItemOut] | None:
    message_conversation_id, message_content = message_meta.get(
        task.message_id,
        (None, {}),
    )
    sort_at = runtime.sort_at(task)
    thumb_url = None
    if item_kind == "generation":
        image = images.get(task.id)
        if image is not None:
            thumb_url = runtime.variant_thumb_url(
                image.id,
                variant_kinds.get(image.id, set()),
            )
    item = runtime.build_item(
        item_kind,
        task,
        conversation_id=message_conversation_id,
        message_content=message_content,
        conversation_default_params=conversation_defaults.get(
            message_conversation_id or ""
        ),
        thumb_url=thumb_url,
        queue_position=queue_positions.get(task.id),
        sort_at=sort_at,
    )
    if not _item_matches(
        item,
        source=source,
        conversation_id=conversation_id,
        project_id=project_id,
        retryable=retryable,
    ):
        return None
    return sort_at, item_kind, task.id, item


def _sortable_items(
    runtime: TaskListingRuntime,
    generations: list[Generation],
    completions: list[Completion],
    **kwargs: Any,
) -> list[tuple[datetime, str, str, TaskItemOut]]:
    items = [
        _sortable_item(runtime, task, item_kind, **kwargs)
        for task, item_kind in [
            *[(generation, "generation") for generation in generations],
            *[(completion, "completion") for completion in completions],
        ]
    ]
    return [item for item in items if item is not None]


def _task_key(
    runtime: TaskListingRuntime,
    sort_at: datetime,
    kind: str,
    task_id: str,
) -> tuple[datetime, int, str]:
    """Merged descending sort key of a task, used to compare window positions."""
    return (sort_at, runtime.kind_rank(kind), task_id)


def _tail_above_page_key(
    runtime: TaskListingRuntime,
    rows: list[Generation] | list[Completion],
    kind: Literal["generation", "completion"],
    last_page_key: tuple[datetime, int, str] | None,
) -> bool:
    """Whether a full per-table batch may still hide matches above the page tail.

    Batches are cut by (sort_expr, id) inside each table; when the tail of a
    full batch still sorts above the last page item in the merged order, rows
    deeper in that stream could hold matches that a cursor at the page tail
    would silently skip on the next page.
    """
    if not rows:
        return False
    tail = rows[-1]
    tail_key = (runtime.sort_at(tail), runtime.kind_rank(kind), tail.id)
    return last_page_key is None or tail_key > last_page_key


async def _deeper_batch(
    db: AsyncSession,
    runtime: TaskListingRuntime,
    request: TaskListRequest,
    *,
    rows: list[Generation] | list[Completion],
    kind: Literal["generation", "completion"],
    query_limit: int,
) -> tuple[list[Generation], list[Completion]]:
    """Fetch the next batch of one table, strictly below its current tail."""
    tail = rows[-1]
    gen_stmt, comp_stmt = _query_statements(
        runtime,
        user_id=request.user_id,
        status=request.status,
        error_code=request.error_code,
        date_filter=request.date_filter,
        cursor=(runtime.sort_at(tail), kind, tail.id),
        request=request,
    )
    return await _task_rows(
        db,
        runtime,
        gen_stmt=gen_stmt,
        comp_stmt=comp_stmt,
        kind=kind,
        query_limit=query_limit,
    )


async def build_task_list(
    db: AsyncSession,
    runtime: TaskListingRuntime,
    request: TaskListRequest,
) -> TaskListOut:
    query_limit = min(max(request.limit * 3, request.limit + 1), 1000)
    gen_stmt, comp_stmt = _query_statements(
        runtime,
        user_id=request.user_id,
        status=request.status,
        error_code=request.error_code,
        date_filter=request.date_filter,
        cursor=request.cursor,
        request=request,
    )
    generations, completions = await _task_rows(
        db,
        runtime,
        gen_stmt=gen_stmt,
        comp_stmt=comp_stmt,
        kind=request.kind,
        query_limit=query_limit,
    )
    # A per-table batch is "full" when the query returned a whole batch: more
    # rows may still exist deeper in that stream.  Matching happens in memory
    # (`_item_matches`), so with sparse filters the first batches can hold
    # fewer than `limit` matches even though more exist below; deciding
    # `next_cursor` from the match count alone would silently drop them.
    # Exact item filters are pushed down to SQL (see `_query_statements`), so a
    # zero-match filter makes both batches empty here and the loop exits after
    # one round instead of draining the user's tables one batch at a time.
    gen_full = (
        request.kind in {"all", "generation"} and len(generations) >= query_limit
    )
    comp_full = (
        request.kind in {"all", "completion"} and len(completions) >= query_limit
    )

    sortable: list[tuple[datetime, str, str, TaskItemOut]] = []
    page: list[tuple[datetime, str, str, TaskItemOut]] = []
    queue_positions: dict[str, int] = {}
    queue_positions_fetched = False
    while True:
        message_meta = await _message_meta(db, runtime, generations, completions)
        defaults = await _conversation_defaults(db, runtime, message_meta)
        images, variant_kinds = await _generation_media(db, generations)
        if not queue_positions_fetched:
            queue_positions = await _queue_positions(db, runtime, request.user_id)
            queue_positions_fetched = True
        sortable = _sortable_items(
            runtime,
            generations,
            completions,
            message_meta=message_meta,
            conversation_defaults=defaults,
            images=images,
            variant_kinds=variant_kinds,
            queue_positions=queue_positions,
            source=request.source,
            conversation_id=request.conversation_id,
            project_id=request.project_id,
            retryable=request.retryable,
        )
        sortable.sort(
            key=lambda pair: (pair[0], runtime.kind_rank(pair[1]), pair[2]),
            reverse=True,
        )
        page = sortable[: request.limit]
        last_key = _task_key(runtime, *page[-1][:3]) if page else None
        need_gens = gen_full and _tail_above_page_key(
            runtime, generations, "generation", last_key
        )
        need_comps = comp_full and _tail_above_page_key(
            runtime, completions, "completion", last_key
        )
        if not need_gens and not need_comps:
            break
        if need_gens:
            extra_gens, _extra_comps = await _deeper_batch(
                db,
                runtime,
                request,
                rows=generations,
                kind="generation",
                query_limit=query_limit,
            )
            generations.extend(extra_gens)
            gen_full = len(extra_gens) >= query_limit
        if need_comps:
            extra_comps, _extra_gens = await _deeper_batch(
                db,
                runtime,
                request,
                rows=completions,
                kind="completion",
                query_limit=query_limit,
            )
            completions.extend(extra_comps)
            comp_full = len(extra_comps) >= query_limit
    next_cursor = None
    if page and (len(sortable) > request.limit or gen_full or comp_full):
        sort_at, item_kind, task_id, _item = page[-1]
        next_cursor = runtime.encode_cursor(sort_at, item_kind, task_id)
    return TaskListOut(
        items=[item for *_prefix, item in page],
        next_cursor=next_cursor,
    )
