from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Response
from sqlalchemy.dialects import postgresql
from starlette.requests import Request

from app import deps
from app.images.adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from app.images.application import http_routes
from app.images.application.upload import UploadCommandService
from app.images.domain.artifact import ArtifactStatus
from app.routes import conversations, me, messages, regenerate, tasks
from app.services.active_user import (
    ActiveSessionRevoked,
    ActiveUserDeleted,
    lock_active_user,
)
from app.services.message_request import AssistantContextRuntime
from app.services.video import submission as video_submission
from lumen_core.schemas import VideoCreateIn


class _Result:
    def __init__(
        self,
        *,
        scalar: Any = None,
        first: Any = None,
        rowcount: int = 0,
    ) -> None:
        self._scalar = scalar
        self._first = first
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def first(self) -> Any:
        return self._first


class _SharedUserState:
    def __init__(self) -> None:
        self.user_id = "user-1"
        self.deleted = False


class _SharedSessionState(_SharedUserState):
    def __init__(self) -> None:
        super().__init__()
        self.session_id = "session-1"
        self.session_revoked = False


class _AuthDb:
    def __init__(self, user: Any) -> None:
        self.user = user

    async def execute(self, _statement: Any) -> _Result:
        session = SimpleNamespace(
            revoked_at=None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        return _Result(first=(session, self.user))


class _DeleteDb:
    def __init__(self, state: _SharedUserState) -> None:
        self.state = state
        self.committed = False
        self.statements: list[Any] = []
        self._results = [
            _Result(scalar=state.user_id),
            _Result(rowcount=1),
            _Result(rowcount=1),
            _Result(rowcount=1),
            _Result(rowcount=1),
        ]

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self._results.pop(0)

    async def commit(self) -> None:
        self.committed = True
        self.state.deleted = True


class _WriteDb:
    def __init__(self, state: _SharedUserState) -> None:
        self.state = state
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.committed = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(scalar=None if self.state.deleted else self.state.user_id)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _row: Any) -> None:
        raise AssertionError("a deleted user must not create a conversation")


class _PausedActiveUserDb:
    def __init__(self, state: _SharedUserState, results: list[_Result]) -> None:
        self.state = state
        self.results = results
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False
        self.active_user_lock_requested = asyncio.Event()
        self.allow_active_user_lock = asyncio.Event()

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        if "from users" in str(statement).lower():
            self.active_user_lock_requested.set()
            await self.allow_active_user_lock.wait()
            return _Result(
                scalar=None if self.state.deleted else self.state.user_id,
            )
        if not self.results:
            raise AssertionError(f"unexpected statement: {statement}")
        return self.results.pop(0)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        raise AssertionError("a deleted user must not flush durable work")

    async def commit(self) -> None:
        self.committed = True
        raise AssertionError("a deleted user must not commit durable work")

    async def rollback(self) -> None:
        self.rolled_back = True


class _PausedActiveSessionDb:
    def __init__(self, state: _SharedSessionState, results: list[_Result]) -> None:
        self.state = state
        self.results = results
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False
        self.active_session_lock_requested = asyncio.Event()
        self.allow_active_session_lock = asyncio.Event()

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        rendered = str(statement).lower()
        if "from users" in rendered:
            return _Result(
                scalar=None if self.state.deleted else self.state.user_id,
            )
        if "from auth_sessions" in rendered:
            self.active_session_lock_requested.set()
            await self.allow_active_session_lock.wait()
            return _Result(
                scalar=SimpleNamespace(
                    user_id=self.state.user_id,
                    revoked_at=(
                        datetime.now(timezone.utc)
                        if self.state.session_revoked
                        else None
                    ),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            )
        if not self.results:
            raise AssertionError(f"unexpected statement: {statement}")
        return self.results.pop(0)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        raise AssertionError("a revoked session must not flush durable work")

    async def commit(self) -> None:
        self.committed = True
        raise AssertionError("a revoked session must not commit durable work")

    async def rollback(self) -> None:
        self.rolled_back = True


class _SessionRevokeDb:
    def __init__(self, state: _SharedSessionState) -> None:
        self.state = state
        self.committed = False
        self.statements: list[Any] = []
        self.session = SimpleNamespace(
            id=state.session_id,
            user_id=state.user_id,
            revoked_at=None,
        )

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(scalar=self.session)

    async def commit(self) -> None:
        self.committed = True
        self.state.session_revoked = True


class _RepositorySession:
    def __init__(self, state: _SharedUserState) -> None:
        self.state = state
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.flushed = False
        self.committed = False

    async def __aenter__(self) -> "_RepositorySession":
        return self

    async def __aexit__(
        self,
        _exc_type: Any,
        _exc: Any,
        _traceback: Any,
    ) -> None:
        return None

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(scalar=None if self.state.deleted else self.state.user_id)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


class _NoStorageUploadService:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **_kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("a deleted user must not start upload storage work")


class _DeletedPublicationRepository:
    def __init__(self, state: _SharedUserState) -> None:
        self.state = state

    @asynccontextmanager
    async def active_user_fence(self, _user_id: str):
        if self.state.deleted:
            raise ActiveUserDeleted()
        yield


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
        }
    )


