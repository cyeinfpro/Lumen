from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app import sse_publish
from app.tasks import completion
from lumen_core.models import OutboxEvent


@pytest.mark.asyncio
async def test_completion_lease_renewer_sets_event_and_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenRedis:
        async def eval(self, *_args, **_kwargs) -> None:
            raise RuntimeError("redis down")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(completion.asyncio, "sleep", no_sleep)
    lease_lost = completion.asyncio.Event()

    await completion._lease_renewer(BrokenRedis(), "comp-1", "worker-1", lease_lost)

    assert lease_lost.is_set()


@pytest.mark.asyncio
async def test_completion_lease_renewer_cancellation_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel() lease renewer task 不应 raise 出 _lease_renewer 之外。

    主路径在 finally 里 cancel renewer + suppress(CancelledError) 等它完成；
    如果 _lease_renewer 处理 cancel 时漏抛非 CancelledError 异常或吞掉
    CancelledError，外层 await 会失去取消语义。这里直接 cancel 一个 sleep
    阻塞中的 renewer，确认 await 拿回 CancelledError、redis.expire 没被
    调用、lease_lost 没被误 set。
    """
    expire_calls: list[tuple] = []

    class HealthyRedis:
        async def eval(self, *args, **_kwargs) -> int:
            expire_calls.append(args)
            return 1

    lease_lost = completion.asyncio.Event()

    task = asyncio.create_task(
        completion._lease_renewer(HealthyRedis(), "comp-cancel", "worker-1", lease_lost)
    )
    # 让 task 进入 sleep 状态再 cancel
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled() or task.done()
    assert not lease_lost.is_set()
    assert expire_calls == []  # cancel 在第一次 sleep 内即触发，没机会调 expire


@pytest.mark.asyncio
async def test_completion_lease_renewer_clears_fail_streak_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连续失败后再连续 2 轮成功，fail streak 应归零，避免后续失败累加触阈值。

    实现里 ``consecutive_failures = 0`` 紧跟在 expire 成功路径，覆盖：
    fail → fail → success → success → fail → fail（streak 应该是 2，不是 4），
    第三次 fail 才触发 >=3 的 lease_lost。这里 sequence 故意只到第 6 步就
    再来 2 次 fail（第 7、8 步）让 streak 触阈值退出循环；如果实现没在 success
    时 reset，第 5 步那次 fail 就已经触发 lease_lost（4 次累计），断言失败。
    """
    sequence: list[bool] = [
        False,  # 1: fail (streak=1)
        False,  # 2: fail (streak=2)
        True,  # 3: success → reset to 0
        True,  # 4: success → still 0
        False,  # 5: fail (streak=1, NOT 3 if reset works)
        False,  # 6: fail (streak=2)
        False,  # 7: fail (streak=3 → set lease_lost & return)
    ]
    call_idx = {"i": 0}

    class FlakyRedis:
        async def eval(self, *_args, **_kwargs) -> int:
            i = call_idx["i"]
            call_idx["i"] += 1
            # 越界视作 fail（兜底），但 streak=3 会先 return 出来
            ok = sequence[i] if i < len(sequence) else False
            if not ok:
                raise RuntimeError("redis blip")
            return 1

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(completion.asyncio, "sleep", no_sleep)
    lease_lost = completion.asyncio.Event()

    await completion._lease_renewer(FlakyRedis(), "comp-streak", "worker-1", lease_lost)

    # 关键：必须走完第 5、6、7 步才退（streak 在第 7 步=3）。如果 reset 没生效
    # ，第 5 步就已经是 streak=3 退出，call_idx 只会到 5。
    assert call_idx["i"] == 7
    # 第 7 步触阈值后 lease_lost 才 set
    assert lease_lost.is_set()


@pytest.mark.asyncio
async def test_completion_lease_renewer_exits_on_owner_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        async def eval(self, *_args, **_kwargs) -> int:
            return 0

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(completion.asyncio, "sleep", no_sleep)
    lease_lost = completion.asyncio.Event()

    await completion._lease_renewer(Redis(), "comp-owner", "worker-old", lease_lost)

    assert lease_lost.is_set()


@pytest.mark.asyncio
async def test_completion_release_lease_uses_owner_cas() -> None:
    class Redis:
        def __init__(self) -> None:
            self.eval_args: tuple[object, ...] | None = None

        async def eval(self, *args: object) -> int:
            self.eval_args = args
            return 0

    redis = Redis()

    await completion._release_lease(redis, "comp-1", "worker-1")

    assert redis.eval_args is not None
    assert redis.eval_args[1:4] == (1, "task:comp-1:lease", "worker-1")


def test_completion_lease_lost_keeps_base_exception_semantics() -> None:
    assert issubclass(completion._LeaseLost, BaseException)
    assert not issubclass(completion._LeaseLost, Exception)


