"""Focused offline checks for private voice domain and durable worker boundaries."""

from __future__ import annotations

import importlib.util
import io
import sys
import wave
from pathlib import Path

import pytest
import pytest_asyncio

from lumen_core.voice_domain import billable_chars, estimate_micro, private_path, split_text

MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "voice_pipeline.py"
SPEC = importlib.util.spec_from_file_location("voice_pipeline_under_test", MODULE_PATH)
pipeline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pipeline
SPEC.loader.exec_module(pipeline)


def test_billing_uses_han_weight_without_repricing_output():
    assert billable_chars("中文A123。") == 9
    assert estimate_micro(6_000, "cn-beijing") == 840_000
    assert estimate_micro(6_000, "ap-southeast-1") == 1_314_000
    assert estimate_micro(0, "cn-beijing") == 0


def test_segment_partition_preserves_text_and_ascii_run():
    original = "拍摄开始了。随后使用 AI-360.4 完成同步！结束。"
    segments = split_text(original, 18)
    assert "".join(segments) == original
    assert all(0 < billable_chars(part) <= 18 for part in segments)
    assert any("AI-360.4" in part for part in segments)


def test_rejects_unbreakable_input():
    with pytest.raises(ValueError, match="unbreakable"):
        split_text("W" * 500)


def test_private_audio_path_cannot_escape_root(tmp_path):
    with pytest.raises(ValueError):
        private_path(tmp_path, "samples/../ref.wav")
    outside = tmp_path.parent
    (tmp_path / "outputs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        private_path(tmp_path, "outputs/job/file.wav")


def _wav(data: bytes) -> bytes:
    result = io.BytesIO()
    with wave.open(result, "wb") as file:
        file.setparams((1, 2, 24_000, 0, "NONE", "not compressed"))
        file.writeframes(data)
    return result.getvalue()


def test_wav_assembly_preserves_frame_order_and_revalidates(tmp_path):
    (tmp_path / "outputs" / "job").mkdir(parents=True)
    keys = ["outputs/job/a.wav", "outputs/job/b.wav"]
    expected = [bytes.fromhex("02030203"), bytes.fromhex("0a0b")]
    for key, data in zip(keys, expected, strict=True):
        private_path(tmp_path, key).write_bytes(_wav(data))
    assert pipeline._wav_pcm(pipeline.join_wav(tmp_path, keys)) == b"".join(expected)
    private_path(tmp_path, keys[0]).write_bytes(b"RIFF")
    with pytest.raises((EOFError, wave.Error, ValueError)):
        pipeline.join_wav(tmp_path, keys)


@pytest.mark.asyncio
async def test_paid_dispatch_fails_closed_without_settlement(tmp_path):
    runner = pipeline.VoicePipeline(None, None, tmp_path)
    assert await runner.synthesize("owner", "id") is None



@pytest_asyncio.fixture
async def speech_db():
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime, timedelta
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from lumen_core.models import Base, User
    from lumen_core.voice_models import SpeechJob, SpeechSegment, VoiceProfile

    @asynccontextmanager
    async def fixture():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[
                    User.__table__, VoiceProfile.__table__, SpeechJob.__table__, SpeechSegment.__table__,
                ]))
            factory = async_sessionmaker(engine, expire_on_commit=False)
            now = datetime.now(UTC)
            async with factory() as session, session.begin():
                session.add(User(id="owner", email="owner@example.invalid"))
                voice = VoiceProfile(
                    id="voice", user_id="owner", name="Fixture", consent_kind="self",
                    consent_at=now, client_key="v", request_hash="v", region="cn-beijing",
                    workspace_id="test", model=pipeline.MODEL, provider_prefix="test",
                    provider_voice_id="remote", saved_at=now, state="ready",
                    draft_expires_at=now + timedelta(hours=24), due_at=now,
                )
                session.add(voice)
                await session.flush()
                job = SpeechJob(
                    id="job", user_id="owner", voice_id="voice", kind="speech", client_key="j",
                    request_hash="j", params={}, state="queued", due_at=now,
                )
                session.add(job)
                await session.flush()
                session.add_all([
                    SpeechSegment(
                        id=f"s{n}", job_id="job", segment_no=n, revision=1, body=part,
                    )
                    for n, part in enumerate(("甲。", "乙。"))
                ])
            yield factory
        finally:
            await engine.dispose()

    return fixture


