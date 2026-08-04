from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from app.routes import tasks
from app.services.task_listing import TaskListRequest, build_task_list
from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
    GenerationStage,
    GenerationStatus,
)
from lumen_core.model_entities import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
)


class _Result:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalars(self) -> _Result:
        return self

    def all(self) -> Any:
        return self.value if isinstance(self.value, list) else []


class _Db:
    def __init__(
        self,
        results: list[_Result],
        *,
        active_account_mode: str = "wallet",
    ) -> None:
        self.results = results
        self.active_account_mode = active_account_mode
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.committed = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        if "from users" in str(statement).lower():
            active_user = _user()
            active_user.account_mode = self.active_account_mode
            return _Result(active_user)
        return self.results.pop(0) if self.results else _Result()

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = "outbox-1"

    async def commit(self) -> None:
        self.committed = True


class _Redis:
    def __init__(
        self,
        values: dict[str, Any] | None = None,
        *,
        fail_delete: bool = False,
    ) -> None:
        self.values = values or {}
        self.fail_delete = fail_delete
        self.calls: list[tuple[Any, ...]] = []

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.calls.append(("set", key, value, ex))

    async def get(self, key: str) -> Any:
        self.calls.append(("get", key))
        return self.values.get(key)

    async def zrem(self, key: str, member: str) -> None:
        self.calls.append(("zrem", key, member))

    async def delete(self, *keys: str) -> None:
        self.calls.append(("delete", *keys))
        if self.fail_delete:
            raise RuntimeError("redis delete failed")

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        self.calls.append(("eval", script, numkeys, *args))
        return 1


def _user() -> SimpleNamespace:
    return SimpleNamespace(id="user-1", account_mode="wallet")


async def _billing_disabled(_db: Any) -> bool:
    return False


async def _billing_enabled_true(_db: Any) -> bool:
    return True


async def _billing_allow_negative_false(_db: Any) -> bool:
    return False


async def _noop_invalidate(_user_id: str) -> None:
    return None


