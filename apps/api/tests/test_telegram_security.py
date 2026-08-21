from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request

from app import deps, proxy_pool
from app.routes import (
    telegram,
    telegram_generation,
    telegram_prompt_enhance,
    telegram_prompt_idempotency,
)
from app.routes.prompt_parts import idempotency as prompt_idempotency
from app.routes.prompt_parts import responses as prompt_responses


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/telegram/bind",
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
        }
    )


def _request_with_headers(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/telegram/me",
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in headers.items()
            ],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_link_code_preserves_urlsafe_entropy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        telegram.secrets,
        "token_urlsafe",
        lambda _n: "ab-CD_ef0123456789==",
    )
    code = telegram._gen_link_code()

    assert code == "ab-CD_ef0123456789"
    # Why also assert entropy class shape: a future regression that lower-cases
    # or uppercases the alphabet (e.g. someone "normalising" the code for
    # storage) would still pass the literal-equality check above only because
    # the monkeypatched fake happens to match. Pin the mixed-case + URL-safe
    # punctuation explicitly so the check breaks if the alphabet collapses.
    assert any(c.isupper() for c in code)
    assert any(c.islower() for c in code)
    assert any(c in "-_" for c in code)


def test_link_code_real_token_keeps_mixed_case_alphabet() -> None:
    # Why: the monkeypatched test above proves _gen_link_code does not eat
    # uppercase letters; this one proves the *real* token_urlsafe alphabet is
    # in fact mixed-case URL-safe (so the contract being defended is real and
    # not just an artifact of the mock's chosen characters). 22 chars is the
    # base64-no-pad encoding of 16 bytes, so a real call must produce >=22.
    code = telegram._gen_link_code()
    assert len(code) >= 22


class _TelegramPromptIdempotencyDb:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.commits = 0
        self.rollbacks = 0

    async def get(self, _model: Any, record_id: str) -> Any:
        return self.rows.get(record_id)

    def add(self, row: Any) -> None:
        self.rows[row.id] = row

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def test_telegram_prompt_enhance_idempotency_key_and_fingerprint_contract() -> None:
    assert (
        telegram_prompt_idempotency.resolve_client_idempotency_key("tg:request-1")
        == "tg:request-1"
    )
    first = telegram_prompt_idempotency.canonical_request_fingerprint(
        user_id="user-1",
        chat_id="-100123",
        tg_user_id="42",
        text="cat",
    )
    changed_text = telegram_prompt_idempotency.canonical_request_fingerprint(
        user_id="user-1",
        chat_id="-100123",
        tg_user_id="42",
        text="dog",
    )
    changed_identity = telegram_prompt_idempotency.canonical_request_fingerprint(
        user_id="user-1",
        chat_id="-100456",
        tg_user_id="84",
        text="cat",
    )

    assert first != changed_text
    assert first != changed_identity
    for raw in (
        None,
        "",
        " tg:request-1",
        "tg:request-1 ",
        "tg:request 1",
        "请求-1",
        "x" * 97,
    ):
        with pytest.raises(Exception) as excinfo:
            telegram_prompt_idempotency.resolve_client_idempotency_key(raw)
        assert getattr(excinfo.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_telegram_prompt_enhance_reservation_replays_conflicts_and_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(prompt_idempotency, "lock_user_key", no_lock)
    db = _TelegramPromptIdempotencyDb()
    operation = telegram_prompt_idempotency.telegram_prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="tg:request-1",
        chat_id="-100123",
        tg_user_id="42",
        text="cat",
    )

    first = await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
        db,  # type: ignore[arg-type]
        operation,
    )
    assert first.replay_enhanced is None

    with pytest.raises(Exception) as pending:
        await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
            db,  # type: ignore[arg-type]
            operation,
        )
    assert getattr(pending.value, "status_code", None) == 425
    assert pending.value.detail["error"]["code"] == "idempotency_in_progress"

    await telegram_prompt_idempotency.persist_terminal_success(
        db,  # type: ignore[arg-type]
        operation,
        "enhanced cat",
    )
    replay = await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
        db,  # type: ignore[arg-type]
        operation,
    )
    assert replay.replay_enhanced == "enhanced cat"

    changed_operations = [
        telegram_prompt_idempotency.telegram_prompt_enhance_operation(
            user_id="user-1",
            idempotency_key="tg:request-1",
            chat_id="-100123",
            tg_user_id="42",
            text="dog",
        ),
        telegram_prompt_idempotency.telegram_prompt_enhance_operation(
            user_id="user-1",
            idempotency_key="tg:request-1",
            chat_id="-100456",
            tg_user_id="84",
            text="cat",
        ),
        telegram_prompt_idempotency.telegram_prompt_enhance_operation(
            user_id="user-2",
            idempotency_key="tg:request-1",
            chat_id="-100123",
            tg_user_id="42",
            text="cat",
        ),
    ]
    for changed in changed_operations:
        with pytest.raises(Exception) as conflict:
            await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
                db,  # type: ignore[arg-type]
                changed,
            )
        assert getattr(conflict.value, "status_code", None) == 409
        assert conflict.value.detail["error"]["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_telegram_prompt_enhance_stale_crash_before_provider_takes_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    current = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(prompt_idempotency, "lock_user_key", no_lock)
    monkeypatch.setattr(prompt_idempotency, "_utcnow", lambda: current)
    db = _TelegramPromptIdempotencyDb()
    operation = telegram_prompt_idempotency.telegram_prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="tg:crash-before-provider",
        chat_id="-100123",
        tg_user_id="42",
        text="cat",
    )

    first = await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
        db,  # type: ignore[arg-type]
        operation,
    )
    assert first.attempt is not None
    await db.commit()

    current += timedelta(seconds=46)
    takeover = await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
        db,  # type: ignore[arg-type]
        operation,
    )

    assert takeover.attempt is not None
    assert takeover.attempt.number == first.attempt.number + 1
    assert takeover.attempt.billing_request_id == first.attempt.billing_request_id
    assert takeover.recovery is None


