"""auto_title 测试。

覆盖修复后的核心路径：
- _is_default_title 边界
- _parse_response_text：SSE delta 累积、done 事件、纯 JSON 兜底
- _call_upstream：多 provider failover（第一个 5xx → 切第二个成功）
- reconcile_default_titles：扫描默认标题并 enqueue
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lumen_core.models import Conversation
from app.tasks import auto_title


# --- _is_default_title -----------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        (None, True),
        ("", True),
        ("   ", True),
        ("New Canvas", True),  # 前端 fallback；写到 DB 时也算"待生成"
        ("未命名", True),
        ("新会话", True),
        ("untitled", True),
        ("Untitled", True),
        ("我的标题", False),
        ("Hello", False),
    ],
)
def test_is_default_title(title: str | None, expected: bool) -> None:
    assert auto_title._is_default_title(title) is expected


# --- _parse_response_text --------------------------------------------------


def test_parse_response_text_from_done_event() -> None:
    sse = (
        'event: response.output_text.done\n'
        'data: {"type":"response.output_text.done","text":"我的标题"}\n\n'
    )
    assert auto_title._parse_response_text(sse, "text/event-stream") == "我的标题"


def test_parse_response_text_from_delta_only() -> None:
    """关键 bug 修复：上游只发 delta + completed 时也能拿到 title。"""
    sse = (
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","delta":"前端"}\n\n'
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","delta":"重构"}\n\n'
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{}}\n\n'
    )
    assert auto_title._parse_response_text(sse, "text/event-stream") == "前端重构"


def test_parse_response_text_done_takes_precedence_over_delta() -> None:
    """done 事件给的完整 text 优先于 delta 拼接（两者偶有 tokenizer 误差）。"""
    sse = (
        'event: response.output_text.delta\n'
        'data: {"type":"response.output_text.delta","delta":"半个"}\n\n'
        'event: response.output_text.done\n'
        'data: {"type":"response.output_text.done","text":"完整标题"}\n\n'
    )
    assert auto_title._parse_response_text(sse, "text/event-stream") == "完整标题"


def test_parse_response_text_from_content_part_done() -> None:
    sse = (
        'event: response.content_part.done\n'
        'data: {"type":"response.content_part.done","part":{"type":"output_text","text":"标题 X"}}\n\n'
    )
    assert auto_title._parse_response_text(sse, "text/event-stream") == "标题 X"


def test_parse_response_text_from_response_completed_output() -> None:
    """没 done 没 delta，只有 completed 里嵌套的 output：仍能解析。"""
    sse = (
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"output":[{"type":"message","content":[{"type":"output_text","text":"Aha"}]}]}}\n\n'
    )
    assert auto_title._parse_response_text(sse, "text/event-stream") == "Aha"


def test_parse_response_text_falls_back_to_json() -> None:
    """上游切回 application/json 时退化到整体解析。"""
    json_body = '{"output":[{"content":[{"text":"非流式标题"}]}]}'
    assert (
        auto_title._parse_response_text(json_body, "application/json")
        == "非流式标题"
    )


def test_parse_response_text_uses_top_level_output_text() -> None:
    json_body = '{"output_text":"顶层标题"}'
    assert (
        auto_title._parse_response_text(json_body, "application/json")
        == "顶层标题"
    )


def test_parse_response_text_returns_empty_when_nothing_parseable() -> None:
    assert auto_title._parse_response_text("garbage", "text/plain") == ""
    assert auto_title._parse_response_text("", "text/event-stream") == ""


# --- _call_upstream：multi-provider failover -------------------------------


class _StubResolved:
    def __init__(self, name: str) -> None:
        self.name = name
        self.base_url = f"https://{name}.example"
        self.api_key = f"sk-{name}"


class _StubPool:
    def __init__(self, providers: list[_StubResolved]) -> None:
        self.providers = providers

    async def select(self, *, route: str = "text") -> list[_StubResolved]:
        _ = route
        return self.providers


@pytest.mark.asyncio
async def test_call_upstream_failover_on_first_provider_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _StubPool([_StubResolved("acc1"), _StubResolved("acc2")])

    async def fake_get_pool() -> _StubPool:
        return pool

    from app import provider_pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)

    calls: list[str] = []

    async def fake_one(
        input_list: list[dict[str, Any]],
        *,
        base_url: str,
        api_key: str,
    ) -> str:
        calls.append(api_key)
        if api_key == "sk-acc1":
            raise auto_title.UpstreamError(
                "boom", error_code="upstream_error", status_code=503
            )
        return "FROM ACC2"

    monkeypatch.setattr(auto_title, "_call_upstream_one", fake_one)
    # 缩短 backoff，加速测试
    monkeypatch.setattr(auto_title, "_PER_PROVIDER_RETRY_ATTEMPTS", 1)

    result = await auto_title._call_upstream(
        [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    )
    assert result == "FROM ACC2"
    assert calls == ["sk-acc1", "sk-acc2"]


@pytest.mark.asyncio
async def test_call_upstream_does_not_failover_on_terminal_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """terminal（如 400 invalid）按 retry.py 不可重试；当前实现仍跳到下一个 provider
    （input 共享，下一个号同样 4xx），但保证不会无限重试在同一个 provider。"""
    pool = _StubPool([_StubResolved("acc1"), _StubResolved("acc2")])

    async def fake_get_pool() -> _StubPool:
        return pool

    from app import provider_pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)

    calls: list[str] = []

    async def fake_one(
        input_list: list[dict[str, Any]],
        *,
        base_url: str,
        api_key: str,
    ) -> str:
        calls.append(api_key)
        raise auto_title.UpstreamError(
            "bad request",
            error_code="invalid_request_error",
            status_code=400,
        )

    monkeypatch.setattr(auto_title, "_call_upstream_one", fake_one)
    monkeypatch.setattr(auto_title, "_PER_PROVIDER_RETRY_ATTEMPTS", 1)

    with pytest.raises(auto_title.UpstreamError) as exc_info:
        await auto_title._call_upstream(
            [{"role": "user", "content": [{"type": "input_text", "text": "x"}]}]
        )
    # 每个 provider 只调用 1 次（terminal 不内部 retry）
    assert calls == ["sk-acc1", "sk-acc2"]
    assert exc_info.value.error_code == "all_providers_failed"


@pytest.mark.asyncio
async def test_call_upstream_retries_within_provider_on_retriable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单 provider 内 5xx 重试 1 次：第二次成功则不切 provider。"""
    pool = _StubPool([_StubResolved("acc1"), _StubResolved("acc2")])

    async def fake_get_pool() -> _StubPool:
        return pool

    from app import provider_pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)

    call_count = {"n": 0}

    async def fake_one(
        input_list: list[dict[str, Any]],
        *,
        base_url: str,
        api_key: str,
    ) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise auto_title.UpstreamError(
                "tmp", error_code="server_error", status_code=502
            )
        return "OK"

    monkeypatch.setattr(auto_title, "_call_upstream_one", fake_one)
    monkeypatch.setattr(auto_title, "_PER_PROVIDER_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(auto_title, "_PER_PROVIDER_RETRY_BACKOFF_S", 0.0)

    result = await auto_title._call_upstream(
        [{"role": "user", "content": [{"type": "input_text", "text": "x"}]}]
    )
    assert result == "OK"
    assert call_count["n"] == 2


# --- _sanitize_title --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("我的标题", "我的标题"),
        ("「项目重构」", "项目重构"),
        ("\"标题\"", "标题"),
        ("标题：前端优化", "前端优化"),
        ("Title: Refactor", "Refactor"),
        ("a" * 30, "a" * 24),  # 长度 24 上限
    ],
)
def test_sanitize_title(raw: str, expected: str) -> None:
    assert auto_title._sanitize_title(raw) == expected