def _retry_candidate(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": "gen-1",
        "user_id": "user-1",
        "message_id": "assistant-1",
        "status": GenerationStatus.CANCELED.value,
        "progress_stage": GenerationStage.FINALIZING.value,
        "attempt": 1,
        "execution_epoch": 0,
        "billing_retry_count": 0,
        "error_code": "cancelled",
        "error_message": "cancelled by user",
        "started_at": None,
        "finished_at": datetime.now(timezone.utc),
        "cancel_requested_at": datetime.now(timezone.utc),
        "size_requested": "2048x2048",
        "upstream_request": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _task_record(**overrides: Any) -> Any:
    created = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    values: dict[str, Any] = {
        "id": "gen-1",
        "message_id": "msg-1",
        "status": GenerationStatus.QUEUED.value,
        "progress_stage": GenerationStage.QUEUED.value,
        "started_at": None,
        "created_at": created,
        "finished_at": None,
        "upstream_request": {},
        "error_code": None,
        "error_message": None,
        "attempt": 0,
        "prompt": "render an image",
        "primary_input_image_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_task_item_exposes_live_route_fields() -> None:
    created = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    task = _task_record(
        upstream_request={
            "workflow_run_id": "project-1",
            "action_source": "composer.reroll",
            "trace_id": "trace-request-1",
            "queue_metadata": {
                "queue_lane": "image:workflow:large",
                "workflow_type": "apparel_model_showcase",
                "workflow_step_key": "showcase_generation",
                "pixel_count": 8_294_400,
                "size_bucket": "large",
                "cost_class": "large",
                "queue_wait_ms": 3200,
            },
        },
        error_code="provider_exhausted",
        error_message="all providers are busy",
        attempt=0,
        prompt="render a wide hero image",
        primary_input_image_id="source-image-1",
    )

    item = tasks._build_task_item(  # noqa: SLF001
        "generation",
        task,
        conversation_id="conv-1",
        conversation_default_params={"telegram": True},
        thumb_url="/api/images/image-1/variants/thumb256",
        queue_position=4,
        sort_at=created,
    )

    assert (
        item.kind,
        item.id,
        item.message_id,
        item.status,
        item.progress_stage,
        item.stage,
    ) == (
        "generation",
        "gen-1",
        "msg-1",
        GenerationStatus.QUEUED.value,
        GenerationStage.QUEUED.value,
        GenerationStage.QUEUED.value,
    )
    assert (item.started_at, item.finished_at, item.created_at, item.date) == (
        None,
        None,
        created,
        created,
    )
    assert tasks._decode_task_cursor(item.cursor) == (  # noqa: SLF001
        created,
        "generation",
        "gen-1",
    )
    assert (
        item.conversation_id,
        item.project_id,
        item.source,
        item.workflow_type,
        item.workflow_step_key,
    ) == (
        "conv-1",
        "project-1",
        "project",
        "apparel_model_showcase",
        "showcase_generation",
    )
    assert (item.action_source, item.trace_id) == (
        "composer.reroll",
        "trace-request-1",
    )
    assert (
        item.queue_lane,
        item.pixel_count,
        item.size_bucket,
        item.cost_class,
        item.queue_wait_ms,
        item.queue_position,
    ) == ("image:workflow:large", 8_294_400, "large", "large", 3200, 4)
    assert (item.title, item.prompt, item.source_image_id, item.thumb_url) == (
        "render a wide hero image",
        "render a wide hero image",
        "source-image-1",
        "/api/images/image-1/variants/thumb256",
    )
    assert (item.error_code, item.error_message, item.retryable) == (
        "provider_exhausted",
        "all providers are busy",
        False,
    )
    assert item.recommended_actions == []
    assert (item.substage, item.retrying, item.waiting_provider, item.cancelled) == (
        "waiting_provider",
        False,
        True,
        False,
    )


@pytest.mark.parametrize(
    ("request_substage", "diagnostics_substage", "expected"),
    [
        ("request_stage", "diagnostics_stage", "request_stage"),
        (None, "diagnostics_stage", "diagnostics_stage"),
        (None, None, "waiting_provider"),
    ],
)
def test_build_task_item_substage_priority(
    request_substage: str | None,
    diagnostics_substage: str | None,
    expected: str,
) -> None:
    upstream_request: dict[str, Any] = {}
    if request_substage is not None:
        upstream_request["substage"] = request_substage
    if diagnostics_substage is not None:
        upstream_request["generation_diagnostics"] = {
            "substage": diagnostics_substage,
        }
    task = _task_record(
        upstream_request=upstream_request,
        error_code="provider_exhausted",
    )

    item = tasks._build_task_item("generation", task)  # noqa: SLF001

    assert item.substage == expected


@pytest.mark.asyncio
async def test_list_tasks_uses_shared_task_item_builder() -> None:
    created = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    task = _task_record()
    image = SimpleNamespace(id="image-1", owner_generation_id="gen-1")
    db = _Db(
        [
            _Result([task]),
            _Result([("msg-1", "conv-1", {})]),
            _Result([("conv-1", {})]),
            _Result([image]),
            _Result([("image-1", "thumb256")]),
            _Result(["gen-1"]),
        ]
    )

    output = await tasks.list_tasks(
        user=_user(),  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        query=tasks.TaskListQuery(kind="generation", limit=10),
    )

    expected = tasks._build_task_item(  # noqa: SLF001
        "generation",
        task,
        conversation_id="conv-1",
        message_content={},
        conversation_default_params={},
        thumb_url="/api/images/image-1/variants/thumb256",
        queue_position=1,
        sort_at=created,
    )
    assert output.items == [expected]


@pytest.mark.asyncio
async def test_list_tasks_preserves_cross_kind_sort_order() -> None:
    created = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    generation = _task_record(id="gen-1", created_at=created)
    completion = _task_record(
        id="comp-1",
        message_id="msg-2",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        created_at=created,
    )
    db = _Db(
        [
            _Result([generation]),
            _Result([completion]),
            _Result(
                [
                    ("msg-1", "conv-1", {}),
                    ("msg-2", "conv-1", {}),
                ]
            ),
            _Result([("conv-1", {})]),
            _Result([]),
            _Result(["gen-1"]),
        ]
    )

    output = await tasks.list_tasks(
        user=_user(),  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        query=tasks.TaskListQuery(kind="all", limit=10),
    )

    assert [(item.kind, item.id) for item in output.items] == [
        ("generation", "gen-1"),
        ("completion", "comp-1"),
    ]


def _paged_task_rows(n: int, prefix: str, *, base: datetime) -> list[Any]:
    """n 条 created_at 递增的任务,按最新在前排序,便于 _Db 逐页返回。"""
    rows = []
    for i in range(n):
        created = base + timedelta(seconds=n - i)
        rows.append(
            _task_record(
                id=f"{prefix}-{n - i:03d}",
                message_id=f"msg-{prefix}-{n - i:03d}",
                created_at=created,
                finished_at=created,
                upstream_request={"source": "X"},
            )
        )
    return rows


@pytest.mark.asyncio
async def test_list_tasks_sparse_source_filter_paginates_past_initial_batch() -> None:
    """稀疏 source 筛选时,初始 3x 窗口内匹配数 <= limit 也必须给游标继续翻页。

    修复前: 每表只抓 query_limit 行后在内存过滤,匹配稀疏时首页不足 limit 条
    即返回 next_cursor=None,窗口之外的匹配任务被静默丢弃。
    """
    base = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    gens = _paged_task_rows(200, "gen", base=base)
    matched_ids: set[str] = set()
    for gen in gens:
        if int(gen.id.split("-")[1]) % 10 != 1:
            gen.upstream_request = {"source": "Y"}
        else:
            matched_ids.add(gen.id)

    collected: list[str] = []
    cursor: str | None = None
    for page_no in range(5):
        start = page_no * 60
        db = _Db(
            [
                _Result(gens[start : start + 60]),
                _Result([]),  # message meta
                _Result([]),  # image meta
                _Result([]),  # queue positions
            ]
        )
        output = await tasks.list_tasks(
            user=_user(),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            query=tasks.TaskListQuery(
                kind="generation", limit=20, source="X", cursor=cursor
            ),
        )
        assert all(item.source == "X" for item in output.items)
        collected.extend(item.id for item in output.items)
        cursor = output.next_cursor
        if cursor is None:
            break
    # 修复前: 首页 6 条后 next_cursor=None,其余 14 条匹配永远不可见
    assert len(collected) == 20
    assert set(collected) == matched_ids


@pytest.mark.asyncio
async def test_list_tasks_does_not_skip_generation_matches_below_page_tail() -> None:
    """kind=all 时,generation 表尾部仍有匹配但页尾落在 completion 上时,必须
    继续拉深 generation 窗口,否则下一页游标会越过这些匹配行。

    修复前: 每表各自抓 top-query_limit,两表流不对齐时,游标(取页尾行)会落在
    较浅表上,较深表中排序在游标之上的未抓取匹配被永久跳过。
    """
    base_gen = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    base_comp = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    gens = _paged_task_rows(200, "gen", base=base_gen)
    matched_gen_ids: set[str] = set()
    for gen in gens:
        if int(gen.id.split("-")[1]) % 10 != 1:
            gen.upstream_request = {"source": "Y"}
        else:
            matched_gen_ids.add(gen.id)
    comps = _paged_task_rows(60, "comp", base=base_comp)

    # 首页: 首轮(gen, comp, msg, image, queue 共 5 次查询)后,generation 窗口
    # 尾部仍高于页尾(completion),需 3 轮深挖(每轮 gen_chunk, msg, image)。
    db = _Db(
        [
            _Result(gens[0:60]),
            _Result(comps),
            _Result([]),
            _Result([]),
            _Result([]),
            _Result(gens[60:120]),
            _Result([]),
            _Result([]),
            _Result(gens[120:180]),
            _Result([]),
            _Result([]),
            _Result(gens[180:200]),
            _Result([]),
            _Result([]),
        ]
    )
    output = await tasks.list_tasks(
        user=_user(),  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        query=tasks.TaskListQuery(kind="all", limit=20, source="X"),
    )
    collected = [item.id for item in output.items]
    # 修复前: 首页只有 6 个匹配 generation + 14 个 completion,其余 14 个匹配
    # generation 排序在下一页游标(t_comp, completion, ...)之上,永远不可见
    assert set(collected) == matched_gen_ids
    assert output.next_cursor is not None
    cursor = output.next_cursor

    for window in (comps[0:60], comps[20:60], comps[40:60]):
        db = _Db(
            [
                _Result([]),  # generation 已耗尽
                _Result(window),
                _Result([]),  # message meta
                _Result([]),  # queue positions
            ]
        )
        output = await tasks.list_tasks(
            user=_user(),  # type: ignore[arg-type]
            db=db,  # type: ignore[arg-type]
            query=tasks.TaskListQuery(kind="all", limit=20, source="X", cursor=cursor),
        )
        collected.extend(item.id for item in output.items)
        cursor = output.next_cursor
        if cursor is None:
            break
    assert set(collected) == matched_gen_ids | {comp.id for comp in comps}


def _apply_task_item_filters_pusher() -> Any:
    return tasks._task_listing_runtime().apply_item_filters  # noqa: SLF001


def test_apply_task_item_filters_pushes_exact_conditions_into_sql() -> None:
    """精确 item 过滤条件必须进 SQL,零匹配由数据库直接返回空集。

    深挖循环(修复的 pagination 逻辑)对零匹配 filter 会把用户整张任务表逐批
    拉完;conversation_id / retryable / 显式 source / project_id 下推后,
    零匹配首轮即空、循环立即退出。
    """
    apply_item_filters = _apply_task_item_filters_pusher()
    request = TaskListRequest(
        user_id="user-1",
        status=None,
        kind="generation",
        source="X",
        conversation_id="conv-1",
        project_id="proj-1",
        retryable=True,
        date_filter=None,
        cursor=None,
        error_code=None,
        limit=20,
    )
    stmt = apply_item_filters(
        select(Generation).where(Generation.user_id == "user-1"),
        Generation,
        request,
    )
    compiled = str(
        stmt.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True})
    ).lower()
    assert "json_extract" in compiled  # source / project_id JSON 下推
    assert "messages.id" in compiled  # conversation / project 消息子查询
    assert "'failed'" in compiled  # retryable 状态谓词
    assert "'canceled'" in compiled

    request_false = dataclasses.replace(request, retryable=False)
    compiled_false = str(
        apply_item_filters(
            select(Generation).where(Generation.user_id == "user-1"),
            Generation,
            request_false,
        ).compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "error_code IN" in compiled_false


class _CountingSession:
    """真实 sqlite 会话包装:统计 build_task_list 实际执行的语句数。"""

    def __init__(self, inner: AsyncSession) -> None:
        self.inner = inner
        self.calls = 0

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await self.inner.execute(statement, *args, **kwargs)

    def add(self, value: Any) -> None:
        self.inner.add(value)

    async def commit(self) -> None:
        await self.inner.commit()


def _seed_task_database(
    session: AsyncSession,
    *,
    user_id: str = "user-1",
    source: str = "Y",
    count: int = 400,
) -> None:
    """真实 sqlite 内存库:写入 conversations / messages / generations。"""
    session.add(
        Conversation(id="conv-1", user_id=user_id, title="t", default_params={})
    )
    base = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    for i in range(count):
        message_id = f"msg-{i:03d}"
        session.add(
            Message(id=message_id, conversation_id="conv-1", role="user", content={})
        )
        session.add(
            Generation(
                id=f"gen-{i:03d}",
                message_id=message_id,
                user_id=user_id,
                action="generate",
                model="test-model",
                prompt="test prompt",
                size_requested="1024x1024",
                aspect_ratio="1:1",
                input_image_ids=[],
                status=GenerationStatus.SUCCEEDED.value,
                progress_stage="finalized",
                attempt=0,
                idempotency_key=f"key-{i:03d}",
                created_at=base + timedelta(seconds=count - i),
                upstream_request={"source": source},
            )
        )


_TASK_LISTING_TABLES = [
    Conversation.__table__,
    Message.__table__,
    Generation.__table__,
    Completion.__table__,
    Image.__table__,
    ImageVariant.__table__,
]


def _create_sqlite_tables(sync_connection: Any) -> None:
    """SQLite 测试库建表:逐表 DDL 编译并替换 PG 专属 ARRAY[] 默认值。"""
    for table in _TASK_LISTING_TABLES:
        ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
        ddl = ddl.replace("DEFAULT (ARRAY[]::varchar[])", "DEFAULT '[]'")
        sync_connection.execute(text(ddl))


async def _zero_match_output(
    *,
    source: str | None = None,
    conversation_id: str | None = None,
    retryable: bool | None = None,
) -> tuple[Any, int]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_create_sqlite_tables)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _seed_task_database(session, count=400)
            await session.commit()
            counting = _CountingSession(session)
            output = await build_task_list(
                counting,
                tasks._task_listing_runtime(),  # noqa: SLF001
                TaskListRequest(
                    user_id="user-1",
                    status=None,
                    kind="generation",
                    source=source,
                    conversation_id=conversation_id,
                    project_id=None,
                    date_filter=None,
                    cursor=None,
                    error_code=None,
                    retryable=retryable,
                    limit=20,
                ),
            )
            return output, counting.calls
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_tasks_zero_match_source_returns_empty_without_full_scan() -> None:
    """零匹配 source filter 首轮即返回空页,不能深挖分页扫完整张任务表。

    修复前: 用户有 400 行(> query_limit),source="X" 零匹配时深挖循环在
    page 为空(无页尾 key)的判定下会把 400 行按批全拉一遍(全表分页扫描);
    修复后显式 source 下推到 SQL,首轮查询即空,循环立即退出。
    """
    output, calls = await _zero_match_output(source="X")
    assert output.items == []
    assert output.next_cursor is None
    # 修复前此场景约 7 轮深挖(29+ 条语句);下推后仅首轮 2 条语句即退出
    assert calls <= 4


