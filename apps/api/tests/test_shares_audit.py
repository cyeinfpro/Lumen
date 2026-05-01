from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import Request
from sqlalchemy.dialects import postgresql

from app.routes import conversations, shares
from lumen_core.models import AuditLog, Share


class _ScalarResult:
    def __init__(self, value):
        self.value = value
        self.rowcount = 0

    def scalar_one_or_none(self):
        return self.value

    def first(self):
        return self.value

    def scalars(self):
        return self

    def all(self):
        if self.value is None:
            return []
        if isinstance(self.value, list):
            return self.value
        return [self.value]


class _Db:
    def __init__(self, result):
        self.result = result
        self.added = []
        self.committed = False
        self.flushed = 0
        self.statements = []
        self.last_statement = None

    async def execute(self, stmt):
        self.statements.append(stmt)
        self.last_statement = stmt
        if isinstance(self.result, list):
            value = self.result.pop(0) if self.result else None
            return _ScalarResult(value)
        return _ScalarResult(self.result)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed += 1
        for value in self.added:
            if isinstance(value, Share):
                if value.id is None:
                    value.id = "share-1"
                if getattr(value, "created_at", None) is None:
                    value.created_at = datetime.now(timezone.utc)

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        if isinstance(value, Share) and getattr(value, "created_at", None) is None:
            value.created_at = datetime.now(timezone.utc)


