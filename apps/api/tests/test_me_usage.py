from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db as app_db
from app import deps
from app.routes import me


class _UsageResult:
    def one(self):
        return SimpleNamespace(
            messages_count=0,
            generations_count=0,
            generations_succeeded=0,
            generations_failed=0,
            completions_count=0,
            completions_succeeded=0,
            completions_failed=0,
            total_pixels_generated=0,
            total_tokens_in=0,
            total_tokens_out=0,
            storage_bytes=0,
        )


class _UsageDb:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _UsageResult()


def _client(db: _UsageDb) -> TestClient:
    app = FastAPI()
    app.include_router(me.router)

    async def override_user():
        return SimpleNamespace(id="user_1", email="user@example.com")

    async def override_db():
        return db

    app.dependency_overrides[deps.get_current_user] = override_user
    app.dependency_overrides[app_db.get_db] = override_db
    return TestClient(app)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_usage_days_controls_response_range() -> None:
    db = _UsageDb()
    client = _client(db)

    response = client.get("/me/usage?days=7")

    assert response.status_code == 200
    payload = response.json()
    actual = _parse_dt(payload["range_end"]) - _parse_dt(payload["range_start"])
    assert abs(actual - timedelta(days=7)) < timedelta(seconds=1)
    assert len(db.statements) == 1


def test_usage_days_rejects_out_of_range_values() -> None:
    client = _client(_UsageDb())

    assert client.get("/me/usage?days=0").status_code == 422
    assert client.get("/me/usage?days=366").status_code == 422