@pytest.mark.asyncio
async def test_list_tasks_zero_match_conversation_returns_empty() -> None:
    """零匹配 conversation_id 同样首轮即空(消息 EXISTS 子查询下推)。"""
    output, calls = await _zero_match_output(conversation_id="conv-missing")
    assert output.items == []
    assert output.next_cursor is None
    assert calls <= 4


@pytest.mark.asyncio
async def test_list_tasks_zero_match_retryable_returns_empty() -> None:
    """retryable=True 零匹配(全部 succeeded)时首轮即空。"""
    output, calls = await _zero_match_output(retryable=True)
    assert output.items == []
    assert output.next_cursor is None
    assert calls <= 4


@pytest.mark.asyncio
@pytest.mark.parametrize("derived_source", ["project", "telegram"])
async def test_list_tasks_zero_match_derived_source_returns_empty_without_full_scan(
    derived_source: str,
) -> None:
    """派生源 source=project/telegram 零匹配(种子库全为显式 source)时,
    派生源预筛进 SQL 后首轮即空,深挖循环立即退出,不再把 400 行任务表
    逐批扫完。"""
    output, calls = await _zero_match_output(source=derived_source)
    assert output.items == []
    assert output.next_cursor is None
    assert calls <= 4


def test_apply_task_item_filters_pushes_derived_source_preconditions_into_sql() -> None:
    """派生源预筛必须进 SQL(不能只靠内存过滤):project 要求 workflow_run_id
    非空,telegram 要求会话 default_params.telegram=true,零匹配派生源 filter
    才能首轮即空。"""
    apply_item_filters = _apply_task_item_filters_pusher()
    request = TaskListRequest(
        user_id="user-1",
        status=None,
        kind="generation",
        source="telegram",
        conversation_id=None,
        project_id=None,
        date_filter=None,
        cursor=None,
        error_code=None,
        retryable=None,
        limit=20,
    )
    compiled = str(
        apply_item_filters(
            select(Generation).where(Generation.user_id == "user-1"),
            Generation,
            request,
        ).compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "json_extract" in compiled
    assert "workflow_run_id" in compiled
    assert "conversations" in compiled  # telegram 会话 default_params 子查询


def _seed_derived_task_database(session: AsyncSession) -> None:
    """派生源正例种子:显式 source / project(请求臂与消息臂)/ telegram / chat。"""
    session.add(
        Conversation(id="conv-1", user_id="user-1", title="t", default_params={})
    )
    session.add(
        Conversation(
            id="conv-tg",
            user_id="user-1",
            title="tg",
            default_params={"telegram": True},
        )
    )
    base = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    specs = [
        ("msg-plain", "conv-1", {}, {"source": "Y"}),  # 显式 source,非派生源
        (
            "msg-proj-req",
            "conv-1",
            {},
            {"workflow_run_id": "proj-1"},
        ),  # project(请求臂)
        (
            "msg-proj-msg",
            "conv-1",
            {"workflow_run_id": "proj-2"},
            {},
        ),  # project(消息臂)
        ("msg-tg", "conv-tg", {}, {}),  # 派生 telegram
        ("msg-chat", "conv-1", {}, {}),  # 派生 chat
        ("msg-explicit-project", "conv-1", {}, {"source": "project"}),  # 显式 project
    ]
    for i, (message_id, conv_id, content, upstream) in enumerate(specs):
        session.add(
            Message(
                id=message_id,
                conversation_id=conv_id,
                role="user",
                content=content,
            )
        )
        session.add(
            Generation(
                id=f"gen-{i:02d}",
                message_id=message_id,
                user_id="user-1",
                action="generate",
                model="test-model",
                prompt="test prompt",
                size_requested="1024x1024",
                aspect_ratio="1:1",
                input_image_ids=[],
                status=GenerationStatus.SUCCEEDED.value,
                progress_stage="finalized",
                attempt=0,
                idempotency_key=f"key-{i:02d}",
                created_at=base + timedelta(seconds=len(specs) - i),
                upstream_request=upstream,
            )
        )


@pytest.mark.asyncio
async def test_list_tasks_derived_source_filters_keep_matches() -> None:
    """派生源预筛不能漏行:project(请求臂/消息臂/显式)/ telegram / chat
    各 filter 应原样返回命中任务。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_create_sqlite_tables)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _seed_derived_task_database(session)
            await session.commit()
            counting = _CountingSession(session)

            async def list_ids(*, source: str) -> list[str]:
                output = await build_task_list(
                    counting,
                    tasks._task_listing_runtime(),  # noqa: SLF001
                    TaskListRequest(
                        user_id="user-1",
                        status=None,
                        kind="generation",
                        source=source,
                        conversation_id=None,
                        project_id=None,
                        date_filter=None,
                        cursor=None,
                        error_code=None,
                        retryable=None,
                        limit=20,
                    ),
                )
                return [item.id for item in output.items]

            assert await list_ids(source="project") == [
                "gen-01",
                "gen-02",
                "gen-05",
            ]
            assert await list_ids(source="telegram") == ["gen-03"]
            assert await list_ids(source="chat") == ["gen-04"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_tasks_conversation_filter_keeps_matches() -> None:
    """conversation_id 下推不能漏行:有匹配的会话应原样返回全部任务。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_create_sqlite_tables)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _seed_task_database(session, count=5)
            await session.commit()
            counting = _CountingSession(session)
            output = await build_task_list(
                counting,
                tasks._task_listing_runtime(),  # noqa: SLF001
                TaskListRequest(
                    user_id="user-1",
                    status=None,
                    kind="generation",
                    source=None,
                    conversation_id="conv-1",
                    project_id=None,
                    date_filter=None,
                    cursor=None,
                    error_code=None,
                    retryable=None,
                    limit=20,
                ),
            )
        assert [item.id for item in output.items] == [
            "gen-000",
            "gen-001",
            "gen-002",
            "gen-003",
            "gen-004",
        ]
        assert output.next_cursor is None
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("kind", "status", "error_code", "expected"),
    [
        (
            "generation",
            GenerationStatus.FAILED.value,
            "upstream_timeout",
            ("retryable", True, False, ("retry",)),
        ),
        (
            "generation",
            GenerationStatus.FAILED.value,
            "INSUFFICIENT_BALANCE",
            ("terminal", False, False, ("open_wallet", "reduce_cost")),
        ),
        (
            "generation",
            GenerationStatus.CANCELED.value,
            "cancelled",
            ("cancelled", True, True, ("retry",)),
        ),
        (
            "generation",
            GenerationStatus.SUCCEEDED.value,
            None,
            ("display_ready", False, False, ()),
        ),
        (
            "completion",
            CompletionStatus.SUCCEEDED.value,
            None,
            ("completed", False, False, ()),
        ),
        (
            "completion",
            CompletionStatus.STREAMING.value,
            None,
            (CompletionStage.THINKING.value, False, False, ()),
        ),
    ],
)
def test_build_task_item_preserves_terminal_and_status_semantics(
    kind: Any,
    status: str,
    error_code: str | None,
    expected: tuple[str, bool, bool, tuple[str, ...]],
) -> None:
    progress_stage = (
        CompletionStage.THINKING.value
        if status == CompletionStatus.STREAMING.value
        else GenerationStage.FINALIZING.value
    )
    task = _task_record(
        id=f"{kind}-1",
        status=status,
        progress_stage=progress_stage,
        error_code=error_code,
        error_message="task failed" if error_code else None,
        attempt=1,
        queue_wait_ms=125,
    )

    item = tasks._build_task_item(kind, task)  # noqa: SLF001

    substage, retryable, cancelled, actions = expected
    assert (item.status, item.progress_stage, item.stage) == (
        status,
        progress_stage,
        progress_stage,
    )
    assert (item.substage, item.retryable, item.cancelled) == (
        substage,
        retryable,
        cancelled,
    )
    assert item.retrying is False
    assert item.waiting_provider is False
    assert tuple(action.id for action in item.recommended_actions) == actions