@pytest.mark.asyncio
async def test_run_completion_lease_conflict_never_opens_db_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_tokens: list[str] = []

    class BusyRedis:
        async def set(self, _key: str, token: str, **kwargs: Any) -> bool:
            lease_tokens.append(token)
            assert kwargs["nx"] is True
            return False

    def fail_session() -> None:
        raise AssertionError("duplicate worker must not inspect or mutate completion")

    monkeypatch.setattr(completion, "SessionLocal", fail_session)

    for _ in range(2):
        await completion.run_completion(
            {"redis": BusyRedis(), "worker_id": "worker-1"},
            "comp-busy",
        )

    assert len(set(lease_tokens)) == 2
    assert all(token.startswith("worker-1:") for token in lease_tokens)


@pytest.mark.asyncio
async def test_run_completion_setup_lease_loss_does_not_mutate_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        status=completion.CompletionStatus.QUEUED.value,
        attempt=2,
        text="valid worker text",
    )

    class Result:
        def scalar_one_or_none(self) -> Any:
            return row

    class Session:
        async def __aenter__(self) -> "Session":
            await asyncio.sleep(0)
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, _statement: Any) -> Result:
            return Result()

        async def get(self, *_args: Any) -> Any:
            raise AssertionError("lost lease must abort before loading related rows")

    class Redis:
        async def set(self, _key: str, _token: str, **_kwargs: Any) -> bool:
            return True

    async def no_lock(_session: Any, _task_id: str) -> None:
        return None

    async def lose_lease(
        _redis: Any,
        _task_id: str,
        _token: str,
        lease_lost: asyncio.Event,
    ) -> None:
        lease_lost.set()

    released: list[tuple[str, str]] = []

    async def release(_redis: Any, task_id: str, token: str) -> None:
        released.append((task_id, token))

    monkeypatch.setattr(completion, "SessionLocal", lambda: Session())
    monkeypatch.setattr(completion, "_acquire_completion_xact_lock", no_lock)
    monkeypatch.setattr(completion, "_lease_renewer", lose_lease)
    monkeypatch.setattr(completion, "_release_lease", release)

    await completion.run_completion(
        {"redis": Redis(), "worker_id": "worker-1"},
        "comp-lost",
    )

    assert row.status == completion.CompletionStatus.QUEUED.value
    assert row.attempt == 2
    assert row.text == "valid worker text"
    assert len(released) == 1
    assert released[0][0] == "comp-lost"
    assert released[0][1].startswith("worker-1:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_name", "event_data"),
    [
        (completion.EV_COMP_STARTED, {"completion_id": "comp-1"}),
        (completion.EV_COMP_DELTA, {"completion_id": "comp-1", "text_delta": "x"}),
        (
            completion.EV_COMP_PROGRESS,
            {"completion_id": "comp-1", "stage": "tool_call"},
        ),
    ],
)
async def test_completion_publish_failure_stages_stable_sse_redrive(
    monkeypatch: pytest.MonkeyPatch,
    event_name: str,
    event_data: dict[str, Any],
) -> None:
    class Session:
        def __init__(self) -> None:
            self.rows: list[OutboxEvent] = []
            self.commits = 0

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def add(self, row: OutboxEvent) -> None:
            self.rows.append(row)

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    attempted: list[dict[str, Any]] = []

    async def fail_publish(
        _redis: Any,
        _user_id: str,
        _channel: str,
        _event_name: str,
        payload: dict[str, Any],
    ) -> None:
        attempted.append(payload)
        raise sse_publish.SSEPublishRetryableError(
            stream_key="sse:user-1:task:comp-1",
            event_id=str(payload["event_id"]),
            diagnostic_dlq_persisted=True,
        )

    monkeypatch.setattr(completion, "SessionLocal", lambda: session)
    monkeypatch.setattr(completion, "_publish_sse_event", fail_publish)

    await completion.publish_event(
        object(),
        "user-1",
        "task:comp-1",
        event_name,
        event_data,
    )

    assert session.commits == 1
    assert len(session.rows) == 1
    row = session.rows[0]
    assert row.kind == "sse"
    assert row.published_at is None
    assert row.payload["event_name"] == event_name
    assert row.payload["data"]["event_id"] == attempted[0]["event_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_name", "event_data"),
    [
        (completion.EV_COMP_SUCCEEDED, {"text": "done"}),
        (completion.EV_COMP_FAILED, {"code": "upstream_error"}),
        (completion.EV_COMP_FAILED, {"code": "cancelled"}),
    ],
)
async def test_terminal_completion_publish_failure_keeps_outbox_for_redrive(
    event_name: str,
    event_data: dict[str, Any],
) -> None:
    class Session:
        def __init__(self) -> None:
            self.rows: list[OutboxEvent] = []

        def add(self, row: OutboxEvent) -> None:
            self.rows.append(row)

    class BrokenRedis:
        async def get(self, _key: str) -> None:
            raise RuntimeError("redis unavailable")

    session = Session()
    delivery = completion._stage_completion_event(
        session,
        "user-1",
        "task:comp-1",
        event_name,
        event_data,
    )
    event_id = delivery[2]["data"]["event_id"]

    await completion._deliver_completion_event(BrokenRedis(), delivery)

    assert len(session.rows) == 1
    assert session.rows[0].published_at is None
    assert session.rows[0].payload["data"]["event_id"] == event_id