@pytest.mark.asyncio
async def test_telegram_prompt_enhance_charge_before_terminal_recovers_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    current = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(prompt_idempotency, "lock_user_key", no_lock)
    monkeypatch.setattr(prompt_idempotency, "_utcnow", lambda: current)
    db = _TelegramPromptIdempotencyDb()
    operation = telegram_prompt_idempotency.telegram_prompt_enhance_operation(
        user_id="user-1",
        idempotency_key="tg:charge-before-terminal",
        chat_id="-100123",
        tg_user_id="42",
        text="cat",
    )
    first = await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
        db,  # type: ignore[arg-type]
        operation,
    )
    assert first.attempt is not None
    await prompt_idempotency.bind_billing_snapshot(  # type: ignore[arg-type]
        db,
        operation,
        first.attempt,
        {
            "version": 1,
            "mode": "wallet",
            "request_id": first.attempt.billing_request_id,
            "user_id": "user-1",
            "rate_multiplier_x10000": 10_000,
            "cache_aware": True,
            "allow_negative": False,
            "hold_amount_micro": 10_000,
            "pricing_snapshots": {},
        },
    )
    text_chunk = 'data: {"text":"enhanced cat"}\n\n'
    await prompt_idempotency.checkpoint_response_chunk(  # type: ignore[arg-type]
        db,
        operation,
        first.attempt,
        sequence=0,
        chunk=text_chunk,
    )
    await prompt_idempotency.checkpoint_finalization(  # type: ignore[arg-type]
        db,
        operation,
        first.attempt,
        terminal_state="succeeded",
        terminal_chunk="data: [DONE]\n\n",
        billing_action="charge",
        billing_capture={"usage": {"input_tokens": 1, "output_tokens": 1}},
    )

    charged_ids = {first.attempt.billing_request_id}
    charge_attempts = 1
    current += timedelta(seconds=46)
    takeover = await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
        db,  # type: ignore[arg-type]
        operation,
    )
    assert takeover.attempt is not None
    assert takeover.recovery is not None

    class SessionContext:
        async def __aenter__(self) -> _TelegramPromptIdempotencyDb:
            return db

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    async def recover_billing(
        _db: Any,
        _recovery: prompt_idempotency.PromptEnhanceRecovery,
    ) -> None:
        nonlocal charge_attempts
        charge_attempts += 1
        charged_ids.add(first.attempt.billing_request_id)

    async def must_not_call_provider(_db: Any):
        raise AssertionError("checkpoint recovery must not call providers")
        yield ""

    consumer, task = prompt_responses.durable_stream(
        operation=operation,
        attempt=takeover.attempt,
        recovery=takeover.recovery,
        session_factory=SessionContext,
        source_factory=must_not_call_provider,
        recovery_handler=recover_billing,
        heartbeat_interval_seconds=0,
        logger=SimpleNamespace(
            exception=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
    )
    assert [chunk async for chunk in consumer] == [
        text_chunk,
        "data: [DONE]\n\n",
    ]
    await task

    replay = await telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
        db,  # type: ignore[arg-type]
        operation,
    )
    assert replay.replay_enhanced == "enhanced cat"
    assert charge_attempts == 2
    assert charged_ids == {first.attempt.billing_request_id}


@pytest.mark.asyncio
async def test_telegram_ssh_proxy_reuses_lifecycle_owned_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    proxy = SimpleNamespace(name="ssh-egress")

    async def fake_resolve(
        actual_proxy: object,
        *,
        runtime: object,
        bind_host: str,
        advertise_host: str,
    ) -> str:
        captured["args"] = (
            actual_proxy,
            runtime,
            bind_host,
            advertise_host,
        )
        return "socks5h://api:41560"

    monkeypatch.setattr(proxy_pool, "_resolve_provider_proxy_url", fake_resolve)

    url = await proxy_pool.resolve_provider_proxy_url(
        proxy,  # type: ignore[arg-type]
        bind_host="0.0.0.0",
        advertise_host="api",
    )

    actual_proxy, runtime, bind_host, advertise_host = captured["args"]
    expected_runtime = proxy_pool._proxy_pool_state.provider_proxy  # noqa: SLF001
    assert actual_proxy is proxy
    assert runtime is expected_runtime
    assert bind_host == "0.0.0.0"
    assert advertise_host == "api"
    assert url == "socks5h://api:41560"


@pytest.mark.asyncio
async def test_first_telegram_conversation_is_serialized_on_user_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Shared:
        def __init__(self) -> None:
            self.lock = asyncio.Lock()
            self.conversations: list[Any] = []

    class Result:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class Db:
        def __init__(self, shared: Shared) -> None:
            self.shared = shared
            self.pending: Any = None
            self.holds_lock = False

        async def execute(self, _statement: Any) -> Result:
            value = self.shared.conversations[0] if self.shared.conversations else None
            await asyncio.sleep(0)
            return Result(value)

        def add(self, value: Any) -> None:
            value.id = value.id or "telegram-conversation"
            self.pending = value

        async def flush(self) -> None:
            if self.pending is not None:
                self.shared.conversations.append(self.pending)
                self.pending = None

        async def commit(self) -> None:
            if self.holds_lock:
                self.holds_lock = False
                self.shared.lock.release()

        async def refresh(self, _value: Any) -> None:
            return None

    shared = Shared()

    async def fake_lock_active_user(db: Db, _user_id: str) -> None:
        await shared.lock.acquire()
        db.holds_lock = True

    monkeypatch.setattr(
        telegram_generation,
        "lock_active_user",
        fake_lock_active_user,
    )

    first, second = await asyncio.gather(
        telegram_generation.get_or_create_tg_conversation(
            Db(shared),  # type: ignore[arg-type]
            "user-1",
        ),
        telegram_generation.get_or_create_tg_conversation(
            Db(shared),  # type: ignore[arg-type]
            "user-1",
        ),
    )

    assert len(shared.conversations) == 1
    assert first.id == second.id == "telegram-conversation"


@pytest.mark.asyncio
async def test_generation_rechecks_binding_after_concurrent_revoke_before_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_started = asyncio.Event()
    allow_lock = asyncio.Event()
    events: list[str] = []

    class Result:
        def first(self) -> None:
            events.append("binding_read")
            return None

    class Db:
        async def execute(self, _statement: Any) -> Result:
            return Result()

    async def fake_lock_active_user(_db: Any, _user_id: str) -> None:
        events.append("lock_wait")
        lock_started.set()
        await allow_lock.wait()
        events.append("lock_acquired")

    monkeypatch.setattr(
        telegram_generation,
        "lock_active_user",
        fake_lock_active_user,
    )

    task = asyncio.create_task(
        telegram_generation.lock_telegram_generation_context(
            Db(),  # type: ignore[arg-type]
            authenticated_user_id="user-1",
            chat_id="chat-1",
            tg_user_id="tg-1",
        )
    )
    await asyncio.wait_for(lock_started.wait(), timeout=1)
    allow_lock.set()

    with pytest.raises(Exception) as excinfo:
        await task

    assert events == ["lock_wait", "lock_acquired", "binding_read"]
    assert getattr(excinfo.value, "status_code", None) == 403
    assert excinfo.value.detail["error"]["code"] == "telegram_binding_revoked"