def test_task_cursor_round_trips_and_rejects_invalid() -> None:
    sort_at = datetime(2026, 5, 19, 10, 15, tzinfo=timezone.utc)
    raw = tasks._encode_task_cursor(sort_at, "generation", "gen-1")  # noqa: SLF001

    assert tasks._decode_task_cursor(raw) == (  # noqa: SLF001
        sort_at,
        "generation",
        "gen-1",
    )
    with pytest.raises(Exception) as exc_info:
        tasks._decode_task_cursor("not-a-cursor")  # noqa: SLF001
    assert getattr(exc_info.value, "status_code", None) == 422


def test_task_cursor_same_timestamp_mode_matches_merged_order() -> None:
    assert (
        tasks._same_timestamp_cursor_mode(  # noqa: SLF001
            model_kind="completion",
            cursor_kind="generation",
        )
        == "all"
    )
    assert (
        tasks._same_timestamp_cursor_mode(  # noqa: SLF001
            model_kind="generation",
            cursor_kind="completion",
        )
        == "none"
    )
    assert (
        tasks._same_timestamp_cursor_mode(  # noqa: SLF001
            model_kind="generation",
            cursor_kind="generation",
        )
        == "same_kind_id"
    )
    assert (
        tasks._same_timestamp_cursor_mode(  # noqa: SLF001
            model_kind="completion",
            cursor_kind="completion",
        )
        == "same_kind_id"
    )


