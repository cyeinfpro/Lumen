"""Offline worker hot-reload checks: no genuine key or provider request."""

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_config_activates_and_rotates_without_worker_restart(monkeypatch, tmp_path):
    from app import db, main, voice_pipeline
    from lumen_core import aliyun_speech, voice_config

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    class Redis:
        def __init__(self):
            self.writes = []

        async def set(self, key, value, *, ex):
            self.writes.append((key, value, ex))

    snapshots = {"current": None}
    clients = []
    swept = []

    async def load_config(session, *, include_key, master_secret):
        assert isinstance(session, Session)
        assert include_key and master_secret == "unit-test-secret"
        return snapshots["current"]

    class Client:
        def __init__(self, region, workspace, key):
            clients.append((region, workspace, key))
            self.key = key

    class Pipeline:
        def __init__(self, _, client_for, media_root, *, settle, binding):
            self.client_for = client_for
            self.binding = binding
            assert media_root.is_dir()
            assert callable(settle)

        async def sweep(self):
            region, workspace_id = self.binding
            voice = SimpleNamespace(region=region, workspace_id=workspace_id, model=main._VOICE_MODEL)
            swept.append(self.client_for(voice).key)

    monkeypatch.setattr(db, "SessionLocal", Session)
    monkeypatch.setattr(voice_config, "load_voice_provider_config", load_config)
    monkeypatch.setattr(aliyun_speech, "AliyunSpeechClient", Client)
    monkeypatch.setattr(voice_pipeline, "VoicePipeline", Pipeline)
    monkeypatch.setattr(main.settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(main.settings, "byok_api_key_master_secret", "unit-test-secret")
    for flag in ("ENABLED", "REGION", "ALLOWED_USER_IDS", "WORKSPACE_ID", "API_KEY"):
        monkeypatch.delenv("LUMEN_VOICE_" + flag, raising=False)

    redis = Redis()
    ctx = {"redis": redis, "voice_sweep_lock": asyncio.Lock()}
    await main.process_voice_work(ctx)
    assert not redis.writes and not clients

    for revision in ("first", "second"):
        snapshots["current"] = SimpleNamespace(
            region="cn-beijing", workspace_id="", api_key=f"fake-{revision}",
            ready_marker=f"cn-beijing::{revision}",
        )
        await main.process_voice_work(ctx)

    # A repeated refresh with the same revision can reuse the client.
    await main.process_voice_work(ctx)
    assert clients == [("cn-beijing", "", "fake-first"), ("cn-beijing", "", "fake-second")]
    assert swept == ["fake-first", "fake-second", "fake-second"]
    assert redis.writes[-1] == (
        voice_config.VOICE_WORKER_READY_KEY, "cn-beijing::second", main._VOICE_WORKER_READY_TTL_SECONDS,
    )

    snapshots["current"] = None
    await main.process_voice_work(ctx)
    assert "voice_pipeline" not in ctx and "voice_ready_binding" not in ctx
    assert len(redis.writes) == 3 and len(swept) == 3