class StubProvider:
    def __init__(self):
        self.sent = []
        self.downloaded = []
        self.deleted = []

    async def synthesize(self, text, voice_id, **_):
        self.sent.append((text, voice_id))
        return pipeline.SpeechReceipt(audio_url="stub", characters=3, request_id=f"r{len(self.sent)}")

    async def download(self, receipt):
        self.downloaded.append(receipt.request_id)
        return _wav(b"\x00\x01")

    async def delete_voice(self, voice_id):
        self.deleted.append(voice_id)


@pytest.mark.asyncio
async def test_worker_runs_mock_tts_then_downloads_and_joins(tmp_path, speech_db):
    provider = StubProvider()
    settled = []

    async def settle(_session, _job, segment, action):
        settled.append((segment.id, action, segment.usage_chars))

    async with speech_db() as factory:
        worker = pipeline.VoicePipeline(factory, lambda _: provider, tmp_path, settle)
        await worker.synthesize("someone-else", "job")
        assert provider.sent == []
        await worker.synthesize("owner", "job")
        await worker.synthesize("owner", "job")
        async with factory() as session:
            job = await session.get(pipeline.SpeechJob, "job")
            assert job.state == "ready"
            assert job.usage_chars == 6
            assert pipeline._wav_pcm(private_path(tmp_path, job.artifact_key).read_bytes()) == b"\x00\x01" * 2
    assert provider.sent == [("甲。", "remote"), ("乙。", "remote")]
    assert provider.downloaded == ["r1", "r2"]
    assert settled == [("s0", "settle", 3), ("s1", "settle", 3)]


@pytest.mark.asyncio
async def test_interrupted_send_never_replays_and_releases_only_unsent(tmp_path, speech_db):
    provider = StubProvider()
    settled = []

    async def settle(_session, _job, segment, action):
        settled.append((segment.id, action))

    async with speech_db() as factory:
        async with factory() as session, session.begin():
            row = await session.get(pipeline.SpeechSegment, "s0")
            row.state = "sending"
        worker = pipeline.VoicePipeline(factory, lambda _: provider, tmp_path, settle)
        await worker.synthesize("owner", "job")
        async with factory() as session:
            assert (await session.get(pipeline.SpeechSegment, "s0")).state == "unknown"
            assert (await session.get(pipeline.SpeechJob, "job")).state == "unknown"
    assert not provider.sent
    assert settled == [("s1", "release")]


@pytest.mark.asyncio
async def test_expired_draft_reclaims_remote_voice_and_blocks_queued_job(tmp_path, speech_db):
    provider = StubProvider()
    settled = []

    async def settle(_session, _job, segment, action):
        settled.append((segment.id, action))

    async with speech_db() as factory:
        async with factory() as session, session.begin():
            row = await session.get(pipeline.VoiceProfile, "voice")
            row.saved_at = None
            row.draft_expires_at = pipeline._now() - pipeline.timedelta(seconds=1)
        worker = pipeline.VoicePipeline(factory, lambda _: provider, tmp_path, settle)
        await worker.sweep()
        async with factory() as session:
            assert (await session.get(pipeline.VoiceProfile, "voice")).state == "revoked"
            assert (await session.get(pipeline.SpeechJob, "job")).state == "failed"
    assert provider.deleted == ["remote"] and not provider.sent
    assert settled == [("s0", "release"), ("s1", "release")]



@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [False, True])
async def test_ineligible_owner_has_no_new_paid_dispatch(tmp_path, speech_db, admitted):
    from lumen_core.models import User

    provider = StubProvider()
    settled = []

    async def settle(_session, _job, segment, action):
        settled.append((segment.id, action))

    async with speech_db() as factory:
        if admitted:
            async with factory() as session, session.begin():
                (await session.get(User, "owner")).deleted_at = pipeline._now()
        worker = pipeline.VoicePipeline(
            factory, lambda _: provider, tmp_path, settle, owner_allowed=lambda _: admitted,
        )
        await worker.synthesize("owner", "job")
        async with factory() as session:
            assert (await session.get(pipeline.SpeechJob, "job")).state == "failed"
    assert not provider.sent
    assert settled == [("s0", "release"), ("s1", "release")]


def test_reference_crop_validation_accepts_16khz_three_seconds(tmp_path):
    key = "samples/clip.wav"
    path = private_path(tmp_path, key)
    path.parent.mkdir()
    with wave.open(str(path), "wb") as file:
        file.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        file.writeframes(b"\x00\x01" * 48_000)
    assert pipeline._read_sample(tmp_path, key) == path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked_before_resume", [False, True])