def test_task_recommended_actions_cover_retry_and_terminal_errors() -> None:
    retryable = tasks._task_retryable(  # noqa: SLF001
        "generation",
        GenerationStatus.FAILED.value,
        "upstream_timeout",
    )
    assert retryable is True
    retry_actions = tasks._task_recommended_actions(  # noqa: SLF001
        kind="generation",
        status=GenerationStatus.FAILED.value,
        error_code="upstream_timeout",
        retryable=retryable,
    )
    assert [action.id for action in retry_actions] == ["retry"]

    terminal_retryable = tasks._task_retryable(  # noqa: SLF001
        "generation",
        GenerationStatus.FAILED.value,
        "INSUFFICIENT_BALANCE",
    )
    wallet_actions = tasks._task_recommended_actions(  # noqa: SLF001
        kind="generation",
        status=GenerationStatus.FAILED.value,
        error_code="INSUFFICIENT_BALANCE",
        retryable=terminal_retryable,
    )
    assert terminal_retryable is False
    assert [action.id for action in wallet_actions] == ["open_wallet", "reduce_cost"]


@pytest.mark.asyncio
async def test_retry_generation_requeues_same_row_without_rebuilding_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[dict[str, Any], str]] = []

    async def fake_publish_queued(payload: dict[str, Any], message_id: str) -> None:
        published.append((payload, message_id))

    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_disabled)

    upstream_request = {
        "fast": True,
        "render_quality": "high",
        "output_format": "webp",
        "output_compression": 95,
        "background": "auto",
        "moderation": "low",
    }
    old_time = datetime(2026, 4, 28, tzinfo=timezone.utc)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="assistant-1",
        status=GenerationStatus.FAILED.value,
        progress_stage=GenerationStage.FINALIZING.value,
        attempt=2,
        execution_epoch=4,
        error_code="upstream_timeout",
        error_message="timeout",
        started_at=old_time,
        finished_at=old_time,
        billing_retry_count=0,
        prompt="render a wide hero image",
        size_requested="3840x2160",
        aspect_ratio="16:9",
        upstream_request={
            **upstream_request,
            "trace_id": "trace-old",
            "sidecar_execution": {"job_id": "job-old"},
            "provider_idempotency_key": "provider-key-old",
            "provider_idempotency_stable": True,
            "provider": "provider-old",
            "actual_provider": "provider-old",
            "actual_route": "image_jobs",
            "image_job_url": "https://sidecar.example/job-old",
            "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
            "upstream_dispatch_attempt": 2,
            "upstream_dispatch_execution_epoch": 4,
            "upstream_response_received_at": "2026-07-30T00:00:01+00:00",
            "upstream_response_attempt": 2,
            "upstream_response_execution_epoch": 4,
            "billing_pricing_snapshot": {"tier": "4k", "unit_micro": 500},
            "billing_admission_billable": True,
            "billing_admission_ref_id": "gen-1",
        },
    )
    db = _Db([_Result(gen)])

    out = await tasks.retry_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.QUEUED.value}
    assert gen.status == GenerationStatus.QUEUED.value
    assert gen.progress_stage == GenerationStage.QUEUED.value
    assert gen.attempt == 0
    assert gen.execution_epoch == 5
    assert gen.billing_retry_count == 1
    assert gen.error_code is None
    assert gen.error_message is None
    assert gen.started_at is None
    assert gen.finished_at is None

    assert gen.prompt == "render a wide hero image"
    assert gen.size_requested == "3840x2160"
    assert gen.aspect_ratio == "16:9"
    assert gen.upstream_request == upstream_request

    assert db.committed is True
    assert redis.calls == [("delete", "task:gen-1:cancel")]
    assert len(db.added) == 1
    assert published == [
        (
            {
                "task_id": "gen-1",
                "user_id": "user-1",
                "kind": "generation",
                "execution_epoch": 5,
                "outbox_id": "outbox-1",
            },
            "assistant-1",
        )
    ]


@pytest.mark.asyncio
async def test_retry_generation_holds_new_retry_billing_ref_before_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held: list[dict[str, Any]] = []
    invalidated: list[tuple[str, bool]] = []

    async def fake_publish_queued(_payload: dict[str, Any], _message_id: str) -> None:
        return None

    async def estimate_image_cost_for_tier(
        *_args: Any, **kwargs: Any
    ) -> tuple[int, str]:
        assert kwargs["tier"] == "2k"
        assert kwargs["n"] == 3
        return 75_000, "2k"

    async def hold(_db: _Db, user_id: str, amount_micro: int, **kwargs: Any) -> Any:
        held.append(
            {
                "committed": _db.committed,
                "user_id": user_id,
                "amount_micro": amount_micro,
                **kwargs,
            }
        )
        return SimpleNamespace(balance_after=75_000)

    async def invalidate(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_enabled_true)
    monkeypatch.setattr(tasks, "_billing_allow_negative", _billing_allow_negative_false)
    monkeypatch.setattr(
        tasks.billing_core,
        "estimate_image_cost_for_tier",
        estimate_image_cost_for_tier,
    )
    monkeypatch.setattr(tasks.billing_core, "hold", hold)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate)

    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="assistant-1",
        status=GenerationStatus.CANCELED.value,
        progress_stage=GenerationStage.FINALIZING.value,
        attempt=1,
        execution_epoch=8,
        billing_retry_count=0,
        error_code="cancelled",
        error_message="cancelled by user",
        started_at=None,
        finished_at=datetime.now(timezone.utc),
        size_requested="2048x2048",
        upstream_request={"billing_tier": "2k", "n": 3},
    )
    db = _Db([_Result(gen)])

    out = await tasks.retry_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.QUEUED.value}
    assert gen.billing_retry_count == 1
    assert held == [
        {
            "committed": False,
            "user_id": "user-1",
            "amount_micro": 75_000,
            "ref_type": "generation",
            "ref_id": "gen-1:retry:1",
            "idempotency_key": "hold:gen-1:retry:1",
            "allow_negative": False,
            "meta": {
                "generation_id": "gen-1",
                "reason": "generation retry",
                "retry_count": 1,
                "execution_epoch": 9,
            },
        }
    ]
    assert invalidated == [("user-1", True)]


