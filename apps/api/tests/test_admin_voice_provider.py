"""Admin setup and private voice settings tests; no provider or production I/O."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.deps import AdminUser, verify_csrf
from app.routes import admin_voice_provider as routes
from lumen_core import voice_config as voice

_FAKE_KEY = "not-a-real-test-key-987654"
_SECOND_KEY = "other-fake-key-987654"
_SECRET = "testing-only-not-a-real-voice-master-987654"


class AsyncDB:
    def __init__(self, db):
        self.db = db

    def get_bind(self):
        return self.db.get_bind()

    async def execute(self, *args, **kwargs):
        return self.db.execute(*args, **kwargs)

    async def scalar(self, *args, **kwargs):
        return self.db.scalar(*args, **kwargs)

    async def commit(self):
        self.db.commit()


def test_configure_rotate_and_disconnect_with_private_ciphertext():
    async def flow(db):
        assert await voice.load_voice_provider_config(db) is None
        assert (await voice.get_voice_provider_state(db)).region == "cn-beijing"
        saved = await voice.change_voice_provider_config(db, master_secret=_SECRET, api_key=_FAKE_KEY)
        await db.commit()
        assert saved.configured and saved.workspace_id == ""
        assert saved.api_key is None
        assert _FAKE_KEY not in str(db.db.scalar(select(voice.SystemSetting.value)))
        assert (await voice.load_voice_provider_config(db)).api_key is None
        loaded = await voice.load_voice_provider_config(db, include_key=True, master_secret=_SECRET)
        assert loaded.api_key == _FAKE_KEY and _FAKE_KEY not in repr(loaded)
        assert loaded.ready_marker == saved.ready_marker
        with pytest.raises(voice.VoiceProviderConfigurationError, match="KEY_REQUIRED"):
            await voice.change_voice_provider_config(db, region="ap-southeast-1")
        db.db.rollback()
        same = await voice.change_voice_provider_config(db, region="cn-beijing")
        assert same.revision == saved.revision
        changed = await voice.change_voice_provider_config(
            db, api_key=_SECOND_KEY, master_secret=_SECRET,
            region="ap-southeast-1", workspace_id="LLM-Test",
        )
        await db.commit()
        assert changed.workspace_id == "llm-test"
        assert changed.ready_marker != saved.ready_marker
        decoded = await voice.load_voice_provider_config(db, include_key=True, master_secret=_SECRET)
        assert decoded.api_key == _SECOND_KEY
        with pytest.raises(voice.VoiceProviderConfigurationError, match="CRYPTO_UNAVAILABLE"):
            await voice.load_voice_provider_config(db, include_key=True, master_secret="x" * 33)
        disabled = await voice.change_voice_provider_config(db, remove_key=True)
        await db.commit()
        assert not disabled.configured and disabled.workspace_id == "llm-test"
        assert await voice.load_voice_provider_config(db, include_key=True, master_secret=_SECRET) is None
        assert json.loads(db.db.scalar(select(voice.SystemSetting.value)))["ciphertext"] is None

    engine = create_engine("sqlite://")
    voice.SystemSetting.__table__.create(engine)
    with Session(engine) as db:
        asyncio.run(flow(AsyncDB(db)))
    engine.dispose()


def test_api_denies_nonadmin_and_csrf_then_rejects_secret_safely(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    admin_dep = AdminUser.__metadata__[0].dependency
    app.dependency_overrides[routes.get_db] = lambda: None

    def reject_admin():
        raise HTTPException(403, "admin required")

    def reject_csrf():
        raise HTTPException(403, "bad csrf")

    app.dependency_overrides[admin_dep] = reject_admin
    app.dependency_overrides[verify_csrf] = lambda: None
    with TestClient(app) as client:
        assert client.get("/admin/voice-provider").status_code == 403
        assert client.put("/admin/voice-provider", json={"api_key": _FAKE_KEY}).status_code == 403
        app.dependency_overrides[admin_dep] = lambda: SimpleNamespace(id="admin", email="admin.invalid")
        app.dependency_overrides[verify_csrf] = reject_csrf
        assert client.delete("/admin/voice-provider").status_code == 403
        assert client.put("/admin/voice-provider", json={"api_key": _FAKE_KEY}).status_code == 403
        app.dependency_overrides[verify_csrf] = lambda: None
        bad = client.put("/admin/voice-provider", json={"api_key": [_FAKE_KEY]})
        assert bad.status_code == 422 and _FAKE_KEY not in bad.text
        too_long = client.put("/admin/voice-provider", json={"api_key": _FAKE_KEY * 30})
        assert too_long.status_code == 422 and _FAKE_KEY not in too_long.text


def test_api_only_exposes_current_worker_revision_and_never_audits_key(monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[AdminUser.__metadata__[0].dependency] = lambda: SimpleNamespace(id="admin", email="admin.invalid")
    app.dependency_overrides[verify_csrf] = lambda: None
    state = voice.VoiceProviderConfig("cn-beijing", "", "a" * 32, True, _FAKE_KEY)
    latest = [state]
    committed, audit, captures = [], [], []
    marker = [b"stale"]

    class DBStub:
        async def commit(self):
            committed.append(True)

    class RedisStub:
        async def get(self, key):
            assert key == voice.VOICE_WORKER_READY_KEY
            return marker[0]

    class PoolStub:
        async def enqueue_job(self, name):
            assert committed and name == "process_voice_work"

    async def get_pool():
        return PoolStub()

    async def get_state(db):
        return latest[0]

    async def change(db, **kwargs):
        captures.append(kwargs)
        if kwargs.get("remove_key"):
            latest[0] = voice.VoiceProviderConfig("cn-beijing", "", "b" * 32, False)
        return latest[0]

    async def log(db, **kwargs):
        audit.append(kwargs)

    app.dependency_overrides[routes.get_db] = DBStub
    monkeypatch.setattr(routes, "get_arq_pool", get_pool)
    monkeypatch.setattr(routes, "get_redis", RedisStub)
    monkeypatch.setattr(routes, "get_voice_provider_state", get_state)
    monkeypatch.setattr(routes, "change_voice_provider_config", change)
    monkeypatch.setattr(routes, "write_audit", log)
    with TestClient(app) as client:
        endpoint = "/admin/voice-provider"
        saved = client.put(endpoint, json={"api_key": _FAKE_KEY})
        assert saved.status_code == 200 and saved.json()["configured"] and not saved.json()["ready"]
        assert _FAKE_KEY not in saved.text and saved.headers["cache-control"] == "no-store"
        assert captures[0]["api_key"] == _FAKE_KEY and _FAKE_KEY not in repr(audit)
        assert audit[0]["autocommit"] is False
        marker[0] = state.ready_marker.encode("ascii")
        assert client.get(endpoint).json()["ready"]
        removed = client.delete(endpoint).json()
        assert not removed["configured"] and not removed["ready"]