async def _authenticate_request(user: Any) -> Any:
    _session, authenticated_user = await deps.require_active_session_user(
        _request(),
        _AuthDb(user),  # type: ignore[arg-type]
        "session-1",
    )
    return authenticated_user


async def _authenticate_request_with_session(user: Any) -> tuple[Request, Any]:
    request = _request()
    _session, authenticated_user = await deps.require_active_session_user(
        request,
        _AuthDb(user),  # type: ignore[arg-type]
        "session-1",
    )
    return request, authenticated_user


async def _commit_account_deletion(
    monkeypatch: pytest.MonkeyPatch,
    state: _SharedUserState,
    user: Any,
) -> _DeleteDb:
    cleanup = {
        "generations_canceled": 0,
        "completions_canceled": 0,
        "video_generations_canceled": 0,
        "videos_deleted": 0,
        "memory_extractions_canceled": 0,
    }

    async def cancel_account_active_tasks(
        *_args: Any, **_kwargs: Any
    ) -> dict[str, int]:
        return cleanup

    async def write_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def post_commit_account_task_cleanup(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(me, "get_redis", lambda: object())
    monkeypatch.setattr(me, "cancel_account_active_tasks", cancel_account_active_tasks)
    monkeypatch.setattr(me, "write_audit", write_audit)
    monkeypatch.setattr(
        me,
        "post_commit_account_task_cleanup",
        post_commit_account_task_cleanup,
    )
    db = _DeleteDb(state)
    response = Response()

    out = await me.delete_my_account(
        None,  # type: ignore[arg-type]
        user,
        response,
        db,  # type: ignore[arg-type]
    )

    assert out is response
    assert db.committed is True
    assert state.deleted is True
    return db


def _assert_user_deleted(exc: HTTPException) -> None:
    assert exc.status_code == 401
    assert exc.detail == {
        "error": {"code": "user_deleted", "message": "user account was deleted"}
    }


def _assert_session_revoked(exc: HTTPException) -> None:
    assert exc.status_code == 401
    assert exc.detail == {
        "error": {"code": "session_revoked", "message": "session was revoked"}
    }


def _assert_active_user_lock(statement: Any) -> None:
    rendered = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert "FROM USERS" in rendered
    assert "USERS.DELETED_AT IS NULL" in rendered
    assert "FOR UPDATE" in rendered


def _assert_active_session_lock_order(statements: list[Any]) -> None:
    rendered = [
        str(statement.compile(dialect=postgresql.dialect())).upper()
        for statement in statements
    ]
    user_index = next(
        index for index, sql in enumerate(rendered) if "FROM USERS" in sql
    )
    session_index = next(
        index for index, sql in enumerate(rendered) if "FROM AUTH_SESSIONS" in sql
    )
    assert user_index < session_index
    assert "USERS.DELETED_AT IS NULL" in rendered[user_index]
    assert "FOR UPDATE" in rendered[user_index]
    assert "AUTH_SESSIONS.USER_ID" in rendered[session_index]
    assert "FOR UPDATE" in rendered[session_index]
    assert (
        statements[session_index].get_execution_options().get("populate_existing")
        is True
    )


async def _commit_session_revocation(
    monkeypatch: pytest.MonkeyPatch,
    state: _SharedSessionState,
    request: Request,
    user: Any,
) -> _SessionRevokeDb:
    async def write_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(me, "write_audit", write_audit)
    db = _SessionRevokeDb(state)
    await me.revoke_my_session(
        state.session_id,
        request,
        user,
        db,  # type: ignore[arg-type]
    )
    assert db.committed is True
    assert state.session_revoked is True
    assert "FOR UPDATE" in str(db.statements[0]).upper()
    return db


def _active_user(state: _SharedUserState) -> SimpleNamespace:
    return SimpleNamespace(
        id=state.user_id,
        email="member@example.com",
        account_mode="wallet",
        deleted_at=None,
        default_system_prompt_id=None,
    )


@pytest.mark.asyncio
async def test_authenticated_upload_after_delete_never_starts_storage_or_image_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedUserState()
    user = SimpleNamespace(
        id=state.user_id,
        email="member@example.com",
        account_mode="wallet",
        deleted_at=None,
    )
    authenticated_user = await _authenticate_request(user)
    await _commit_account_deletion(monkeypatch, state, authenticated_user)

    route_db = _WriteDb(state)
    upload_service = _NoStorageUploadService()
    with pytest.raises(HTTPException) as route_exc:
        await http_routes.upload_image(
            authenticated_user,
            route_db,  # type: ignore[arg-type]
            upload_service,  # type: ignore[arg-type]
            file=SimpleNamespace(filename="photo.png"),
            purpose=None,
            reference_width=None,
            reference_height=None,
        )

    _assert_user_deleted(route_exc.value)
    assert upload_service.calls == 0
    assert route_db.added == []
    assert route_db.committed is False
    assert "FOR UPDATE" not in str(route_db.statements[0]).upper()

    repository_session = _RepositorySession(state)
    repository = SQLAlchemyImageRepository(lambda: repository_session)  # type: ignore[arg-type]
    image = SimpleNamespace(
        artifact_status=ArtifactStatus.STAGING.value,
        user_id=state.user_id,
    )
    with pytest.raises(ActiveUserDeleted):
        await repository.create_staging(image)  # type: ignore[arg-type]

    assert repository_session.added == []
    assert repository_session.flushed is False
    assert repository_session.committed is False
    rendered = str(
        repository_session.statements[0].compile(dialect=postgresql.dialect())
    ).upper()
    assert "FOR UPDATE" in rendered
    assert "USERS.DELETED_AT IS NULL" in rendered


@pytest.mark.asyncio
async def test_authenticated_conversation_create_after_delete_never_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedUserState()
    user = SimpleNamespace(
        id=state.user_id,
        email="member@example.com",
        account_mode="wallet",
        deleted_at=None,
    )
    authenticated_user = await _authenticate_request(user)
    await _commit_account_deletion(monkeypatch, state, authenticated_user)

    db = _WriteDb(state)
    with pytest.raises(HTTPException) as excinfo:
        await conversations.create_conversation(
            conversations.ConversationCreateIn(),
            authenticated_user,
            db,  # type: ignore[arg-type]
        )

    _assert_user_deleted(excinfo.value)
    assert db.added == []
    assert db.committed is False
    rendered = str(db.statements[0].compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in rendered
    assert "USERS.DELETED_AT IS NULL" in rendered


@pytest.mark.asyncio
async def test_deleted_user_publication_fence_prevents_storage_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedUserState()
    user = SimpleNamespace(
        id=state.user_id,
        email="member@example.com",
        account_mode="wallet",
        deleted_at=None,
    )
    authenticated_user = await _authenticate_request(user)
    await _commit_account_deletion(monkeypatch, state, authenticated_user)

    service = object.__new__(UploadCommandService)
    service.repository = _DeletedPublicationRepository(state)  # type: ignore[attr-defined]
    published: list[str] = []

    with pytest.raises(ActiveUserDeleted):
        async with service._active_user_fence(authenticated_user.id):  # noqa: SLF001
            published.append("original")

    assert published == []


@pytest.mark.asyncio
async def test_authenticated_submit_started_before_delete_never_creates_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedUserState()
    user = _active_user(state)
    authenticated_user = await _authenticate_request(user)
    created_tasks: list[dict[str, Any]] = []

    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_visible_check(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_lookup(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_idempotency_lock(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def no_fast_default(_db: Any) -> bool:
        return False

    async def no_system_prompt(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_credential_pin(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_setting(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def task_must_not_be_created(**kwargs: Any) -> Any:
        created_tasks.append(kwargs)
        raise AssertionError("deleted user must not create an assistant task")

    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(
        messages,
        "_ensure_conversation_visible_to_user",
        no_visible_check,
    )
    monkeypatch.setattr(messages, "_lookup_idempotent_post", no_lookup)
    monkeypatch.setattr(messages, "_lock_idempotency_key", no_idempotency_lock)
    monkeypatch.setattr(messages, "_resolve_fast_default", no_fast_default)
    monkeypatch.setattr(messages, "_create_assistant_task", task_must_not_be_created)
    monkeypatch.setattr(
        messages,
        "_assistant_context_runtime",
        lambda: AssistantContextRuntime(
            resolve_system_prompt=no_system_prompt,
            resolve_credential_pin=no_credential_pin,
            get_setting=no_setting,
            default_image_output_format="png",
            image_output_format_values=set(),
        ),
    )

    conversation = SimpleNamespace(
        id="conv-1",
        user_id=state.user_id,
        deleted_at=None,
    )
    db = _PausedActiveUserDb(state, [_Result(scalar=conversation)])
    submit = asyncio.create_task(
        messages.post_message(
            "conv-1",
            messages.PostMessageIn(
                idempotency_key="delete-race-submit",
                text="draw a poster",
                intent="text_to_image",
            ),
            authenticated_user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    )
    await db.active_user_lock_requested.wait()
    await _commit_account_deletion(monkeypatch, state, authenticated_user)
    db.allow_active_user_lock.set()

    with pytest.raises(HTTPException) as excinfo:
        await submit

    _assert_user_deleted(excinfo.value)
    _assert_active_user_lock(db.statements[-1])
    assert db.added == []
    assert db.committed is False
    assert db.rolled_back is True
    assert created_tasks == []


@pytest.mark.asyncio
async def test_authenticated_silent_submit_started_before_delete_never_creates_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedUserState()
    user = _active_user(state)
    authenticated_user = await _authenticate_request(user)
    created_tasks: list[dict[str, Any]] = []

    async def no_visible_check(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_retention_policy(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_lookup(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_idempotency_lock(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def no_fast_default(_db: Any) -> bool:
        return False

    async def task_must_not_be_created(**kwargs: Any) -> Any:
        created_tasks.append(kwargs)
        raise AssertionError("deleted user must not create an assistant task")

    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(
        messages,
        "_ensure_conversation_visible_to_user",
        no_visible_check,
    )
    monkeypatch.setattr(
        messages, "_byok_retention_policy_for_user", no_retention_policy
    )
    monkeypatch.setattr(messages, "_lookup_silent_generation", no_lookup)
    monkeypatch.setattr(messages, "_lock_idempotency_key", no_idempotency_lock)
    monkeypatch.setattr(messages, "get_spec", lambda _key: None)
    monkeypatch.setattr(messages, "_resolve_fast_default", no_fast_default)
    monkeypatch.setattr(messages, "_create_assistant_task", task_must_not_be_created)

    conversation = SimpleNamespace(
        id="conv-1",
        user_id=state.user_id,
        deleted_at=None,
    )
    parent_message = SimpleNamespace(
        id="parent-1",
        conversation_id=conversation.id,
        role="user",
    )
    db = _PausedActiveUserDb(
        state,
        [
            _Result(scalar=conversation),
            _Result(scalar=parent_message),
        ],
    )
    submit = asyncio.create_task(
        messages.create_silent_generation(
            "conv-1",
            messages.SilentGenerationIn(
                idempotency_key="delete-race-silent",
                parent_message_id=parent_message.id,
            ),
            authenticated_user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    )
    await db.active_user_lock_requested.wait()
    await _commit_account_deletion(monkeypatch, state, authenticated_user)
    db.allow_active_user_lock.set()

    with pytest.raises(HTTPException) as excinfo:
        await submit

    _assert_user_deleted(excinfo.value)
    _assert_active_user_lock(db.statements[-1])
    assert db.added == []
    assert db.committed is False
    assert created_tasks == []


@pytest.mark.asyncio
async def test_authenticated_regenerate_started_before_delete_never_creates_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedUserState()
    user = _active_user(state)
    authenticated_user = await _authenticate_request(user)
    created_tasks: list[dict[str, Any]] = []

    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def task_must_not_be_created(**kwargs: Any) -> Any:
        created_tasks.append(kwargs)
        raise AssertionError("deleted user must not create an assistant task")

    monkeypatch.setattr(regenerate.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(regenerate, "get_redis", lambda: object())
    monkeypatch.setattr(regenerate, "_create_assistant_task", task_must_not_be_created)

    conversation = SimpleNamespace(
        id="conv-1",
        user_id=state.user_id,
        deleted_at=None,
    )
    target = SimpleNamespace(
        id="assistant-1",
        conversation_id=conversation.id,
        role="assistant",
        parent_message_id="parent-1",
        status="pending",
    )
    parent_message = SimpleNamespace(
        id="parent-1",
        conversation_id=conversation.id,
        role="user",
        content={"text": "hello", "attachments": []},
    )
    db = _PausedActiveUserDb(
        state,
        [
            _Result(scalar=conversation),
            _Result(scalar=target),
            _Result(scalar=parent_message),
            _Result(scalar=None),
            _Result(scalar=None),
        ],
    )
    regenerate_request = asyncio.create_task(
        regenerate.regenerate_message(
            conversation.id,
            target.id,
            regenerate.RegenerateIn(
                intent="chat",
                idempotency_key="delete-race-regenerate",
            ),
            authenticated_user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    )
    await db.active_user_lock_requested.wait()
    await _commit_account_deletion(monkeypatch, state, authenticated_user)
    db.allow_active_user_lock.set()

    with pytest.raises(HTTPException) as excinfo:
        await regenerate_request

    _assert_user_deleted(excinfo.value)
    _assert_active_user_lock(db.statements[-1])
    assert target.status == "pending"
    assert db.added == []
    assert db.committed is False
    assert created_tasks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("task_kind", ["generation", "completion"])
async def test_authenticated_retry_started_before_delete_never_commits_work(
    monkeypatch: pytest.MonkeyPatch,
    task_kind: str,
) -> None:
    state = _SharedUserState()
    user = _active_user(state)
    authenticated_user = await _authenticate_request(user)
    task = SimpleNamespace(
        id=f"{task_kind}-1",
        user_id=state.user_id,
        message_id="assistant-1",
        status="canceled",
        progress_stage="finalizing",
        attempt=1,
        error_code="cancelled",
        error_message="cancelled",
        started_at=None,
        finished_at=datetime.now(timezone.utc),
    )
    db = _PausedActiveUserDb(state, [_Result(scalar=task)])
    retry = (
        tasks.retry_generation if task_kind == "generation" else tasks.retry_completion
    )
    retry_request = asyncio.create_task(
        retry(
            task.id,
            authenticated_user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )
    )
    await db.active_user_lock_requested.wait()
    await _commit_account_deletion(monkeypatch, state, authenticated_user)
    db.allow_active_user_lock.set()

    with pytest.raises(HTTPException) as excinfo:
        await retry_request

    _assert_user_deleted(excinfo.value)
    assert len(db.statements) == 1
    _assert_active_user_lock(db.statements[0])
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_sessionless_active_user_lock_keeps_legacy_user_only_fence() -> None:
    state = _SharedSessionState()
    db = _WriteDb(state)

    await lock_active_user(db, state.user_id)

    assert len(db.statements) == 1
    _assert_active_user_lock(db.statements[0])


@pytest.mark.asyncio
async def test_revoked_session_upload_never_starts_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedSessionState()
    request, user = await _authenticate_request_with_session(_active_user(state))
    await _commit_session_revocation(monkeypatch, state, request, user)
    db = _PausedActiveSessionDb(state, [])
    db.allow_active_session_lock.set()
    upload_service = _NoStorageUploadService()

    with pytest.raises(HTTPException) as excinfo:
        await http_routes.upload_image(
            user,
            db,  # type: ignore[arg-type]
            upload_service,  # type: ignore[arg-type]
            file=SimpleNamespace(filename="photo.png"),
            purpose=None,
            reference_width=None,
            reference_height=None,
            request=request,
        )

    _assert_session_revoked(excinfo.value)
    assert upload_service.calls == 0
    assert len(db.statements) == 2
    rendered = [str(statement).upper() for statement in db.statements]
    assert "FOR UPDATE" not in rendered[0]
    assert "FOR UPDATE" not in rendered[1]


@pytest.mark.asyncio
async def test_revoked_session_wins_before_message_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedSessionState()
    request, user = await _authenticate_request_with_session(_active_user(state))
    created_tasks: list[dict[str, Any]] = []

    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_visible_check(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_lookup(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_idempotency_lock(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def no_fast_default(_db: Any) -> bool:
        return False

    async def no_system_prompt(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_credential_pin(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_setting(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def task_must_not_be_created(**kwargs: Any) -> Any:
        created_tasks.append(kwargs)
        raise AssertionError("a revoked session must not create an assistant task")

    monkeypatch.setattr(messages.MESSAGES_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(messages, "get_redis", lambda: object())
    monkeypatch.setattr(
        messages,
        "_ensure_conversation_visible_to_user",
        no_visible_check,
    )
    monkeypatch.setattr(messages, "_lookup_idempotent_post", no_lookup)
    monkeypatch.setattr(messages, "_lock_idempotency_key", no_idempotency_lock)
    monkeypatch.setattr(messages, "_resolve_fast_default", no_fast_default)
    monkeypatch.setattr(messages, "_create_assistant_task", task_must_not_be_created)
    monkeypatch.setattr(
        messages,
        "_assistant_context_runtime",
        lambda: AssistantContextRuntime(
            resolve_system_prompt=no_system_prompt,
            resolve_credential_pin=no_credential_pin,
            get_setting=no_setting,
            default_image_output_format="png",
            image_output_format_values=set(),
        ),
    )

    conversation = SimpleNamespace(
        id="conv-1",
        user_id=state.user_id,
        deleted_at=None,
    )
    db = _PausedActiveSessionDb(state, [_Result(scalar=conversation)])
    submit = asyncio.create_task(
        messages.post_message(
            "conv-1",
            messages.PostMessageIn(
                idempotency_key="session-race-message",
                text="draw a poster",
                intent="text_to_image",
            ),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            request=request,
        )
    )
    await db.active_session_lock_requested.wait()
    await _commit_session_revocation(monkeypatch, state, request, user)
    db.allow_active_session_lock.set()

    with pytest.raises(HTTPException) as excinfo:
        await submit

    _assert_session_revoked(excinfo.value)
    _assert_active_session_lock_order(db.statements[-2:])
    assert db.added == []
    assert db.committed is False
    assert db.rolled_back is True
    assert created_tasks == []


@pytest.mark.asyncio
async def test_revoked_session_wins_before_task_retry_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedSessionState()
    request, user = await _authenticate_request_with_session(_active_user(state))
    db = _PausedActiveSessionDb(state, [])

    retry = asyncio.create_task(
        tasks.retry_generation(
            "generation-1",
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            request=request,
        )
    )
    await db.active_session_lock_requested.wait()
    await _commit_session_revocation(monkeypatch, state, request, user)
    db.allow_active_session_lock.set()

    with pytest.raises(HTTPException) as excinfo:
        await retry

    _assert_session_revoked(excinfo.value)
    _assert_active_session_lock_order(db.statements)
    assert len(db.statements) == 2
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_video_submit_fences_durable_session_before_chargeable_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoCreateIn(
        action="t2v",
        model="seedance-2.0-fast",
        prompt="make a video",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="session-fence-video",
    )
    calls: list[tuple[str, Any]] = []

    async def no_winner(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def no_idempotency_lock(*_args: Any, **_kwargs: Any) -> None:
        calls.append(("idempotency", None))

    async def revoked_session_fence(
        _db: Any,
        user_id: str,
        *,
        session_id: str | None = None,
    ) -> None:
        calls.append(("session_fence", (user_id, session_id)))
        raise ActiveSessionRevoked()

    async def must_not_lock_references(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("revoked session must not lock reference media")

    async def must_not_hold(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("revoked session must not create a wallet hold")

    monkeypatch.setattr(
        video_submission,
        "_find_idempotent_generation",
        no_winner,
    )
    monkeypatch.setattr(video_submission, "lock_user_key", no_idempotency_lock)
    monkeypatch.setattr(
        video_submission,
        "lock_active_user",
        revoked_session_fence,
    )
    monkeypatch.setattr(
        video_submission,
        "lock_user_reference_media",
        must_not_lock_references,
    )
    monkeypatch.setattr(video_submission.billing_core, "hold", must_not_hold)

    with pytest.raises(HTTPException) as excinfo:
        await video_submission.create_video_generation_record(
            object(),  # type: ignore[arg-type]
            body,
            _active_user(_SharedSessionState()),
            context=video_submission.VideoSubmissionContext(
                session_id="session-1",
            ),
        )

    _assert_session_revoked(excinfo.value)
    assert calls == [
        ("idempotency", None),
        ("session_fence", ("user-1", "session-1")),
    ]


@pytest.mark.asyncio
async def test_revoked_session_wins_before_conversation_create_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SharedSessionState()
    request, user = await _authenticate_request_with_session(_active_user(state))
    db = _PausedActiveSessionDb(state, [])

    create = asyncio.create_task(
        conversations.create_conversation(
            conversations.ConversationCreateIn(title="race"),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
            request=request,
        )
    )
    await db.active_session_lock_requested.wait()
    await _commit_session_revocation(monkeypatch, state, request, user)
    db.allow_active_session_lock.set()

    with pytest.raises(HTTPException) as excinfo:
        await create

    _assert_session_revoked(excinfo.value)
    _assert_active_session_lock_order(db.statements)
    assert len(db.statements) == 2
    assert db.added == []
    assert db.committed is False