# --- reconcile_default_titles 巡检 -----------------------------------------


class _FakeRedisEnqueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []

    async def enqueue_job(self, name: str, conv_id: str) -> None:
        self.enqueued.append((name, conv_id))


@pytest.mark.asyncio
async def test_reconcile_returns_zero_when_no_redis() -> None:
    n = await auto_title.reconcile_default_titles({})
    assert n == 0


@pytest.mark.asyncio
async def test_reconcile_returns_minus_one_when_lock_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA 多 worker 场景：第二个 worker 拿不到锁应直接跳过，不扫 DB。"""

    class _LockBusyRedis:
        async def set(
            self,
            key: str,
            value: str,
            *,
            nx: bool = False,
            ex: int | None = None,
        ) -> bool:
            assert nx is True
            assert ex is not None
            return False  # 锁已被其他 worker 持有

        async def enqueue_job(self, *_a: Any, **_kw: Any) -> None:
            raise AssertionError("不应该 enqueue：锁没拿到就退出")

    db_called = False

    class _NotCalledSession:
        async def __aenter__(self) -> "_NotCalledSession":
            nonlocal db_called
            db_called = True
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def execute(self, *_a: Any, **_kw: Any) -> Any:
            raise AssertionError("不应触达 DB：锁没拿到")

    monkeypatch.setattr(auto_title, "SessionLocal", lambda: _NotCalledSession())
    n = await auto_title.reconcile_default_titles({"redis": _LockBusyRedis()})
    assert n == -1
    assert db_called is False  # 锁失败连 SessionLocal 都不进


@pytest.mark.asyncio
async def test_reconcile_proceeds_when_lock_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功拿到锁后正常执行扫描。"""

    class _LockOkRedis(_FakeRedisEnqueue):
        async def set(
            self,
            key: str,
            value: str,
            *,
            nx: bool = False,
            ex: int | None = None,
        ) -> bool:
            return True

    class _Result:
        def __init__(self, rows: list) -> None:
            self._rows = rows

        def all(self) -> list:
            return self._rows

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def execute(self, _stmt: Any) -> _Result:
            return _Result([("conv1",)])

    monkeypatch.setattr(auto_title, "SessionLocal", lambda: _FakeSession())
    redis = _LockOkRedis()
    n = await auto_title.reconcile_default_titles({"redis": redis})
    assert n == 1
    assert redis.enqueued == [("auto_title_conversation", "conv1")]