@pytest.mark.asyncio
async def test_memory_extract_outbox_is_staged_before_success_commit() -> None:
    user = SimpleNamespace(
        id="user-1",
        memory_disabled=False,
        memory_paused=False,
    )
    conversation = SimpleNamespace(
        id="conversation-1",
        user_id="user-1",
        memory_disabled=False,
    )

    class Session:
        def __init__(self) -> None:
            self.rows: list[OutboxEvent] = []
            self.committed = False

        async def get(self, model: Any, _row_id: str) -> Any:
            if model is completion.User:
                return user
            if model is completion.Conversation:
                return conversation
            return None

        def add(self, row: OutboxEvent) -> None:
            self.rows.append(row)

        async def commit(self) -> None:
            assert any(row.kind == "memory_extract" for row in self.rows)
            self.committed = True

    session = Session()
    delivery = (
        await completion._completion_tool_images._stage_completion_memory_extract(
            session,
            feature_enabled=True,
            user_id="user-1",
            conversation_id="conversation-1",
            source_message_id="source-1",
            assistant_message_id="assistant-1",
            hooks=completion._COMPLETION_EVENT_HOOKS,
        )
    )
    await session.commit()

    assert delivery is not None
    assert session.committed is True
    row = session.rows[0]
    assert row.published_at is None
    assert row.payload["task_id"] == "assistant-1"
    assert row.payload["event_id"] == "memory-extract:source-1:assistant-1"
    assert row.payload["source_user_message_id"] == "source-1"
    assert row.payload["assistant_message_id"] == "assistant-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feature_enabled", "source_message_id", "user_disabled", "conversation_disabled"),
    [
        (False, "source-1", False, False),
        (True, None, False, False),
        (True, "source-1", True, False),
        (True, "source-1", False, True),
    ],
)
async def test_memory_extract_outbox_requires_enabled_complete_context(
    feature_enabled: bool,
    source_message_id: str | None,
    user_disabled: bool,
    conversation_disabled: bool,
) -> None:
    class Session:
        async def get(self, model: Any, _row_id: str) -> Any:
            if model is completion.User:
                return SimpleNamespace(
                    id="user-1",
                    memory_disabled=user_disabled,
                    memory_paused=False,
                )
            if model is completion.Conversation:
                return SimpleNamespace(
                    id="conversation-1",
                    user_id="user-1",
                    memory_disabled=conversation_disabled,
                )
            return None

        def add(self, _row: OutboxEvent) -> None:
            raise AssertionError("disabled or incomplete memory context must not stage")

    delivery = (
        await completion._completion_tool_images._stage_completion_memory_extract(
            Session(),
            feature_enabled=feature_enabled,
            user_id="user-1",
            conversation_id="conversation-1",
            source_message_id=source_message_id,
            assistant_message_id="assistant-1",
            hooks=completion._COMPLETION_EVENT_HOOKS,
        )
    )

    assert delivery is None


@pytest.mark.asyncio
async def test_completion_cleanup_survives_base_exception_from_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_requested = asyncio.Event()
    release_calls: list[tuple[str, str]] = []

    async def release(_redis: Any, task_id: str, token: str) -> None:
        release_calls.append((task_id, token))

    async def normal_background_task() -> None:
        await asyncio.Event().wait()

    async def watcher_with_base_exception() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise completion._LeaseLost("watcher lost lease") from exc

    class Span:
        exited = False

        def __exit__(self, *_args: Any) -> None:
            self.exited = True

    monkeypatch.setattr(completion, "_release_lease", release)
    renewer = asyncio.create_task(normal_background_task())
    watcher = asyncio.create_task(watcher_with_base_exception())
    await asyncio.sleep(0)
    span = Span()

    await completion._cleanup_completion_runtime(
        redis=object(),
        task_id="comp-cleanup",
        lease_token="worker-1:token-1",
        lease_acquired=True,
        renewer=renewer,
        cancel_stop_requested=stop_requested,
        cancel_watcher=watcher,
        stream_span_cm=span,
        task_start=asyncio.get_event_loop().time(),
        task_outcome="lease_lost",
    )

    assert stop_requested.is_set()
    assert renewer.done()
    assert watcher.done()
    assert release_calls == [("comp-cleanup", "worker-1:token-1")]
    assert span.exited is True