@pytest.mark.asyncio
async def test_retry_generation_hold_applies_snapshot_rate_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计新-12：重试 hold 必须乘下单时的费率快照，否则少冻结的差额由平台垫付。"""

    held: list[dict[str, Any]] = []

    async def fake_publish_queued(_payload: dict[str, Any], _message_id: str) -> None:
        return None

    async def estimate_image_cost_for_tier(
        *_args: Any, **_kwargs: Any
    ) -> tuple[int, str]:
        return 75_000, "2k"

    async def hold(_db: _Db, _user_id: str, amount_micro: int, **kwargs: Any) -> Any:
        held.append({"amount_micro": amount_micro, "ref_id": kwargs["ref_id"]})
        return SimpleNamespace(balance_after=0)

    async def unexpected_user_multiplier(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("snapshot present: must not re-read the user row")

    monkeypatch.setattr(tasks, "get_redis", lambda: _Redis())
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_enabled_true)
    monkeypatch.setattr(tasks, "_billing_allow_negative", _billing_allow_negative_false)
    monkeypatch.setattr(
        tasks.billing_core,
        "estimate_image_cost_for_tier",
        estimate_image_cost_for_tier,
    )
    monkeypatch.setattr(tasks.billing_core, "hold", hold)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", _noop_invalidate)
    monkeypatch.setattr(
        tasks,
        "user_rate_multiplier_x10000",
        unexpected_user_multiplier,
    )

    gen = _retry_candidate(
        upstream_request={
            "billing_tier": "2k",
            "n": 3,
            "billing_rate_multiplier_x10000": 15_000,
        }
    )

    await tasks.retry_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        _Db([_Result(gen)]),  # type: ignore[arg-type]
    )

    # 75_000 * 15_000 // 10_000 == 112_500，与 worker settle 侧同一口径。
    assert held == [{"amount_micro": 112_500, "ref_id": "gen-1:retry:1"}]


@pytest.mark.asyncio
async def test_retry_generation_hold_falls_back_to_user_rate_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照缺失时回落到用户当前费率，与 worker 的 fallback 顺序一致。"""

    held: list[int] = []

    async def fake_publish_queued(_payload: dict[str, Any], _message_id: str) -> None:
        return None

    async def estimate_image_cost_for_tier(
        *_args: Any, **_kwargs: Any
    ) -> tuple[int, str]:
        return 75_000, "2k"

    async def hold(_db: _Db, _user_id: str, amount_micro: int, **_kwargs: Any) -> Any:
        held.append(amount_micro)
        return SimpleNamespace(balance_after=0)

    async def user_multiplier(_db: Any, user_id: str) -> int:
        assert user_id == "user-1"
        return 12_000

    monkeypatch.setattr(tasks, "get_redis", lambda: _Redis())
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_enabled_true)
    monkeypatch.setattr(tasks, "_billing_allow_negative", _billing_allow_negative_false)
    monkeypatch.setattr(
        tasks.billing_core,
        "estimate_image_cost_for_tier",
        estimate_image_cost_for_tier,
    )
    monkeypatch.setattr(tasks.billing_core, "hold", hold)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", _noop_invalidate)
    monkeypatch.setattr(tasks, "user_rate_multiplier_x10000", user_multiplier)

    gen = _retry_candidate(upstream_request={"billing_tier": "2k", "n": 3})

    await tasks.retry_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        _Db([_Result(gen)]),  # type: ignore[arg-type]
    )

    assert held == [90_000]


@pytest.mark.asyncio
async def test_retry_generation_ignores_cancel_notification_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, Any]] = []

    async def publish_queued(payload: dict[str, Any], _message_id: str) -> None:
        published.append(payload)

    redis = _Redis(fail_delete=True)
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_publish_queued", publish_queued)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_disabled)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="assistant-1",
        status=GenerationStatus.CANCELED.value,
        progress_stage=GenerationStage.FINALIZING.value,
        attempt=1,
        error_code="cancelled",
        error_message="cancelled by user",
        started_at=None,
        finished_at=datetime.now(timezone.utc),
        cancel_requested_at=datetime.now(timezone.utc),
    )
    db = _Db([_Result(gen)])

    out = await tasks.retry_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.QUEUED.value}
    assert gen.status == GenerationStatus.QUEUED.value
    assert gen.cancel_requested_at is None
    assert db.committed is True
    assert redis.calls == [("delete", "task:gen-1:cancel")]
    assert published and published[0]["kind"] == "generation"


@pytest.mark.asyncio
async def test_cancel_running_generation_persists_intent_before_worker_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis(
        {
            "generation:image_queue:task_provider:gen-1": b"provider-a",
            "task:gen-1:lease": b"worker:execution:0:attempt:0",
            "generation:image_queue:reservation:gen-1": b"reservation-0",
        }
    )
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        status=GenerationStatus.RUNNING.value,
        finished_at=None,
        cancel_requested_at=None,
    )
    db = _Db([_Result(gen)])

    out = await tasks.cancel_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": "canceling", "cancel_requested": True}
    assert gen.status == GenerationStatus.RUNNING.value
    assert gen.finished_at is None
    assert gen.cancel_requested_at is not None
    assert db.committed is True
    assert redis.calls == [("set", "task:gen-1:cancel", "1", 3600)]


@pytest.mark.asyncio
async def test_cancel_streaming_completion_returns_canceling_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        status=CompletionStatus.STREAMING.value,
        cancel_requested_at=None,
    )
    db = _Db([_Result(comp)])

    out = await tasks.cancel_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": "canceling", "cancel_requested": True}
    assert comp.status == CompletionStatus.STREAMING.value
    assert comp.cancel_requested_at is not None
    assert db.committed is True
    assert redis.calls == [("set", "task:comp-1:cancel", "1", 3600)]