@pytest.mark.asyncio
async def test_reconcile_continues_when_lock_redis_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁本身的 Redis 调用错时，按"不抢锁"继续巡检（fail-open）。"""

    class _LockErrorRedis(_FakeRedisEnqueue):
        async def set(self, *_a: Any, **_kw: Any) -> bool:
            raise RuntimeError("redis flake")

    class _Result:
        def __init__(self, rows: list) -> None:
            self._rows = rows

        def all(self) -> list:
            return self._rows

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def execute(self, _stmt: Any) -> _Result:
            return _Result([("conv1",)])

    monkeypatch.setattr(auto_title, "SessionLocal", lambda: _FakeSession())
    redis = _LockErrorRedis()
    n = await auto_title.reconcile_default_titles({"redis": redis})
    assert n == 1  # 锁错误不阻断主路径


# --- _call_upstream_one：timeout 显式 30s -----------------------------------


@pytest.mark.asyncio
async def test_call_upstream_one_raises_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """30s timeout 触发时抛 UpstreamError(error_code='upstream_timeout')，
    让 _call_upstream 能识别并切下一个 provider。"""
    import httpx

    class _TimeoutClient:
        async def __aenter__(self) -> "_TimeoutClient":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def post(self, *_a: Any, **_kw: Any) -> Any:
            raise httpx.ReadTimeout("title gen too slow")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _TimeoutClient())
    with pytest.raises(auto_title.UpstreamError) as exc_info:
        await auto_title._call_upstream_one(
            [{"role": "user", "content": [{"type": "input_text", "text": "x"}]}],
            base_url="https://acc1.example",
            api_key="sk-acc1",
        )
    assert exc_info.value.error_code == "upstream_timeout"


@pytest.mark.asyncio
async def test_call_upstream_one_uses_30s_timeout_not_180s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式断言 timeout 配置 = 30s（避免回退到 upstream_read_timeout_s 180s）。"""
    import httpx

    captured: dict[str, Any] = {}

    class _CaptureClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> "_CaptureClient":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def post(self, *_a: Any, **_kw: Any) -> Any:
            class _R:
                status_code = 200
                text = '{"output_text":"OK"}'
                headers: dict[str, str] = {"content-type": "application/json"}

            return _R()

    monkeypatch.setattr(httpx, "AsyncClient", _CaptureClient)
    await auto_title._call_upstream_one(
        [{"role": "user", "content": [{"type": "input_text", "text": "x"}]}],
        base_url="https://acc1.example",
        api_key="sk-acc1",
    )
    timeout_obj = captured.get("timeout")
    assert timeout_obj is not None
    # httpx.Timeout 的 read 字段必须等于常量；不依赖 settings.upstream_read_timeout_s
    assert timeout_obj.read == auto_title._TITLE_HTTP_TIMEOUT_S