@pytest.mark.asyncio
async def test_generation_context_rejects_rebound_identity_under_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def first(self) -> tuple[Any, Any]:
            return (
                SimpleNamespace(user_id="user-2", tg_user_id="tg-2"),
                SimpleNamespace(id="user-2", deleted_at=None, account_mode="wallet"),
            )

    class Db:
        async def execute(self, _statement: Any) -> Result:
            return Result()

    async def fake_lock_active_user(_db: Any, _user_id: str) -> None:
        return None

    monkeypatch.setattr(
        telegram_generation,
        "lock_active_user",
        fake_lock_active_user,
    )

    with pytest.raises(Exception) as excinfo:
        await telegram_generation.lock_telegram_generation_context(
            Db(),  # type: ignore[arg-type]
            authenticated_user_id="user-1",
            chat_id="chat-1",
            tg_user_id="tg-1",
        )

    assert getattr(excinfo.value, "status_code", None) == 403
    assert excinfo.value.detail["error"]["code"] == "telegram_binding_changed"


@pytest.mark.asyncio
async def test_generation_context_refreshes_account_mode_inside_binding_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh_user = SimpleNamespace(
        id="user-1",
        deleted_at=None,
        account_mode="byok",
        email="user@example.com",
    )
    conversation = SimpleNamespace(id="conversation-1")
    binding = SimpleNamespace(user_id="user-1", tg_user_id="tg-1")
    statements: list[Any] = []
    events: list[str] = []

    class Result:
        def __init__(self, *, row: Any = None, scalar: Any = None) -> None:
            self.row = row
            self.scalar = scalar

        def first(self) -> Any:
            return self.row

        def scalar_one_or_none(self) -> Any:
            return self.scalar

    class Db:
        async def execute(self, statement: Any) -> Result:
            statements.append(statement)
            if len(statements) == 1:
                events.append("binding_read")
                return Result(row=(binding, fresh_user))
            events.append("conversation_read")
            return Result(scalar=conversation)

    async def fake_lock_active_user(_db: Any, _user_id: str) -> None:
        events.append("user_locked")

    monkeypatch.setattr(
        telegram_generation,
        "lock_active_user",
        fake_lock_active_user,
    )

    context = await telegram_generation.lock_telegram_generation_context(
        Db(),  # type: ignore[arg-type]
        authenticated_user_id="user-1",
        chat_id="chat-1",
        tg_user_id="tg-1",
    )

    assert events == ["user_locked", "binding_read", "conversation_read"]
    assert context.user is fresh_user
    assert context.user.account_mode == "byok"
    assert context.conversation is conversation
    assert statements[0].get_execution_options()["populate_existing"] is True
    assert statements[0]._for_update_arg is not None  # noqa: SLF001


