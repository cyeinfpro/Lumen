from __future__ import annotations


import pytest

import app.arq_pool as arq_pool
import app.audit as audit
import app.main as main
from app.observability import _scrub_value
from app.runtime_settings import get_settings_view
from app.security import make_session_cookie, parse_session_cookie


def test_session_cookie_contains_expiry_timestamp() -> None:
    raw = make_session_cookie("session-1")

    assert len(raw.split(".")) == 3
    assert parse_session_cookie(raw) == "session-1"


@pytest.mark.asyncio
async def test_body_size_limit_counts_chunked_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def app(_scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    monkeypatch.setattr(main, "_MAX_REQUEST_BYTES", 3)
    wrapped = main._BodySizeLimitMiddleware(app)
    messages = [
        {"type": "http.request", "body": b"aa", "more_body": True},
        {"type": "http.request", "body": b"bb", "more_body": False},
    ]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await wrapped(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
        receive,
        send,
    )

    statuses = [m.get("status") for m in sent if m["type"] == "http.response.start"]
    assert statuses == [413]


@pytest.mark.asyncio
async def test_hsts_uses_trusted_forwarded_proto(monkeypatch: pytest.MonkeyPatch) -> None:
    old = main.settings.trusted_proxies
    main.settings.trusted_proxies = "127.0.0.1/32"

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    try:
        await main._SecurityHeadersMiddleware(app)(
            {
                "type": "http",
                "scheme": "http",
                "method": "GET",
                "path": "/",
                "client": ("127.0.0.1", 12345),
                "headers": [(b"x-forwarded-proto", b"https")],
            },
            receive,
            send,
        )
    finally:
        main.settings.trusted_proxies = old

    headers = dict(sent[0]["headers"])
    assert b"strict-transport-security" in headers


@pytest.mark.asyncio
async def test_arq_pool_recreated_when_loop_marker_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = []
    created = []

    class Pool:
        async def close(self):
            closed.append(self)

    async def fake_create_pool(_settings):
        pool = Pool()
        created.append(pool)
        return pool

    monkeypatch.setattr(arq_pool, "create_pool", fake_create_pool)
    monkeypatch.setattr(arq_pool, "_pool", Pool())
    monkeypatch.setattr(arq_pool, "_pool_loop_id", -1)

    pool = await arq_pool.get_arq_pool()

    assert pool is created[0]
    assert len(closed) == 1


@pytest.mark.asyncio
async def test_write_audit_uses_isolated_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def fake_isolated(**kwargs):
        calls.append(kwargs)

    class PoisonSession:
        def add(self, _row):
            raise AssertionError("main session should not be used")

    monkeypatch.setattr(audit, "write_audit_isolated", fake_isolated)

    await audit.write_audit(
        PoisonSession(), event_type="event", user_id="user-1"  # type: ignore[arg-type]
    )

    assert calls == [
        {
            "event_type": "event",
            "user_id": "user-1",
            "actor_email": None,
            "actor_email_hash": None,
            "actor_ip_hash": None,
            "target_user_id": None,
            "details": None,
        }
    ]


@pytest.mark.asyncio
async def test_write_audit_can_use_caller_transaction() -> None:
    class CallerSession:
        def __init__(self):
            self.rows = []
            self.flushed = False

        def add(self, row):
            self.rows.append(row)

        async def flush(self):
            self.flushed = True

    session = CallerSession()

    await audit.write_audit(
        session,
        event_type="event",
        actor_email="USER@EXAMPLE.COM",
        details={"ok": True},
        autocommit=False,
    )

    assert session.flushed is True
    assert len(session.rows) == 1
    assert session.rows[0].event_type == "event"
    assert session.rows[0].actor_email_hash == audit.hash_email("USER@EXAMPLE.COM")
    assert session.rows[0].details == {"ok": True}


@pytest.mark.asyncio
async def test_write_audit_failure_increments_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Counter:
        def __init__(self) -> None:
            self.mode: str | None = None
            self.count = 0

        def labels(self, *, mode: str) -> "Counter":
            self.mode = mode
            return self

        def inc(self) -> None:
            self.count += 1

    class FailingSession:
        def add(self, _row):
            return None

        async def flush(self):
            raise RuntimeError("db unavailable")

    counter = Counter()
    monkeypatch.setattr(audit, "audit_write_failures_total", counter)

    await audit.write_audit(
        FailingSession(),  # type: ignore[arg-type]
        event_type="event",
        autocommit=False,
    )

    assert counter.mode == "session"
    assert counter.count == 1


def test_observability_sensitive_key_matching_avoids_token_substrings() -> None:
    assert _scrub_value("tokenizer_model", "cl100k") == "cl100k"
    assert _scrub_value("refresh_token", "secret") == "[redacted]"


@pytest.mark.asyncio
async def test_settings_view_does_not_expose_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from lumen_core.runtime_settings import SUPPORTED_SETTINGS

    class Result:
        def all(self):
            return []

    class Db:
        async def execute(self, _stmt):
            return Result()

    spec = next(s for s in SUPPORTED_SETTINGS if not s.sensitive)
    monkeypatch.setenv(spec.env_fallback, "env-value")

    items = await get_settings_view(Db())  # type: ignore[arg-type]
    item = next(i for i in items if i.key == spec.key)

    assert item.has_value is True
    assert item.value is None