@pytest.mark.asyncio
async def test_cancel_queued_generation_marks_terminal_and_clears_queue_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, Any]] = []
    released: list[tuple[str, str, str, str]] = []
    invalidated: list[tuple[str, bool]] = []

    async def fake_publish_sse_event(
        _redis: Any,
        *,
        user_id: str,
        channel: str,
        event_name: str,
        data: dict[str, Any],
    ) -> str:
        published.append(
            {
                "user_id": user_id,
                "channel": channel,
                "event_name": event_name,
                "data": data,
            }
        )
        return "sse-1"

    redis = _Redis(
        {
            "generation:image_queue:task_provider:gen-1": b"provider-a",
            "task:gen-1:lease": b"worker:execution:0:attempt:0",
            "generation:image_queue:reservation:gen-1": b"reservation-0",
        }
    )

    async def release_queued_task_hold(
        db: _Db,
        *,
        user_id: str,
        ref_type: str,
        ref_id: str,
        reason: str,
    ) -> bool:
        released.append((user_id, ref_type, ref_id, reason))
        invalidated.append(("release-before-commit", db.committed))
        return True

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "publish_sse_event", fake_publish_sse_event)
    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "_task_wallet_exists", wallet_exists)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate_balance_cache)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status=GenerationStatus.QUEUED.value,
        finished_at=None,
        execution_epoch=0,
        upstream_request={},
    )
    db = _Db([_Result(gen)])

    out = await tasks.cancel_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.CANCELED.value}
    assert gen.status == GenerationStatus.CANCELED.value
    assert gen.finished_at is not None
    assert released == [
        (
            "user-1",
            "generation",
            "gen-1",
            "queued generation cancelled by user",
        )
    ]
    assert invalidated == [("release-before-commit", False), ("user-1", True)]
    assert redis.calls[:3] == [
        ("get", "generation:image_queue:task_provider:gen-1"),
        ("get", "task:gen-1:lease"),
        ("get", "generation:image_queue:reservation:gen-1"),
    ]
    assert redis.calls[3][0] == "eval"
    assert redis.calls[3][2] == 5
    assert redis.calls[3][3:8] == (
        "generation:image_queue:provider_active:provider-a",
        "generation:image_queue:active",
        "generation:image_queue:task_provider:gen-1",
        "task:gen-1:lease",
        "generation:image_queue:reservation:gen-1",
    )
    assert published == [
        {
            "user_id": "user-1",
            "channel": tasks.task_channel("gen-1"),
            "event_name": "generation.canceled",
            "data": {
                "generation_id": "gen-1",
                "message_id": "msg-1",
                "stage": GenerationStage.FINALIZING.value,
                "substage": "cancelled",
                "cancelled": True,
                "code": "cancelled",
                "message": "cancelled by user",
                "retriable": True,
                "recommended_actions": [
                    {"id": "retry", "label": "重新开始", "kind": "retry"}
                ],
            },
        }
    ]


@pytest.mark.asyncio
async def test_cancel_queued_generation_with_current_dispatch_receipt_defers_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []
    redis = _Redis()

    async def release_queued_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status=GenerationStatus.QUEUED.value,
        finished_at=None,
        cancel_requested_at=None,
        execution_epoch=4,
        upstream_request={
            "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
            "upstream_dispatch_attempt": 2,
            "upstream_dispatch_execution_epoch": 4,
        },
    )
    db = _Db([_Result(gen)])

    out = await tasks.cancel_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": "canceling", "cancel_requested": True}
    assert gen.status == GenerationStatus.QUEUED.value
    assert gen.finished_at is None
    assert gen.cancel_requested_at is not None
    assert released == []
    assert db.committed is True
    assert redis.calls == [("set", "task:gen-1:cancel", "1", 3600)]


@pytest.mark.asyncio
async def test_cancel_queued_generation_skips_wallet_release_for_byok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[dict[str, Any]] = []
    released: list[str] = []

    async def fake_publish_sse_event(*_args: Any, **kwargs: Any) -> str:
        published.append(kwargs)
        return "sse-1"

    async def release_queued_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return False

    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "publish_sse_event", fake_publish_sse_event)
    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "_task_wallet_exists", wallet_exists)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status=GenerationStatus.QUEUED.value,
        finished_at=None,
    )
    db = _Db([_Result(gen)], active_account_mode="byok")

    out = await tasks.cancel_generation(
        "gen-1",
        SimpleNamespace(id="user-1", account_mode="byok"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.CANCELED.value}
    assert released == []
    assert db.committed is True
    assert published


@pytest.mark.asyncio
async def test_cancel_queued_generation_releases_wallet_hold_for_wallet_user_when_wallet_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []

    async def fake_publish_sse_event(*_args: Any, **_kwargs: Any) -> str:
        return "sse-1"

    async def release_queued_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def invalidate_balance_cache(_user_id: str) -> None:
        return None

    monkeypatch.setattr(tasks, "get_redis", lambda: _Redis())
    monkeypatch.setattr(tasks, "publish_sse_event", fake_publish_sse_event)
    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "_task_wallet_exists", wallet_exists)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate_balance_cache)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status=GenerationStatus.QUEUED.value,
        finished_at=None,
    )
    db = _Db([_Result(gen)])

    out = await tasks.cancel_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.CANCELED.value}
    assert released == ["called"]


@pytest.mark.asyncio
async def test_cancel_queued_completion_releases_wallet_hold_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    released: list[tuple[str, str, str, str]] = []
    invalidated: list[tuple[str, bool]] = []
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        finished_at=None,
        upstream_request={"billing_retry_count": 1},
    )
    db = _Db([_Result(comp)])

    async def release_queued_task_hold(
        db: _Db,
        *,
        user_id: str,
        ref_type: str,
        ref_id: str,
        reason: str,
    ) -> bool:
        released.append((user_id, ref_type, ref_id, reason))
        invalidated.append(("release-before-commit", db.committed))
        return True

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate_balance_cache)
    monkeypatch.setattr(tasks, "_task_wallet_exists", wallet_exists)

    out = await tasks.cancel_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": CompletionStatus.CANCELED.value}
    assert comp.status == CompletionStatus.CANCELED.value
    assert comp.progress_stage == CompletionStage.FINALIZING.value
    assert comp.finished_at is not None
    assert released == [
        (
            "user-1",
            "completion",
            "comp-1:retry:1",
            "queued completion cancelled by user",
        )
    ]
    assert invalidated == [("release-before-commit", False), ("user-1", True)]


@pytest.mark.asyncio
async def test_cancel_queued_completion_with_current_usage_defers_actual_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []
    redis = _Redis()

    async def release_queued_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        finished_at=None,
        cancel_requested_at=None,
        execution_epoch=7,
        tokens_in=120,
        upstream_request={
            "billing_retry_count": 2,
            "completion_usage_execution_epoch": 7,
        },
    )
    db = _Db([_Result(comp)])

    out = await tasks.cancel_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": "canceling", "cancel_requested": True}
    assert comp.status == CompletionStatus.QUEUED.value
    assert comp.finished_at is None
    assert comp.cancel_requested_at is not None
    assert released == []
    assert db.committed is True
    assert redis.calls == [("set", "task:comp-1:cancel", "1", 3600)]