def _request(method: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_public_share_select_does_not_take_write_lock() -> None:
    stmt = shares._select_public_share("token-1", datetime.now(timezone.utc))
    sql = str(stmt.compile(dialect=postgresql.dialect())).upper()

    assert "FOR UPDATE" not in sql


def test_share_image_ids_dedupes_and_falls_back_to_single_image() -> None:
    share = SimpleNamespace(
        image_id="img-1",
        image_ids=["img-2", "img-2", "", None, " img-3 "],
    )

    assert shares._share_image_ids(share) == ["img-2", "img-3"]
    assert shares._share_image_ids(SimpleNamespace(image_id="img-1", image_ids=[])) == [
        "img-1"
    ]


@pytest.mark.asyncio
async def test_create_share_writes_audit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_write_audit(db, **kwargs):
        db.add(AuditLog(**kwargs))
        await db.flush()

    monkeypatch.setattr(shares, "write_audit", fake_write_audit)
    expires_at = datetime(2026, 4, 26, tzinfo=timezone.utc)
    db = _Db([SimpleNamespace(id="img-1"), None])

    out = await shares.create_share(
        "img-1",
        shares._CreateShareIn(show_prompt=True, expires_at=expires_at),
        _request("POST"),
        SimpleNamespace(id="user-1", email="user@example.com"),
        db,  # type: ignore[arg-type]
    )

    audits = [row for row in db.added if isinstance(row, AuditLog)]
    assert db.committed is True
    assert db.flushed == 2
    assert out.image_id == "img-1"
    assert out.image_ids == ["img-1"]
    assert len(audits) == 1
    assert audits[0].event_type == "share.create"
    assert audits[0].details["share_id"] == "share-1"
    assert audits[0].details["image_ids"] == ["img-1"]
    assert audits[0].details["image_count"] == 1
    assert audits[0].details["show_prompt"] is True


@pytest.mark.asyncio
async def test_create_share_uses_default_expiration_days_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_write_audit(db, **kwargs):
        db.add(AuditLog(**kwargs))
        await db.flush()

    monkeypatch.setattr(shares, "write_audit", fake_write_audit)
    before = datetime.now(timezone.utc)
    db = _Db([SimpleNamespace(id="img-1"), "3"])

    out = await shares.create_share(
        "img-1",
        shares._CreateShareIn(show_prompt=False),
        _request("POST"),
        SimpleNamespace(id="user-1", email="user@example.com"),
        db,  # type: ignore[arg-type]
    )

    after = datetime.now(timezone.utc)
    assert out.expires_at is not None
    assert before + timedelta(days=3) <= out.expires_at <= after + timedelta(days=3)
    audits = [row for row in db.added if isinstance(row, AuditLog)]
    assert audits[0].details["expires_at"] == out.expires_at.isoformat()


@pytest.mark.asyncio
async def test_revoke_share_writes_audit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_write_audit(db, **kwargs):
        db.add(AuditLog(**kwargs))

    monkeypatch.setattr(shares, "write_audit", fake_write_audit)
    share = SimpleNamespace(id="share-1", image_id="img-1", revoked_at=None)
    db = _Db(share)

    await shares.revoke_share(
        "share-1",
        _request("DELETE"),
        SimpleNamespace(id="user-1", email="user@example.com"),
        db,  # type: ignore[arg-type]
    )

    audits = [row for row in db.added if isinstance(row, AuditLog)]
    assert share.revoked_at is not None
    assert db.committed is True
    assert len(audits) == 1
    assert audits[0].event_type == "share.revoke"
    assert audits[0].details == {
        "share_id": "share-1",
        "image_id": "img-1",
        "image_ids": ["img-1"],
    }


@pytest.mark.asyncio
async def test_create_multi_image_share_preserves_image_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_write_audit(db, **kwargs):
        db.add(AuditLog(**kwargs))
        await db.flush()

    monkeypatch.setattr(shares, "write_audit", fake_write_audit)
    expires_at = datetime(2026, 4, 26, tzinfo=timezone.utc)
    db = _Db([
        [SimpleNamespace(id="img-2"), SimpleNamespace(id="img-1")],
        None,
    ])

    out = await shares.create_multi_image_share(
        shares._CreateMultiShareIn(
            image_ids=["img-2", "img-1"],
            show_prompt=False,
            expires_at=expires_at,
        ),
        _request("POST"),
        SimpleNamespace(id="user-1", email="user@example.com"),
        db,  # type: ignore[arg-type]
    )

    audits = [row for row in db.added if isinstance(row, AuditLog)]
    assert out.image_id == "img-2"
    assert out.image_ids == ["img-2", "img-1"]
    assert audits[0].details["image_ids"] == ["img-2", "img-1"]
    assert audits[0].details["image_count"] == 2


@pytest.mark.asyncio
async def test_delete_conversation_writes_audit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_write_audit(db, **kwargs):
        db.add(AuditLog(**kwargs))

    monkeypatch.setattr(conversations, "write_audit", fake_write_audit)
    conv = SimpleNamespace(id="conv-1", deleted_at=None)
    db = _Db(conv)

    result = await conversations.delete_conversation(
        "conv-1",
        _request("DELETE"),
        SimpleNamespace(id="user-1", email="user@example.com"),
        db,  # type: ignore[arg-type]
    )

    audits = [row for row in db.added if isinstance(row, AuditLog)]
    assert result == {"ok": True}
    assert conv.deleted_at is not None
    assert db.committed is True
    assert len(audits) == 1
    assert audits[0].event_type == "conversation.delete"
    assert audits[0].details == {
        "conversation_id": "conv-1",
        "generations_canceled": 0,
        "images_deleted": 0,
    }


@pytest.mark.asyncio
async def test_public_share_prompt_query_is_scoped_to_image_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(shares.PUBLIC_PREVIEW_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(shares, "get_redis", lambda: object())

    share = SimpleNamespace(
        token="token-1",
        show_prompt=True,
        created_at=datetime.now(timezone.utc),
        expires_at=None,
    )
    img = SimpleNamespace(
        id="img-1",
        user_id="user-1",
        source="generated",
        owner_generation_id="gen-1",
        width=100,
        height=100,
        mime="image/png",
    )
    db = _Db([(share, img), [], "safe prompt"])

    await shares.get_public_share(
        "token-1",
        _request("GET"),
        db,  # type: ignore[arg-type]
    )

    rendered = str(db.last_statement)
    assert "generations.user_id" in rendered
    assert "images.id" in rendered


@pytest.mark.asyncio
async def test_public_multi_share_returns_ordered_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(shares.PUBLIC_PREVIEW_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(shares, "get_redis", lambda: object())

    share = SimpleNamespace(
        token="token-1",
        image_id="img-2",
        image_ids=["img-2", "img-1"],
        show_prompt=False,
        created_at=datetime.now(timezone.utc),
        expires_at=None,
    )
    img2 = SimpleNamespace(
        id="img-2",
        user_id="user-1",
        source="generated",
        owner_generation_id="gen-2",
        width=200,
        height=100,
        mime="image/png",
    )
    img1 = SimpleNamespace(
        id="img-1",
        user_id="user-1",
        source="generated",
        owner_generation_id="gen-1",
        width=100,
        height=100,
        mime="image/png",
    )
    db = _Db(
        [
            (share, img2),
            [img1, img2],
            [
                ("img-2", "preview1024"),
                ("img-2", "display2048"),
                ("img-1", "thumb256"),
            ],
        ]
    )

    out = await shares.get_public_share(
        "token-1",
        _request("GET"),
        db,  # type: ignore[arg-type]
    )

    assert [item.id for item in out.images] == ["img-2", "img-1"]
    assert out.images[0].image_url == "/api/share/token-1/images/img-2"
    assert (
        out.images[0].display_url
        == "/api/share/token-1/images/img-2/variants/display2048"
    )
    assert (
        out.images[0].preview_url
        == "/api/share/token-1/images/img-2/variants/preview1024"
    )
    assert (
        out.images[1].thumb_url
        == "/api/share/token-1/images/img-1/variants/thumb256"
    )


@pytest.mark.asyncio
async def test_public_share_metadata_skips_missing_variant_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shares, "_storage_key_exists", lambda key: key == "ok.webp")
    db = _Db(
        [
            [
                ("img-1", "preview1024", "missing.webp"),
                ("img-1", "thumb256", "ok.webp"),
            ]
        ]
    )

    out = await shares._variant_kinds_for_images(
        db,  # type: ignore[arg-type]
        [SimpleNamespace(id="img-1")],
    )

    assert out == {"img-1": {"thumb256"}}


@pytest.mark.asyncio
async def test_public_share_image_by_id_is_scoped_to_share_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(shares.PUBLIC_IMAGE_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(shares, "get_redis", lambda: object())
    monkeypatch.setattr(
        shares,
        "_open_storage_file_safe",
        lambda _storage_key: (BytesIO(b"ok"), 2),
    )

    share = SimpleNamespace(
        token="token-1",
        image_id="img-1",
        image_ids=["img-1", "img-2"],
    )
    primary = SimpleNamespace(
        id="img-1",
        user_id="user-1",
        storage_key="u/user-1/one.png",
        mime="image/png",
    )
    target = SimpleNamespace(
        id="img-2",
        user_id="user-1",
        storage_key="u/user-1/two.png",
        mime="image/png",
        sha256="hash-two",
    )
    db = _Db([(share, primary), target])

    await shares.get_public_share_image_by_id(
        "token-1",
        "img-2",
        _request("GET"),
        db,  # type: ignore[arg-type]
    )

    rendered = str(db.last_statement)
    assert "images.user_id" in rendered


@pytest.mark.asyncio
async def test_public_share_variant_by_id_is_scoped_to_share_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rate_limit(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(shares.PUBLIC_IMAGE_LIMITER, "check", no_rate_limit)
    monkeypatch.setattr(shares, "get_redis", lambda: object())
    monkeypatch.setattr(
        shares,
        "_open_storage_file_safe",
        lambda _storage_key: (BytesIO(b"ok"), 2),
    )

    share = SimpleNamespace(
        token="token-1",
        image_id="img-1",
        image_ids=["img-1", "img-2"],
    )
    primary = SimpleNamespace(
        id="img-1",
        user_id="user-1",
        storage_key="u/user-1/one.png",
        mime="image/png",
    )
    variant = SimpleNamespace(
        image_id="img-2",
        kind="preview1024",
        storage_key="u/user-1/two.preview1024.webp",
    )
    db = _Db([(share, primary), variant])

    await shares.get_public_share_image_variant_by_id(
        "token-1",
        "img-2",
        "preview1024",
        _request("GET"),
        db,  # type: ignore[arg-type]
    )

    rendered = str(db.last_statement)
    assert "image_variants" in rendered
    assert "images.user_id" in rendered