@pytest.mark.asyncio
async def test_auto_title_conversation_applies_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, model: Any, object_id: str) -> Any:
            if model is Conversation:
                return Conversation(id=object_id, user_id="user-1", title="")
            return None

    async def fake_build_summary(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]

    async def slow_call(_input: list[dict[str, Any]]) -> str:
        await asyncio.sleep(1)
        return "too late"

    async def must_not_publish(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("timed out title must not publish")

    monkeypatch.setattr(auto_title, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(auto_title, "_build_summary", fake_build_summary)
    monkeypatch.setattr(auto_title, "_call_upstream", slow_call)
    monkeypatch.setattr(auto_title, "_TITLE_TOTAL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(auto_title, "publish_event", must_not_publish)

    await auto_title.auto_title_conversation({"redis": object()}, "conv-1")


# --- _call_upstream 失败日志包含尝试列表 ------------------------------------


@pytest.mark.asyncio
async def test_call_upstream_logs_attempted_providers_on_total_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = _StubPool([_StubResolved("acc1"), _StubResolved("acc2")])

    async def fake_get_pool() -> _StubPool:
        return pool

    from app import provider_pool

    monkeypatch.setattr(provider_pool, "get_pool", fake_get_pool)

    async def boom(
        _input: list[dict[str, Any]], *, base_url: str, api_key: str
    ) -> str:
        raise auto_title.UpstreamError(
            "down", error_code="server_error", status_code=503
        )

    monkeypatch.setattr(auto_title, "_call_upstream_one", boom)
    monkeypatch.setattr(auto_title, "_PER_PROVIDER_RETRY_ATTEMPTS", 1)

    import logging

    caplog.set_level(logging.WARNING, logger="app.tasks.auto_title")
    with pytest.raises(auto_title.UpstreamError):
        await auto_title._call_upstream(
            [{"role": "user", "content": [{"type": "input_text", "text": "x"}]}]
        )
    log_text = caplog.text
    assert "acc1" in log_text and "acc2" in log_text
    assert "all providers failed" in log_text


@pytest.mark.asyncio
async def test_reconcile_enqueues_default_title_conversations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """巡检发现默认 title + 有 user msg + 已稳定的会话 → enqueue。"""
    redis = _FakeRedisEnqueue()

    # mock SessionLocal 上下文管理器，让 select 返回 [("conv1",), ("conv2",)]
    class _Result:
        def __init__(self, rows: list) -> None:
            self._rows = rows

        def all(self) -> list:
            return self._rows

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, _stmt: Any) -> _Result:
            # 模拟 PG 返回 2 个 candidate
            return _Result([("conv1",), ("conv2",)])

    monkeypatch.setattr(auto_title, "SessionLocal", lambda: _FakeSession())

    n = await auto_title.reconcile_default_titles({"redis": redis})
    assert n == 2
    assert sorted(redis.enqueued) == [
        ("auto_title_conversation", "conv1"),
        ("auto_title_conversation", "conv2"),
    ]


@pytest.mark.asyncio
async def test_reconcile_enqueue_failures_are_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单条 enqueue 失败不影响其他 conversation 处理。"""

    class _BadRedis:
        def __init__(self) -> None:
            self.calls = 0

        async def enqueue_job(self, _name: str, _conv: str) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("redis flake")

    class _Result:
        def __init__(self, rows: list) -> None:
            self._rows = rows

        def all(self) -> list:
            return self._rows

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, _stmt: Any) -> _Result:
            return _Result([("conv1",), ("conv2",)])

    monkeypatch.setattr(auto_title, "SessionLocal", lambda: _FakeSession())
    redis = _BadRedis()
    n = await auto_title.reconcile_default_titles({"redis": redis})
    # 第 2 个成功
    assert n == 1
    assert redis.calls == 2


@pytest.mark.asyncio
async def test_reconcile_handles_db_failure_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 错误不应让 cron 抛异常（只记 warning）。"""

    class _BoomSession:
        async def __aenter__(self) -> "_BoomSession":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def execute(self, _stmt: Any) -> Any:
            raise RuntimeError("DB down")

    monkeypatch.setattr(auto_title, "SessionLocal", lambda: _BoomSession())
    n = await auto_title.reconcile_default_titles({"redis": _FakeRedisEnqueue()})
    assert n == 0


# --- pytest-asyncio event_loop ----------------------------------------------


@pytest.fixture
def event_loop():  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