@pytest.mark.asyncio
async def test_cancel_queued_completion_skips_wallet_release_for_byok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []
    monkeypatch.setattr(tasks, "get_redis", lambda: _Redis())
    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        finished_at=None,
    )
    db = _Db([_Result(comp)], active_account_mode="byok")

    async def release_queued_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def invalidate_balance_cache(_user_id: str) -> None:
        return None

    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "_task_wallet_exists", wallet_exists)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate_balance_cache)

    out = await tasks.cancel_completion(
        "comp-1",
        SimpleNamespace(id="user-1", account_mode="byok"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": CompletionStatus.CANCELED.value}
    assert released == []
    assert db.committed is True


@pytest.mark.asyncio
async def test_cancel_queued_completion_releases_wallet_hold_for_wallet_user_when_wallet_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []
    monkeypatch.setattr(tasks, "get_redis", lambda: _Redis())
    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        finished_at=None,
    )
    db = _Db([_Result(comp)])

    async def release_queued_task_hold(*_args: Any, **_kwargs: Any) -> bool:
        released.append("called")
        return True

    async def wallet_exists(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(tasks, "_release_queued_task_hold", release_queued_task_hold)
    monkeypatch.setattr(tasks, "_task_wallet_exists", wallet_exists)

    out = await tasks.cancel_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": CompletionStatus.CANCELED.value}
    assert released == ["called"]
    assert db.committed is True


@pytest.mark.asyncio
async def test_retry_completion_records_new_billing_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[dict[str, Any], str]] = []

    async def fake_publish_queued(payload: dict[str, Any], message_id: str) -> None:
        published.append((payload, message_id))

    redis = _Redis()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)

    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        message_id="assistant-1",
        status=CompletionStatus.FAILED.value,
        progress_stage=CompletionStage.FINALIZING.value,
        attempt=2,
        execution_epoch=3,
        error_code="upstream_timeout",
        error_message="timeout",
        started_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        text="old partial output",
        tokens_in=101,
        tokens_out=202,
        cache_read_tokens=11,
        cache_creation_tokens=12,
        cache_creation_5m_tokens=13,
        cache_creation_1h_tokens=14,
        reasoning_tokens=15,
        image_output_tokens=16,
        upstream_request={
            "web_search": True,
            "tool_image_reserved_micro": 900,
            "completion_usage_execution_epoch": 3,
            "completion_usage_attempt_epoch": 2,
            "trace_id": "trace-old",
            "provider": "provider-old",
            "context": {"compressed": True},
            "memory": {"used_memory_ids": ["memory-old"]},
            "billing_pricing_snapshot": {"model": "old-model"},
            "billing_admission_billable": True,
            "billing_admission_ref_id": "comp-1",
        },
    )
    db = _Db([_Result(comp)])

    out = await tasks.retry_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": CompletionStatus.QUEUED.value}
    assert comp.status == CompletionStatus.QUEUED.value
    assert comp.progress_stage == CompletionStage.QUEUED.value
    assert comp.attempt == 0
    assert comp.execution_epoch == 4
    assert comp.error_code is None
    assert comp.error_message is None
    assert comp.started_at is None
    assert comp.finished_at is None
    assert comp.text == ""
    assert comp.tokens_in == 0
    assert comp.tokens_out == 0
    assert comp.cache_read_tokens == 0
    assert comp.cache_creation_tokens == 0
    assert comp.cache_creation_5m_tokens == 0
    assert comp.cache_creation_1h_tokens == 0
    assert comp.reasoning_tokens == 0
    assert comp.image_output_tokens == 0
    assert comp.upstream_request == {
        "web_search": True,
        "billing_retry_count": 1,
    }
    assert db.committed is True
    assert redis.calls == [("delete", "task:comp-1:cancel")]
    assert published == [
        (
            {
                "task_id": "comp-1",
                "user_id": "user-1",
                "kind": "completion",
                "execution_epoch": 4,
                "outbox_id": "outbox-1",
            },
            "assistant-1",
        )
    ]


@pytest.mark.asyncio
async def test_retry_completion_holds_new_retry_billing_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[dict[str, Any], str]] = []
    hold_calls: list[dict[str, Any]] = []
    invalidated: list[tuple[str, bool]] = []

    async def fake_publish_queued(payload: dict[str, Any], message_id: str) -> None:
        published.append((payload, message_id))

    async def hold(_db: Any, user_id: str, amount_micro: int, **kwargs: Any) -> Any:
        hold_calls.append({"user_id": user_id, "amount_micro": amount_micro, **kwargs})
        return SimpleNamespace(balance_after=80_000, hold_after=20_000)

    async def invalidate_balance_cache(user_id: str) -> None:
        invalidated.append((user_id, db.committed))

    comp = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        message_id="assistant-1",
        status=CompletionStatus.CANCELED.value,
        progress_stage=CompletionStage.FINALIZING.value,
        attempt=1,
        execution_epoch=6,
        error_code="cancelled",
        error_message="cancelled",
        started_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        upstream_request={},
    )
    previous_hold = SimpleNamespace(amount_micro=-20_000)
    db = _Db([_Result(comp), _Result(previous_hold)])

    monkeypatch.setattr(tasks, "get_redis", lambda: _Redis())
    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_enabled_true)
    monkeypatch.setattr(tasks, "_billing_allow_negative", _billing_allow_negative_false)
    monkeypatch.setattr(tasks, "invalidate_balance_cache", invalidate_balance_cache)
    monkeypatch.setattr(tasks.billing_core, "hold", hold)

    out = await tasks.retry_completion(
        "comp-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": CompletionStatus.QUEUED.value}
    assert comp.upstream_request["billing_retry_count"] == 1
    assert hold_calls == [
        {
            "user_id": "user-1",
            "amount_micro": 20_000,
            "ref_type": "completion",
            "ref_id": "comp-1:retry:1",
            "idempotency_key": "hold:comp-1:retry:1",
            "allow_negative": False,
            "meta": {
                "completion_id": "comp-1",
                "reason": "completion retry",
                "billing_retry_count": 1,
                "previous_billing_retry_count": 0,
                "execution_epoch": 7,
            },
        }
    ]
    assert invalidated == [("user-1", True)]
    assert published


@pytest.mark.asyncio
async def test_concurrent_generation_retry_advances_execution_epoch_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = asyncio.Lock()
    gen = _retry_candidate(execution_epoch=11)

    class LockedDb(_Db):
        async def execute(self, statement: Any) -> _Result:
            async with lock:
                self.statements.append(statement)
                return _Result(gen)

    async def noop_publish(_payload: dict[str, Any], _message_id: str) -> None:
        return None

    monkeypatch.setattr(tasks, "get_redis", lambda: _Redis())
    monkeypatch.setattr(tasks, "_publish_queued", noop_publish)
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_disabled)

    first, second = await asyncio.gather(
        tasks.retry_generation(
            "gen-1",
            _user(),  # type: ignore[arg-type]
            LockedDb([]),  # type: ignore[arg-type]
        ),
        tasks.retry_generation(
            "gen-1",
            _user(),  # type: ignore[arg-type]
            LockedDb([]),  # type: ignore[arg-type]
        ),
        return_exceptions=True,
    )

    results = (first, second)
    assert (
        sum(result == {"status": GenerationStatus.QUEUED.value} for result in results)
        == 1
    )
    conflicts = [
        result for result in results if isinstance(result, tasks.HTTPException)
    ]
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409
    assert gen.execution_epoch == 12
