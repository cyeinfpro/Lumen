"""HTTP contract checks for the private speech surface (no upstream)."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_current_user, verify_csrf
from app.routes import voice

_OWNER = "00000000-0000-4000-8000-000000000001"


def _client(monkeypatch):
    state = {
        "config": SimpleNamespace(region="cn-beijing", workspace_id="", ready_marker="cn-beijing::rev-a"),
        "marker": b"cn-beijing::rev-a",
    }

    async def load_config(db):
        return state["config"]

    class Redis:
        async def get(self, key):
            assert key == voice.VOICE_WORKER_READY_KEY
            return state["marker"]

    monkeypatch.setattr(voice, "load_voice_provider_config", load_config)
    monkeypatch.setattr(voice, "get_redis", Redis)
    app = FastAPI()
    app.state.provider_state = state
    app.include_router(voice.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=_OWNER)
    app.dependency_overrides[voice.get_db] = lambda: None
    app.dependency_overrides[verify_csrf] = lambda: None
    return TestClient(app)


def test_config_only_activation_and_same_binding_rotation(monkeypatch):
    # Old env variables cannot opt in or opt out; active DB config is authoritative.
    for setting in ("ENABLED", "REGION", "WORKSPACE_ID", "ALLOWED_USER_IDS", "API_KEY"):
        monkeypatch.delenv(f"LUMEN_VOICE_{setting}", raising=False)
    with _client(monkeypatch) as client:
        state = client.app.state.provider_state
        active = state["config"]
        state["config"] = None
        assert not client.get("/speech/capabilities").json()["enabled"]
        assert client.post("/speech/quote", json={"text": "你好"}).status_code == 404

        state["config"] = active
        enabled = client.get("/speech/capabilities").json()
        assert enabled["enabled"] and enabled["ready"]
        assert enabled["price_micro_per_character"] == 140

        state["config"] = SimpleNamespace(
            region="cn-beijing", workspace_id="", ready_marker="cn-beijing::rev-b",
        )
        stale = client.get("/speech/capabilities").json()
        assert stale["enabled"] and not stale["ready"]
        state["marker"] = "cn-beijing::rev-b"
        assert client.get("/speech/capabilities").json()["ready"]



def test_quote_counts_han_twice(monkeypatch):
    with _client(monkeypatch) as client:
        response = client.post("/speech/quote", json={"text": "你好，A1"})
        assert response.status_code == 200
        assert response.json() == {"characters": 7, "estimated_micro": 980, "currency": "CNY"}


def test_paid_creation_requires_idempotency_header(monkeypatch):
    with _client(monkeypatch) as client:
        response = client.post("/speech/jobs", json={"voice_id": "voice", "text": "你好"})
        assert response.status_code == 422
        assert any(item.get("loc") == ["header", "Idempotency-Key"] for item in response.json()["detail"])


def test_library_reads_only_owned_latest_artifacts_in_three_statements(monkeypatch):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    other = "00000000-0000-4000-8000-000000000002"
    stamp = datetime.now(timezone.utc)
    engine = create_engine("sqlite://")
    for model in (voice.VoiceProfile, voice.SpeechJob, voice.SpeechSegment):
        model.__table__.create(engine)

    def profile(id_, owner, *, revoked=False):
        return voice.VoiceProfile(
            id=id_, user_id=owner, name=id_, state="ready",
            consent_kind="self", consent_at=stamp, client_key=id_, request_hash="voice",
            region="cn-beijing", workspace_id="llm-test", model=voice.MODEL,
            provider_prefix=id_, draft_expires_at=stamp + timedelta(days=1),
            revoked_at=stamp if revoked else None, created_at=stamp,
        )

    def job(id_, owner, voice_id, created, *, kind="preview", artifact=True):
        return voice.SpeechJob(
            id=id_, user_id=owner, voice_id=voice_id, client_key=id_, request_hash="job",
            kind=kind, state="ready" if artifact else "queued",
            artifact_key=f"{id_}.wav" if artifact else None, created_at=created,
        )

    with Session(engine) as session:
        session.add_all([profile("one", _OWNER), profile("two", _OWNER),
                         profile("three", other), profile("four", _OWNER, revoked=True)])
        session.flush()
        session.add_all([
            job("prev-old", _OWNER, "one", stamp - timedelta(days=1)),
            job("prev-ready", _OWNER, "one", stamp - timedelta(hours=1)),
            job("prev-pending", _OWNER, "one", stamp, artifact=False),
            job("elsewhere", other, "three", stamp),
            job("speech-one", _OWNER, "one", stamp, kind="speech"),
        ])
        session.flush()
        session.add_all([
            voice.SpeechSegment(job_id="speech-one", segment_no=0, revision=1, state="failed", body="obsolete"),
            voice.SpeechSegment(job_id="speech-one", segment_no=0, revision=2, state="ready", body="你好"),
            voice.SpeechSegment(job_id="speech-one", segment_no=1, revision=1, state="ready", body="A"),
            voice.SpeechSegment(job_id="elsewhere", segment_no=0, revision=5, state="ready", body="private"),
        ])
        session.commit()

        class CountedDB:
            calls = 0

            async def execute(self, statement):
                self.calls += 1
                return session.execute(statement)

            async def scalars(self, statement):
                return (await self.execute(statement)).scalars()

        db = CountedDB()
        owner = SimpleNamespace(id=_OWNER)
        voices = asyncio.run(voice.list_voices(owner, db))
        assert db.calls == 1
        assert {row["id"]: row["preview_url"] for row in voices["items"]} == {
            "one": "/api/speech/jobs/prev-ready/audio", "two": None,
        }

        jobs = asyncio.run(voice.jobs(owner, db))
        assert db.calls == 3
        assert {row["id"] for row in jobs["items"]} == {"prev-old", "prev-ready", "prev-pending", "speech-one"}
        speech = next(row for row in jobs["items"] if row["id"] == "speech-one")
        assert speech["segments"] == [
            {"segment_no": 0, "revision": 2, "state": "ready"},
            {"segment_no": 1, "revision": 1, "state": "ready"},
        ]
        assert speech["estimated_characters"] == 5
    engine.dispose()


def test_retry_holds_job_before_voice_like_worker(monkeypatch):
    import asyncio

    calls = []
    row = SimpleNamespace(id="job", voice_id="voice", params={"retry_keys": {"request-1": 0}})

    async def lock_user(*_):
        calls.append("user")

    async def job(*_, lock=False):
        assert lock
        calls.append("job")
        return row

    async def profile(*_, lock=False):
        assert lock
        calls.append("voice")
        return None

    async def status(*_):
        calls.append("status")
        return {"id": "job"}

    monkeypatch.setattr(voice, "_need", lambda user: None)
    monkeypatch.setattr(voice, "_lock_user", lock_user)
    monkeypatch.setattr(voice, "_job", job)
    monkeypatch.setattr(voice, "_voice", profile)
    monkeypatch.setattr(voice, "_job_out", status)
    assert asyncio.run(voice.retry("job", 0, None, None, "request-1")) == {"id": "job"}
    assert calls == ["user", "job", "voice", "status"]
