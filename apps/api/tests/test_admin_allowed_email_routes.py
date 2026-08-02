"""Admin allowed-email route unit tests (no external services)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app.routes.admin_allowed_email_routes import (
    AllowedEmailDependencies,
    add_allowed_email,
)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/allowed_emails",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def _deps(audit_calls: list[str]) -> AllowedEmailDependencies:
    async def audit(*_args, **_kwargs) -> None:
        audit_calls.append("write")

    return AllowedEmailDependencies(
        http_error=lambda code, msg, http: HTTPException(
            status_code=http,
            detail={"error": {"code": code, "message": msg}},
        ),
        write_admin_audit=audit,
        hash_email=lambda email: f"h-{email}",
    )


@pytest.mark.asyncio
async def test_add_allowed_email_duplicate_precheck_returns_409() -> None:
    class Result:
        def scalar_one_or_none(self):
            return object()  # row already present

    class Db:
        async def execute(self, *_args, **_kwargs) -> Result:
            return Result()

        async def flush(self) -> None:
            raise AssertionError("flush must not run")

    with pytest.raises(HTTPException) as exc_info:
        await add_allowed_email(
            body=SimpleNamespace(email="Admin@Example.com "),
            request=_request(),
            admin=SimpleNamespace(id="admin-1", email="admin@example.com"),
            db=Db(),  # type: ignore[arg-type]
            deps=_deps([]),
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"]["code"] == "already_exists"


@pytest.mark.asyncio
async def test_add_allowed_email_race_integrity_error_returns_409_and_rolls_back() -> None:
    """并发重复:唯一约束在 flush 抛 IntegrityError,须转 409 而非泄漏 500。"""

    class Result:
        def scalar_one_or_none(self):
            return None  # pre-check misses the concurrent insert

    class Db:
        def __init__(self) -> None:
            self.rolled_back = False
            self.added = None

        def add(self, obj) -> None:
            self.added = obj

        async def execute(self, *_args, **_kwargs) -> Result:
            return Result()

        async def flush(self) -> None:
            raise IntegrityError("insert", {}, Exception("UNIQUE constraint"))

        async def rollback(self) -> None:
            self.rolled_back = True

        async def commit(self) -> None:
            raise AssertionError("commit must not run")

        async def refresh(self, _obj) -> None:
            raise AssertionError("refresh must not run")

    audit_calls: list[str] = []
    db = Db()
    with pytest.raises(HTTPException) as exc_info:
        await add_allowed_email(
            body=SimpleNamespace(email="admin@example.com"),
            request=_request(),
            admin=SimpleNamespace(id="admin-1", email="admin@example.com"),
            db=db,  # type: ignore[arg-type]
            deps=_deps(audit_calls),
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"]["code"] == "already_exists"
    assert db.rolled_back is True
    assert audit_calls == []  # 冲突时与前置检查路径一致,不写审计


@pytest.mark.asyncio
async def test_add_allowed_email_success_writes_audit_and_commits() -> None:
    from datetime import datetime, timezone

    class Result:
        def scalar_one_or_none(self):
            return None

    class Db:
        def add(self, _obj) -> None:
            pass

        async def execute(self, *_args, **_kwargs) -> Result:
            return Result()

        async def flush(self) -> None:
            pass

        async def commit(self) -> None:
            pass

        async def rollback(self) -> None:
            raise AssertionError("rollback must not run")

        async def refresh(self, allowed) -> None:
            allowed.id = "ae-1"
            allowed.created_at = datetime(2026, 8, 2, tzinfo=timezone.utc)

    audit_calls: list[str] = []
    out = await add_allowed_email(
        body=SimpleNamespace(email="admin@example.com"),
        request=_request(),
        admin=SimpleNamespace(id="admin-1", email="admin@example.com"),
        db=Db(),  # type: ignore[arg-type]
        deps=_deps(audit_calls),
    )
    assert out.id == "ae-1"
    assert out.email == "admin@example.com"
    assert audit_calls == ["write"]