async def test_known_receipt_recovers_wallet_rollback_without_resending(
    tmp_path, speech_db, revoked_before_resume,
):
    provider = StubProvider()
    payments = []
    fail_next = True

    async def settle(_session, _job, segment, action):
        nonlocal fail_next
        if fail_next:
            fail_next = False
            assert action == "settle"
            raise RuntimeError("simulated wallet transaction failure")
        payments.append((segment.id, action, segment.usage_chars))

    async with speech_db() as factory:
        worker = pipeline.VoicePipeline(factory, lambda _: provider, tmp_path, settle)
        with pytest.raises(RuntimeError, match="simulated wallet"):
            await worker.synthesize("owner", "job")
        assert provider.sent == [("甲。", "remote")]
        assert provider.downloaded == []
        async with factory() as session, session.begin():
            job = await session.get(pipeline.SpeechJob, "job")
            first = await session.get(pipeline.SpeechSegment, "s0")
            assert (first.state, first.provider_request_id, first.usage_chars) == ("sending", "r1", 3)
            assert job.usage_chars == 0
            job.lease_until = pipeline._now() - pipeline.timedelta(seconds=1)
            if revoked_before_resume:
                voice = await session.get(pipeline.VoiceProfile, "voice")
                voice.revoked_at = pipeline._now()
                voice.state = "revoking"

        await worker.synthesize("owner", "job")
        await worker.synthesize("owner", "job")
        async with factory() as session:
            job = await session.get(pipeline.SpeechJob, "job")
            first = await session.get(pipeline.SpeechSegment, "s0")
            assert first.provider_request_id == "r1"
            assert first.result_url is None
            if revoked_before_resume:
                assert (job.state, job.usage_chars, first.state) == ("failed", 3, "failed")
            else:
                assert (job.state, job.usage_chars, first.state) == ("ready", 6, "ready")

    if revoked_before_resume:
        assert provider.sent == [("甲。", "remote")]
        assert provider.downloaded == []
        assert payments == [("s0", "settle", 3), ("s1", "release", 0)]
    else:
        assert provider.sent == [("甲。", "remote"), ("乙。", "remote")]
        assert provider.downloaded == ["r1", "r2"]
        assert payments == [("s0", "settle", 3), ("s1", "settle", 3)]


def test_single_segment_assembly_keeps_metadata_and_rejects_damage(tmp_path):
    keys = ["outputs/job/original.wav", "outputs/job/final.wav"]
    source, target = [private_path(tmp_path, key) for key in keys]
    source.parent.mkdir(parents=True)
    # Append a valid RIFF private chunk to prove exact container bytes survive.
    original = _wav(b"\x00\x01") + b"JUNK\x04\x00\x00\x00tag!"
    original = original[:4] + (len(original) - 8).to_bytes(4, "little") + original[8:]
    source.write_bytes(original)
    pipeline._assemble(tmp_path, keys[:1], keys[1])
    assert target.read_bytes() == original
    source.write_bytes(original[:40])
    with pytest.raises((EOFError, wave.Error, ValueError)):
        pipeline._assemble(tmp_path, keys[:1], keys[1])
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_workspace_switch_pauses_older_job_without_releasing_or_resending(tmp_path, speech_db):
    provider = StubProvider()
    paid = []

    async def settle(_session, _job, segment, action):
        paid.append((segment.id, action))

    async with speech_db() as factory:
        other = pipeline.VoicePipeline(
            factory, lambda _: provider, tmp_path, settle, binding=("cn-beijing", ""),
        )
        await other.sweep()
        # Direct recovery entry points must obey the same binding.
        await other.enroll("owner", "voice")
        await other.synthesize("owner", "job")
        async with factory() as session:
            assert (await session.get(pipeline.SpeechJob, "job")).state == "queued"
            assert (await session.get(pipeline.SpeechSegment, "s0")).state == "queued"
        assert not provider.sent and not paid

        restored = pipeline.VoicePipeline(
            factory, lambda _: provider, tmp_path, settle, binding=("cn-beijing", "test"),
        )
        await restored.synthesize("owner", "job")
        async with factory() as session:
            assert (await session.get(pipeline.SpeechJob, "job")).state == "ready"
    assert len(provider.sent) == 2
    assert paid == [("s0", "settle"), ("s1", "settle")]