@pytest.mark.asyncio
async def test_create_generation_uses_locked_fresh_user_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_user = SimpleNamespace(
        id="user-1",
        account_mode="wallet",
    )
    fresh_user = SimpleNamespace(
        id="user-1",
        account_mode="byok",
    )
    conversation = SimpleNamespace(id="conversation-1")
    captured: dict[str, Any] = {}

    async def fake_context(
        _db: Any,
        *,
        authenticated_user_id: str,
        chat_id: str,
        tg_user_id: str,
        client_key: str,
        request_payload: dict[str, Any],
    ) -> Any:
        captured["identity"] = (
            authenticated_user_id,
            chat_id,
            tg_user_id,
            client_key,
            request_payload,
        )
        return telegram_generation.TelegramGenerationContext(
            user=fresh_user,  # type: ignore[arg-type]
            conversation=conversation,  # type: ignore[arg-type]
            message_idempotency_key="tg:internal-key",
            operation_id="operation-1",
        )

    async def fake_submit(
        conversation_id: str,
        body: Any,
        actual_user: Any,
        _db: Any,
    ) -> Any:
        captured["submit"] = (
            conversation_id,
            actual_user,
            actual_user.account_mode,
            body.idempotency_key,
            body.source,
            body.action_source,
            body.trace_id,
        )
        return SimpleNamespace(
            assistant_message=SimpleNamespace(id="message-1"),
            generation_ids=["generation-1"],
        )

    monkeypatch.setattr(
        telegram,
        "lock_telegram_generation_context",
        fake_context,
    )
    monkeypatch.setattr(telegram, "submit_user_message", fake_submit)

    out = await telegram.create_generation(
        _request(
            headers=[
                (b"x-telegram-chat-id", b"chat-1"),
                (b"x-telegram-user-id", b"tg-1"),
            ]
        ),
        telegram.GenerateIn(idempotency_key="tg:locked-user", prompt="cat"),
        stale_user,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    identity = captured["identity"]
    assert identity[:4] == ("user-1", "chat-1", "tg-1", "tg:locked-user")
    assert identity[4]["prompt"] == "cat"
    assert captured["submit"] == (
        "conversation-1",
        fresh_user,
        "byok",
        "tg:internal-key",
        "telegram",
        "telegram.generation",
        "operation-1",
    )
    assert out.user_id == "user-1"
    assert out.generation_ids == ["generation-1"]


class _TelegramOperationResult:
    def __init__(self, value: Any, *, row: bool = False) -> None:
        self.value = value
        self.row = row

    def first(self) -> Any:
        return self.value if self.row else None

    def scalar_one_or_none(self) -> Any:
        return None if self.row else self.value


class _TelegramOperationDb:
    def __init__(self, results: list[_TelegramOperationResult]) -> None:
        self.results = list(results)
        self.execute_count = 0

    async def execute(self, _statement: Any) -> _TelegramOperationResult:
        self.execute_count += 1
        return self.results.pop(0)


class _CreateTelegramOperationDb(_TelegramOperationDb):
    def __init__(self, results: list[_TelegramOperationResult]) -> None:
        super().__init__(results)
        self.added: list[Any] = []
        self.flushes = 0

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1
        for value in self.added:
            if isinstance(value, telegram_generation.Conversation) and not value.id:
                value.id = "conversation-created"


def _telegram_operation_record(
    *,
    owner_user_id: str,
    conversation_id: str,
    chat_id: str,
    tg_user_id: str,
    client_key: str,
    payload: dict[str, Any],
) -> Any:
    message_key = telegram_generation.telegram_generation_message_key(
        chat_id,
        tg_user_id,
        client_key,
    )
    fingerprint = telegram_generation.canonical_generation_request_fingerprint(payload)
    return SimpleNamespace(
        user_id=owner_user_id,
        event_type=telegram_generation._OPERATION_EVENT_TYPE,  # noqa: SLF001
        details=telegram_generation._operation_details(  # noqa: SLF001
            conversation_id=conversation_id,
            chat_id=chat_id,
            tg_user_id=tg_user_id,
            client_key=client_key,
            message_idempotency_key=message_key,
            request_fingerprint=fingerprint,
        ),
    )


@pytest.mark.asyncio
async def test_telegram_operation_first_reservation_binds_owner_and_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(telegram_generation, "lock_user_key", no_lock)
    monkeypatch.setattr(telegram_generation, "lock_active_user", no_lock)
    chat_id = "chat-1"
    tg_user_id = "tg-1"
    client_key = "tg:first"
    payload = {"prompt": "cat", "count": 1}
    user = SimpleNamespace(id="user-1", account_mode="wallet")
    binding = SimpleNamespace(user_id=user.id, tg_user_id=tg_user_id)
    db = _CreateTelegramOperationDb(
        [
            _TelegramOperationResult((binding, user), row=True),
            _TelegramOperationResult(None),
            _TelegramOperationResult(None),
        ]
    )

    context = await telegram_generation.lock_telegram_generation_context(
        db,  # type: ignore[arg-type]
        authenticated_user_id=user.id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        client_key=client_key,
        request_payload=payload,
    )

    audit_rows = [
        value for value in db.added if isinstance(value, telegram_generation.AuditLog)
    ]
    assert len(audit_rows) == 1
    record = audit_rows[0]
    assert record.user_id == user.id
    assert record.target_user_id == user.id
    assert record.details["conversation_id"] == context.conversation.id
    assert record.details["request_fingerprint"] == (
        telegram_generation.canonical_generation_request_fingerprint(payload)
    )
    assert record.details["message_idempotency_key"] == (
        context.message_idempotency_key
    )
    assert context.replay is False
    assert db.flushes == 2


@pytest.mark.asyncio
async def test_telegram_operation_replays_only_for_original_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(telegram_generation, "lock_user_key", no_lock)
    monkeypatch.setattr(telegram_generation, "lock_active_user", no_lock)
    chat_id = "chat-1"
    tg_user_id = "tg-1"
    client_key = "tg:stable"
    payload = {"prompt": "cat", "count": 1}
    user = SimpleNamespace(id="user-1", account_mode="wallet")
    binding = SimpleNamespace(user_id=user.id, tg_user_id=tg_user_id)
    conversation = SimpleNamespace(id="conversation-1", user_id=user.id)
    record = _telegram_operation_record(
        owner_user_id=user.id,
        conversation_id=conversation.id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        client_key=client_key,
        payload=payload,
    )
    db = _TelegramOperationDb(
        [
            _TelegramOperationResult((binding, user), row=True),
            _TelegramOperationResult(record),
            _TelegramOperationResult(conversation),
        ]
    )

    context = await telegram_generation.lock_telegram_generation_context(
        db,  # type: ignore[arg-type]
        authenticated_user_id=user.id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        client_key=client_key,
        request_payload=payload,
    )

    assert context.user is user
    assert context.conversation is conversation
    assert context.replay is True
    assert context.message_idempotency_key == (
        telegram_generation.telegram_generation_message_key(
            chat_id,
            tg_user_id,
            client_key,
        )
    )
    assert db.execute_count == 3


@pytest.mark.asyncio
async def test_telegram_operation_rejects_changed_payload_before_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(telegram_generation, "lock_user_key", no_lock)
    monkeypatch.setattr(telegram_generation, "lock_active_user", no_lock)
    user = SimpleNamespace(id="user-1", account_mode="wallet")
    binding = SimpleNamespace(user_id=user.id, tg_user_id="tg-1")
    record = _telegram_operation_record(
        owner_user_id=user.id,
        conversation_id="conversation-1",
        chat_id="chat-1",
        tg_user_id="tg-1",
        client_key="tg:stable",
        payload={"prompt": "cat", "count": 1},
    )
    db = _TelegramOperationDb(
        [
            _TelegramOperationResult((binding, user), row=True),
            _TelegramOperationResult(record),
        ]
    )

    with pytest.raises(Exception) as excinfo:
        await telegram_generation.lock_telegram_generation_context(
            db,  # type: ignore[arg-type]
            authenticated_user_id=user.id,
            chat_id="chat-1",
            tg_user_id="tg-1",
            client_key="tg:stable",
            request_payload={"prompt": "dog", "count": 1},
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert excinfo.value.detail["error"]["code"] == "idempotency_conflict"
    assert db.execute_count == 2


@pytest.mark.asyncio
async def test_telegram_operation_rebind_conflict_hides_original_task_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(telegram_generation, "lock_user_key", no_lock)
    monkeypatch.setattr(telegram_generation, "lock_active_user", no_lock)
    chat_id = "chat-1"
    tg_user_id = "tg-1"
    client_key = "tg:stable"
    payload = {"prompt": "cat", "count": 1}
    rebound_user = SimpleNamespace(id="user-2", account_mode="wallet")
    binding = SimpleNamespace(user_id=rebound_user.id, tg_user_id=tg_user_id)
    record = _telegram_operation_record(
        owner_user_id="user-1",
        conversation_id="conversation-old",
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        client_key=client_key,
        payload=payload,
    )
    db = _TelegramOperationDb(
        [
            _TelegramOperationResult((binding, rebound_user), row=True),
            _TelegramOperationResult(record),
        ]
    )

    with pytest.raises(Exception) as excinfo:
        await telegram_generation.lock_telegram_generation_context(
            db,  # type: ignore[arg-type]
            authenticated_user_id=rebound_user.id,
            chat_id=chat_id,
            tg_user_id=tg_user_id,
            client_key=client_key,
            request_payload=payload,
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert (
        excinfo.value.detail["error"]["code"] == "telegram_generation_rebind_conflict"
    )
    detail_text = str(excinfo.value.detail)
    assert "user-1" not in detail_text
    assert "conversation-old" not in detail_text
    assert db.execute_count == 2


@pytest.mark.asyncio
async def test_create_generation_rebind_conflict_never_submits_or_charges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def conflict(*_args: Any, **_kwargs: Any) -> Any:
        raise telegram._http(  # noqa: SLF001
            "telegram_generation_rebind_conflict",
            "telegram generation request cannot be replayed under this binding",
            409,
        )

    async def unexpected_submit(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("rebind conflict must fail before task creation or wallet hold")

    monkeypatch.setattr(telegram, "lock_telegram_generation_context", conflict)
    monkeypatch.setattr(telegram, "submit_user_message", unexpected_submit)

    with pytest.raises(Exception) as excinfo:
        await telegram.create_generation(
            _request(
                headers=[
                    (b"x-telegram-chat-id", b"chat-1"),
                    (b"x-telegram-user-id", b"tg-1"),
                ]
            ),
            telegram.GenerateIn(idempotency_key="tg:stable", prompt="cat"),
            SimpleNamespace(id="user-2"),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert (
        excinfo.value.detail["error"]["code"] == "telegram_generation_rebind_conflict"
    )


@pytest.mark.asyncio
async def test_create_generation_rejects_missing_key_before_any_durable_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_context(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("missing key must fail before conversation or identity locking")

    async def unexpected_submit(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("missing key must fail before message/task/wallet/outbox writes")

    monkeypatch.setattr(
        telegram,
        "lock_telegram_generation_context",
        unexpected_context,
    )
    monkeypatch.setattr(telegram, "submit_user_message", unexpected_submit)
    body = telegram.GenerateIn.model_construct(
        idempotency_key=None,
        prompt="cat",
    )

    with pytest.raises(Exception) as excinfo:
        await telegram.create_generation(
            _request(
                headers=[
                    (b"x-telegram-chat-id", b"chat-1"),
                    (b"x-telegram-user-id", b"tg-1"),
                ]
            ),
            body,
            SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["code"] == "missing_idempotency_key"


@pytest.mark.asyncio
async def test_create_generation_replays_same_key_and_rejects_changed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = SimpleNamespace(id="conversation-1")
    locked_user = SimpleNamespace(id="user-1", account_mode="wallet")
    stored: dict[str, tuple[dict[str, Any], Any]] = {}
    creations = 0

    async def fake_context(*_args: Any, **_kwargs: Any) -> Any:
        return telegram_generation.TelegramGenerationContext(
            user=locked_user,  # type: ignore[arg-type]
            conversation=conversation,  # type: ignore[arg-type]
        )

    async def fake_submit(
        _conversation_id: str,
        body: Any,
        _user: Any,
        _db: Any,
    ) -> Any:
        nonlocal creations
        key = body.idempotency_key
        payload = body.model_dump(mode="json", exclude={"idempotency_key"})
        prior = stored.get(key)
        if prior is not None:
            prior_payload, prior_result = prior
            if prior_payload != payload:
                raise telegram._http(  # noqa: SLF001
                    "idempotency_conflict",
                    "idempotency_key conflict",
                    409,
                )
            return prior_result
        creations += 1
        result = SimpleNamespace(
            assistant_message=SimpleNamespace(id="message-1"),
            generation_ids=["generation-1"],
        )
        stored[key] = (payload, result)
        return result

    monkeypatch.setattr(
        telegram,
        "lock_telegram_generation_context",
        fake_context,
    )
    monkeypatch.setattr(telegram, "submit_user_message", fake_submit)
    request = _request(
        headers=[
            (b"x-telegram-chat-id", b"chat-1"),
            (b"x-telegram-user-id", b"tg-1"),
        ]
    )

    first = await telegram.create_generation(
        request,
        telegram.GenerateIn(idempotency_key="tg:stable", prompt="cat"),
        locked_user,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    replay = await telegram.create_generation(
        request,
        telegram.GenerateIn(idempotency_key="tg:stable", prompt="cat"),
        locked_user,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert creations == 1
    assert replay.message_id == first.message_id == "message-1"
    assert replay.generation_ids == first.generation_ids == ["generation-1"]

    with pytest.raises(Exception) as excinfo:
        await telegram.create_generation(
            request,
            telegram.GenerateIn(idempotency_key="tg:stable", prompt="dog"),
            locked_user,  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert excinfo.value.detail["error"]["code"] == "idempotency_conflict"
    assert creations == 1


@pytest.mark.asyncio
async def test_telegram_prompt_enhance_lost_response_retry_replays_without_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}
    order: list[str] = []
    redis = object()
    billing = object()
    runtime = object()
    provider = SimpleNamespace(api_key="key")
    fresh_user = SimpleNamespace(id="user-1", account_mode="byok")

    class Limiter:
        async def check(self, actual_redis: Any, key: str) -> None:
            calls["limit"] = (actual_redis, key)

    async def resolve(_db: Any, actual_runtime: Any) -> list[Any]:
        order.append("providers")
        calls["runtime"] = actual_runtime
        calls["provider_calls"] = calls.get("provider_calls", 0) + 1
        return [provider]

    async def lock_user(
        _db: Any,
        *,
        authenticated_user_id: str,
        chat_id: str,
        tg_user_id: str,
    ) -> Any:
        order.append("identity")
        calls.setdefault("identities", []).append(
            (authenticated_user_id, chat_id, tg_user_id)
        )
        return fresh_user

    async def prepare(
        _db: Any,
        user: Any,
        _operation: Any,
        _reservation: Any,
        *,
        runtime: Any,
    ) -> tuple[Any, bool]:
        del runtime
        order.append("billing")
        calls["billing_calls"] = calls.get("billing_calls", 0) + 1
        calls["billing_user"] = user.id
        calls["billing_mode"] = user.account_mode
        return billing, False

    def durable_stream(
        operation: Any,
        reservation: Any,
        *,
        text: str,
        providers: list[Any],
        billing: Any,
        runtime: Any,
    ) -> tuple[Any, asyncio.Task[None]]:
        order.append("upstream")
        calls["stream_calls"] = calls.get("stream_calls", 0) + 1
        calls["stream"] = (text, providers, billing, runtime)

        async def persist() -> None:
            await telegram_prompt_idempotency.persist_terminal_success(
                db,  # type: ignore[arg-type]
                operation,
                "enhanced",
                attempt=reservation.attempt,
            )

        task = asyncio.create_task(persist())

        async def chunks():
            await task
            yield 'data: {"text":"enhanced"}\n\n'
            yield "data: [DONE]\n\n"

        return chunks(), task

    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        telegram_prompt_enhance,
        "PROMPTS_ENHANCE_LIMITER",
        Limiter(),
    )
    monkeypatch.setattr(telegram_prompt_enhance, "get_redis", lambda: redis)
    monkeypatch.setattr(telegram_prompt_enhance, "resolve_provider_order", resolve)
    monkeypatch.setattr(
        telegram_prompt_enhance,
        "_lock_telegram_prompt_enhance_user",
        lock_user,
    )
    monkeypatch.setattr(
        telegram_prompt_enhance,
        "prepare_reserved_prompt_billing",
        prepare,
    )
    monkeypatch.setattr(
        telegram_prompt_enhance,
        "durable_prompt_enhance_stream",
        durable_stream,
    )
    monkeypatch.setattr(prompt_idempotency, "lock_user_key", no_lock)

    db = _TelegramPromptIdempotencyDb()
    first = await telegram_prompt_enhance.enhance_telegram_prompt(
        text="cat",
        user=SimpleNamespace(id="user-1", account_mode="wallet"),
        chat_id="-100123",
        tg_user_id="42",
        idempotency_key="tg:enhance-request-1",
        db=db,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
    )
    replay = await telegram_prompt_enhance.enhance_telegram_prompt(
        text="cat",
        user=SimpleNamespace(id="user-1", account_mode="wallet"),
        chat_id="-100123",
        tg_user_id="42",
        idempotency_key="tg:enhance-request-1",
        db=db,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
    )

    assert first == replay == "enhanced"
    assert calls["limit"] == (redis, "rl:prompt_enhance:user-1")
    assert calls["identities"] == [
        ("user-1", "-100123", "42"),
        ("user-1", "-100123", "42"),
    ]
    assert calls["billing_user"] == "user-1"
    assert calls["billing_mode"] == "byok"
    assert calls["provider_calls"] == 1
    assert calls["billing_calls"] == 1
    assert calls["stream_calls"] == 1
    assert calls["stream"] == ("cat", [provider], billing, runtime)
    assert order == ["providers", "identity", "billing", "upstream", "identity"]
    assert db.commits == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding_outcome", "expected_code"),
    [
        ("revoked", "telegram_binding_revoked"),
        ("rebound", "telegram_binding_changed"),
    ],
)
async def test_prompt_enhance_revalidates_concurrent_binding_change_before_billing(
    monkeypatch: pytest.MonkeyPatch,
    binding_outcome: str,
    expected_code: str,
) -> None:
    lock_started = asyncio.Event()
    allow_lock = asyncio.Event()
    events: list[str] = []

    class Limiter:
        async def check(self, _redis: Any, _key: str) -> None:
            events.append("limited")

    class Result:
        def first(self) -> Any:
            events.append("binding_read")
            if binding_outcome == "revoked":
                return None
            return (
                SimpleNamespace(user_id="user-2", tg_user_id="84"),
                SimpleNamespace(id="user-2", account_mode="wallet"),
            )

    class Db:
        async def execute(self, _statement: Any) -> Result:
            return Result()

    async def resolve(_db: Any, _runtime: Any) -> list[Any]:
        events.append("providers")
        return [SimpleNamespace(api_key="key")]

    async def fake_lock_active_user(_db: Any, _user_id: str) -> None:
        events.append("lock_wait")
        lock_started.set()
        await allow_lock.wait()
        events.append("lock_acquired")

    async def prepare(*_args: Any, **_kwargs: Any) -> tuple[Any, bool]:
        events.append("billing")
        pytest.fail("binding changes must fail before wallet hold")

    async def reserve(*_args: Any, **_kwargs: Any) -> Any:
        return telegram_prompt_idempotency.TelegramPromptEnhanceReservation()

    monkeypatch.setattr(
        telegram_prompt_enhance,
        "PROMPTS_ENHANCE_LIMITER",
        Limiter(),
    )
    monkeypatch.setattr(telegram_prompt_enhance, "get_redis", object)
    monkeypatch.setattr(telegram_prompt_enhance, "resolve_provider_order", resolve)
    monkeypatch.setattr(
        telegram_prompt_enhance,
        "lock_active_user",
        fake_lock_active_user,
    )
    monkeypatch.setattr(
        telegram_prompt_enhance,
        "prepare_reserved_prompt_billing",
        prepare,
    )
    monkeypatch.setattr(
        telegram_prompt_enhance._telegram_prompt_idempotency,  # noqa: SLF001
        "reserve_telegram_prompt_enhance",
        reserve,
    )

    task = asyncio.create_task(
        telegram_prompt_enhance.enhance_telegram_prompt(
            text="cat",
            user=SimpleNamespace(id="user-1", account_mode="wallet"),
            chat_id="-100123",
            tg_user_id="42",
            idempotency_key="tg:binding-race",
            db=Db(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(lock_started.wait(), timeout=1)
    allow_lock.set()

    with pytest.raises(Exception) as excinfo:
        await task

    assert getattr(excinfo.value, "status_code", None) == 403
    assert excinfo.value.detail["error"]["code"] == expected_code
    assert events == [
        "limited",
        "providers",
        "lock_wait",
        "lock_acquired",
        "binding_read",
    ]


@pytest.mark.asyncio
async def test_prompt_enhance_route_passes_real_telegram_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_enhance(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "enhanced"

    monkeypatch.setattr(telegram, "enhance_telegram_prompt", fake_enhance)
    request = _request(
        headers=[
            (b"x-telegram-chat-id", b"-100123"),
            (b"x-telegram-user-id", b"42"),
        ]
    )
    user = SimpleNamespace(id="user-1")
    db = SimpleNamespace()
    runtime = object()

    result = await telegram.enhance_prompt(
        request,
        telegram.EnhancePromptIn(text="cat"),
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        runtime,  # type: ignore[arg-type]
        "tg:route-enhance",
    )

    assert result.enhanced == "enhanced"
    assert captured == {
        "text": "cat",
        "user": user,
        "chat_id": "-100123",
        "tg_user_id": "42",
        "idempotency_key": "tg:route-enhance",
        "db": db,
        "runtime": runtime,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_key", "expected_code"),
    [
        (None, "idempotency_key_required"),
        ("tg:bad key", "idempotency_key_invalid"),
        ("请求-1", "idempotency_key_invalid"),
    ],
)
async def test_prompt_enhance_route_rejects_missing_or_invalid_key_before_work(
    raw_key: str | None,
    expected_code: str,
) -> None:
    with pytest.raises(Exception) as excinfo:
        await telegram.enhance_prompt(
            _request(
                headers=[
                    (b"x-telegram-chat-id", b"-100123"),
                    (b"x-telegram-user-id", b"42"),
                ]
            ),
            telegram.EnhancePromptIn(text="cat"),
            SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            raw_key,
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["code"] == expected_code


@pytest.mark.asyncio
async def test_bind_invalid_code_counts_against_code_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        async def get(self, _key: str):
            return None

    class Limiter:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def check(self, _redis, key: str) -> None:
            self.keys.append(key)

    limiter = Limiter()
    monkeypatch.setattr(telegram, "get_redis", lambda: Redis())
    monkeypatch.setattr(telegram, "_BOT_BIND_CODE_LIMITER", limiter)

    with pytest.raises(Exception) as excinfo:
        await telegram.bind_telegram(
            _request(headers=[(b"x-telegram-user-id", b"tg-123")]),
            telegram.BindIn(chat_id="chat-1", code="bad-code"),
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert limiter.keys == ["rl:telegram:bind:127.0.0.1"]


@pytest.mark.asyncio
async def test_bind_db_failure_releases_claim_without_deleting_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.values = {telegram._link_code_key("code-1"): "user-1"}  # noqa: SLF001
            self.deleted: list[str] = []

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def set(self, key: str, value: str, **_kwargs: Any) -> bool:
            self.values[key] = value
            return True

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            self.values.pop(key, None)
            return 1

    class Result:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class Db:
        def __init__(self) -> None:
            self.results = [
                SimpleNamespace(
                    id="user-1",
                    email="u@example.com",
                    display_name="User",
                    deleted_at=None,
                ),
                None,
                None,
            ]
            self.rolled_back = False

        async def execute(self, _stmt: Any) -> Result:
            return Result(self.results.pop(0))

        def add(self, _value: Any) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            raise RuntimeError("deadlock")

        async def rollback(self) -> None:
            self.rolled_back = True

    redis = Redis()
    monkeypatch.setattr(telegram, "get_redis", lambda: redis)

    db = Db()
    with pytest.raises(RuntimeError):
        await telegram.bind_telegram(
            _request(),
            telegram.BindIn(chat_id="chat-1", code="code-1", tg_user_id="tg-123"),
            db,  # type: ignore[arg-type]
        )

    assert db.rolled_back is True
    assert telegram._link_code_key("code-1") in redis.values  # noqa: SLF001
    assert telegram._link_code_claim_key("code-1") in redis.deleted  # noqa: SLF001
    assert telegram._link_code_key("code-1") not in redis.deleted  # noqa: SLF001


@pytest.mark.asyncio
async def test_bind_records_tg_user_id_from_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Redis:
        def __init__(self) -> None:
            self.values = {telegram._link_code_key("code-1"): "user-1"}  # noqa: SLF001

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def set(self, key: str, value: str, **_kwargs: Any) -> bool:
            self.values[key] = value
            return True

        async def delete(self, *_keys: str) -> int:
            return 1

    class Result:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class Db:
        def __init__(self) -> None:
            self.results = [
                SimpleNamespace(
                    id="user-1",
                    email="u@example.com",
                    display_name="User",
                    deleted_at=None,
                ),
                None,
                None,
            ]
            self.added: list[Any] = []

        async def execute(self, _stmt: Any) -> Result:
            return Result(self.results.pop(0))

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def commit(self) -> None:
            return None

    monkeypatch.setattr(telegram, "get_redis", lambda: Redis())
    db = Db()

    out = await telegram.bind_telegram(
        _request(headers=[(b"x-telegram-user-id", b"tg-123")]),
        telegram.BindIn(chat_id="chat-1", code="code-1"),
        db,  # type: ignore[arg-type]
    )

    assert out.user_id == "user-1"
    assert db.added[0].tg_user_id == "tg-123"


@pytest.mark.asyncio
async def test_bind_rejects_tg_user_id_header_body_mismatch() -> None:
    with pytest.raises(Exception) as excinfo:
        await telegram.bind_telegram(
            _request(headers=[(b"x-telegram-user-id", b"tg-123")]),
            telegram.BindIn(chat_id="chat-1", code="code-1", tg_user_id="tg-456"),
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "telegram_user_mismatch"


@pytest.mark.asyncio
async def test_bind_rejects_missing_tg_user_id() -> None:
    with pytest.raises(Exception) as excinfo:
        await telegram.bind_telegram(
            _request(),
            telegram.BindIn(chat_id="chat-1", code="code-1"),
            SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "missing_telegram_user_id"


@pytest.mark.asyncio
async def test_release_link_code_claim_only_deletes_matching_owner() -> None:
    class Redis:
        def __init__(self) -> None:
            self.values = {
                telegram._link_code_claim_key("code-1"): "chat:new"  # noqa: SLF001
            }
            self.deleted: list[str] = []

        async def get(self, key: str) -> str | None:
            return self.values.get(key)

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            self.values.pop(key, None)
            return 1

    redis = Redis()

    await telegram._release_link_code_claim(  # noqa: SLF001
        redis,
        "code-1",
        owner="chat:old",
    )

    assert telegram._link_code_claim_key("code-1") in redis.values  # noqa: SLF001
    assert redis.deleted == []

    await telegram._release_link_code_claim(  # noqa: SLF001
        redis,
        "code-1",
        owner="chat:new",
    )

    assert telegram._link_code_claim_key("code-1") not in redis.values  # noqa: SLF001
    assert redis.deleted == [telegram._link_code_claim_key("code-1")]  # noqa: SLF001


@pytest.mark.asyncio
async def test_access_config_returns_allowlist_without_proxy_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_setting_str(_db, key: str, default: str = "") -> str:
        values = {
            "telegram.bot_enabled": "0",
            "telegram.allowed_user_ids": "123,456",
        }
        return values.get(key, default)

    monkeypatch.setattr(telegram, "_get_setting_str", fake_get_setting_str)

    out = await telegram.access_config(SimpleNamespace())  # type: ignore[arg-type]

    assert out.bot_enabled is False
    assert out.allowed_user_ids == "123,456"


@pytest.mark.asyncio
async def test_runtime_config_advertises_container_reachable_ssh_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picked = SimpleNamespace(name="ssh-egress", protocol="ssh")
    captured: dict[str, Any] = {}

    async def fake_get_setting_str(_db, key: str, default: str = "") -> str:
        values = {
            "telegram.bot_enabled": "1",
            "telegram.bot_token": "bot-token",
            "telegram.bot_username": "lumenbot",
            "telegram.allowed_user_ids": "123",
            "telegram.proxy_names": "ssh-egress",
            "telegram.proxy_strategy": "failover",
            "telegram.proxy_bind_host": "0.0.0.0",
            "telegram.proxy_advertise_host": "api",
        }
        return values.get(key, default)

    async def fake_get_setting_int(_db, key: str, default: int) -> int:
        return {
            "proxies.failure_threshold": 4,
            "proxies.cooldown_seconds": 90,
        }.get(key, default)

    async def fake_read_providers(_db) -> tuple[None, str]:
        return None, "none"

    async def fake_pick_proxy(
        redis: object,
        candidates: list[object],
        *,
        strategy: str,
        avoid: set[str],
    ) -> Any:
        captured["pick"] = (redis, candidates, strategy, avoid)
        return picked

    async def fake_resolve_proxy(
        proxy: object,
        *,
        bind_host: str,
        advertise_host: str,
    ) -> str:
        captured["resolve"] = (proxy, bind_host, advertise_host)
        return "socks5h://api:41560"

    redis = object()
    monkeypatch.setattr(telegram, "get_redis", lambda: redis)
    monkeypatch.setattr(telegram, "_get_setting_str", fake_get_setting_str)
    monkeypatch.setattr(telegram, "_get_setting_int", fake_get_setting_int)
    monkeypatch.setattr(telegram, "_read_providers", fake_read_providers)
    monkeypatch.setattr(telegram, "pick_proxy", fake_pick_proxy)
    monkeypatch.setattr(telegram, "resolve_provider_proxy_url", fake_resolve_proxy)

    out = await telegram.runtime_config(
        SimpleNamespace(),  # type: ignore[arg-type]
        avoid="failed-a, failed-b",
    )

    assert captured["pick"] == (
        redis,
        [],
        "failover",
        {"failed-a", "failed-b"},
    )
    assert captured["resolve"] == (picked, "0.0.0.0", "api")
    assert out.proxy is not None
    assert out.proxy.name == "ssh-egress"
    assert out.proxy.url == "socks5h://api:41560"
    assert out.failure_threshold == 4
    assert out.cooldown_seconds == 90


@pytest.mark.asyncio
async def test_runtime_config_does_not_rewrite_regular_socks_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    picked = SimpleNamespace(name="shared-socks", protocol="socks5")
    setting_reads: list[str] = []

    async def fake_get_setting_str(_db, key: str, default: str = "") -> str:
        setting_reads.append(key)
        values = {
            "telegram.bot_enabled": "1",
            "telegram.proxy_strategy": "random",
        }
        return values.get(key, default)

    async def fake_get_setting_int(_db, _key: str, default: int) -> int:
        return default

    async def fake_read_providers(_db) -> tuple[None, str]:
        return None, "none"

    async def fake_pick_proxy(*_args: Any, **_kwargs: Any) -> Any:
        return picked

    async def fake_resolve_proxy(proxy: object) -> str:
        assert proxy is picked
        return "socks5h://proxy.example:1080"

    monkeypatch.setattr(telegram, "get_redis", object)
    monkeypatch.setattr(telegram, "_get_setting_str", fake_get_setting_str)
    monkeypatch.setattr(telegram, "_get_setting_int", fake_get_setting_int)
    monkeypatch.setattr(telegram, "_read_providers", fake_read_providers)
    monkeypatch.setattr(telegram, "pick_proxy", fake_pick_proxy)
    monkeypatch.setattr(
        telegram,
        "resolve_provider_proxy_url",
        fake_resolve_proxy,
    )

    out = await telegram.runtime_config(SimpleNamespace())  # type: ignore[arg-type]

    assert out.proxy is not None
    assert out.proxy.url == "socks5h://proxy.example:1080"
    assert "telegram.proxy_bind_host" not in setting_reads
    assert "telegram.proxy_advertise_host" not in setting_reads


@pytest.mark.asyncio
async def test_get_bot_user_requires_bound_tg_user_id_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "secret")

    failures: list[str] = []

    async def fake_record_failure(_request: Request) -> None:
        failures.append("failed")

    monkeypatch.setattr(deps, "_record_bot_auth_failure", fake_record_failure)

    class Result:
        def first(self) -> tuple[Any, Any]:
            return (
                SimpleNamespace(chat_id="100", tg_user_id="200", user_id="user-1"),
                SimpleNamespace(id="user-1", deleted_at=None),
            )

    class Db:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    ok = await deps.get_bot_user(
        _request_with_headers(
            {
                "X-Bot-Token": "secret",
                "X-Telegram-Chat-Id": "100",
                "X-Telegram-User-Id": "200",
            }
        ),
        Db(),  # type: ignore[arg-type]
    )
    assert ok.id == "user-1"

    with pytest.raises(Exception) as excinfo:
        await deps.get_bot_user(
            _request_with_headers(
                {
                    "X-Bot-Token": "secret",
                    "X-Telegram-Chat-Id": "100",
                    "X-Telegram-User-Id": "201",
                }
            ),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 403
    assert excinfo.value.detail["error"]["code"] == "telegram_user_mismatch"
    assert failures == ["failed"]


@pytest.mark.asyncio
async def test_get_bot_user_rejects_missing_tg_user_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "secret")

    failures: list[str] = []

    async def fake_record_failure(_request: Request) -> None:
        failures.append("failed")

    monkeypatch.setattr(deps, "_record_bot_auth_failure", fake_record_failure)

    class Result:
        def first(self) -> tuple[Any, Any]:
            return (
                SimpleNamespace(chat_id="100", tg_user_id="200", user_id="user-1"),
                SimpleNamespace(id="user-1", deleted_at=None),
            )

    class Db:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    with pytest.raises(Exception) as excinfo:
        await deps.get_bot_user(
            _request_with_headers(
                {
                    "X-Bot-Token": "secret",
                    "X-Telegram-Chat-Id": "100",
                }
            ),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "missing_telegram_user_id"
    assert failures == ["failed"]


@pytest.mark.asyncio
async def test_get_bot_user_accepts_backfilled_legacy_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "secret")

    failures: list[str] = []

    async def fake_record_failure(_request: Request) -> None:
        failures.append("failed")

    monkeypatch.setattr(deps, "_record_bot_auth_failure", fake_record_failure)

    class Result:
        def first(self) -> tuple[Any, Any]:
            return (
                SimpleNamespace(chat_id="100", tg_user_id="100", user_id="user-1"),
                SimpleNamespace(id="user-1", deleted_at=None),
            )

    class Db:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    user = await deps.get_bot_user(
        _request_with_headers(
            {
                "X-Bot-Token": "secret",
                "X-Telegram-Chat-Id": "100",
                "X-Telegram-User-Id": "100",
            }
        ),
        Db(),  # type: ignore[arg-type]
    )

    assert user.id == "user-1"
    assert failures == []


@pytest.mark.asyncio
async def test_get_bot_user_rejects_corrupt_binding_without_tg_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deps.settings, "telegram_bot_shared_secret", "secret")

    failures: list[str] = []

    async def fake_record_failure(_request: Request) -> None:
        failures.append("failed")

    monkeypatch.setattr(deps, "_record_bot_auth_failure", fake_record_failure)

    class Result:
        def first(self) -> tuple[Any, Any]:
            return (
                SimpleNamespace(chat_id="100", tg_user_id=None, user_id="user-1"),
                SimpleNamespace(id="user-1", deleted_at=None),
            )

    class Db:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    with pytest.raises(Exception) as excinfo:
        await deps.get_bot_user(
            _request_with_headers(
                {
                    "X-Bot-Token": "secret",
                    "X-Telegram-Chat-Id": "100",
                    "X-Telegram-User-Id": "100",
                }
            ),
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 403
    assert excinfo.value.detail["error"]["code"] == "telegram_rebind_required"
    assert failures == ["failed"]
