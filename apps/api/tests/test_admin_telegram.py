from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError

from app.routes import admin_telegram
from app.services import telegram_control_dispatch


def _request() -> Request:
    app = SimpleNamespace(state=SimpleNamespace())
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/telegram/restart",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "app": app,
        }
    )


class RecordingDb:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit
        self.added: list[object] = []
        self.events: list[str] = []
        self.rolled_back = False
        self.rows: dict[str, object] = {}

    def add(self, row: object) -> None:
        self.added.append(row)
        row_id = str(getattr(row, "id"))
        self.rows[row_id] = row

    async def commit(self) -> None:
        self.events.append("commit")
        if self.fail_commit:
            raise IntegrityError("insert", {}, RuntimeError("duplicate active slot"))

    async def rollback(self) -> None:
        self.rolled_back = True
        self.events.append("rollback")

    async def get(self, _model: object, row_id: str) -> object | None:
        return self.rows.get(row_id)


@pytest.mark.asyncio
async def test_restart_is_committed_and_queued_before_publisher_wakeup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    audit_events: list[tuple[str, bool]] = []

    async def fake_audit(*_args: object, **kwargs: object) -> None:
        audit_events.append((str(kwargs["event_type"]), bool(kwargs["autocommit"])))

    def fake_wakeup(_request: Request) -> bool:
        assert db.events == ["commit"]
        db.events.append("wakeup")
        return True

    monkeypatch.setattr(admin_telegram, "write_admin_audit", fake_audit)
    monkeypatch.setattr(
        admin_telegram,
        "wake_telegram_control_publisher",
        fake_wakeup,
    )

    out = await admin_telegram.restart_bot(
        _request(),
        SimpleNamespace(id="admin-1"),
        db,  # type: ignore[arg-type]
    )

    assert out.command == "restart"
    assert out.status == "queued"
    assert out.command_id
    assert db.events == ["commit", "wakeup"]
    assert len(db.added) == 1
    row = db.added[0]
    assert getattr(row, "status") == "pending"
    assert getattr(row, "active_slot") == 1
    assert getattr(row, "requested_by") == "admin-1"
    assert audit_events == [("admin.telegram.restart.queued", False)]


@pytest.mark.asyncio
async def test_restart_commit_failure_never_wakes_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb(fail_commit=True)
    wakeups = 0

    async def fake_audit(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_wakeup(_request: Request) -> bool:
        nonlocal wakeups
        wakeups += 1
        return True

    monkeypatch.setattr(admin_telegram, "write_admin_audit", fake_audit)
    monkeypatch.setattr(
        admin_telegram,
        "wake_telegram_control_publisher",
        fake_wakeup,
    )

    with pytest.raises(HTTPException) as exc:
        await admin_telegram.restart_bot(
            _request(),
            SimpleNamespace(id="admin-1"),
            db,  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["error"]["code"] == "telegram_command_pending"
    assert db.rolled_back is True
    assert wakeups == 0


@pytest.mark.parametrize(
    ("internal", "public"),
    [
        ("pending", "queued"),
        ("published", "queued"),
        ("accepted", "accepted"),
        ("failed", "failed"),
    ],
)
def test_command_status_mapping_is_explicit(internal: str, public: str) -> None:
    row = SimpleNamespace(
        id="command-1",
        command="restart",
        status=internal,
        last_error=None,
    )

    assert admin_telegram._public_command(row).status == public  # noqa: SLF001


@pytest.mark.asyncio
async def test_restart_status_reads_durable_command() -> None:
    db = RecordingDb()
    row = SimpleNamespace(
        id="command-1",
        target="tgbot",
        command="restart",
        status="accepted",
        last_error=None,
    )
    db.rows[row.id] = row

    out = await admin_telegram.restart_status(
        row.id,
        SimpleNamespace(id="admin-1"),
        db,  # type: ignore[arg-type]
    )

    assert out.command_id == row.id
    assert out.status == "accepted"


@pytest.mark.asyncio
async def test_control_dedup_history_gets_finite_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "lumen:tgbot:control:v1:command:"

    class Redis:
        def __init__(self) -> None:
            self.expired: list[tuple[str, int]] = []

        async def scan(
            self,
            *,
            cursor: int,
            match: str,
            count: int,
        ) -> tuple[int, list[str]]:
            assert cursor == 0
            assert match == f"{prefix}*"
            assert count == 100
            return (
                17,
                [
                    f"{prefix}terminal",
                    f"{prefix}active",
                    f"{prefix}orphan",
                ],
            )

        async def ttl(self, _key: str) -> int:
            return -1

        async def expire(self, key: str, ttl_seconds: int) -> bool:
            self.expired.append((key, ttl_seconds))
            return True

    async def fake_statuses(command_ids: list[str]) -> dict[str, str]:
        assert command_ids == ["terminal", "active", "orphan"]
        return {"terminal": "accepted", "active": "published"}

    monkeypatch.setattr(
        telegram_control_dispatch,
        "_control_command_statuses",
        fake_statuses,
    )
    redis = Redis()

    cursor = await telegram_control_dispatch.maintain_control_dedup_keys(redis)

    assert cursor == 17
    assert redis.expired == [
        (f"{prefix}terminal", 90 * 24 * 60 * 60),
        (f"{prefix}orphan", 7 * 24 * 60 * 60),
    ]
