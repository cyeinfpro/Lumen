from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app import main
from app.routes import storyboards
from app.routes.storyboard_parts import assembly as storyboard_assembly_commands
from app.routes.storyboard_parts import image_submission as storyboard_image_submission
from app.routes.storyboard_parts import queries as storyboard_queries
from app.routes.storyboard_parts import video_submission as storyboard_video_submission
from app.services.storyboard import idempotency as storyboard_idempotency
from app.services.storyboard.contracts import StoryboardRunOut
from app.services.storyboard import tasks as storyboard_tasks


def _storyboard_run_out(
    *,
    run_id: str = "run-1",
    current_stage: str = "assets",
) -> StoryboardRunOut:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    return StoryboardRunOut.model_validate(
        {
            "id": run_id,
            "conversation_id": "conv-1",
            "title": "Storyboard",
            "idea": "Idea",
            "style": "",
            "script": "",
            "script_confirmed": False,
            "script_revision": 0,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "model": "seedance-2.0",
            "generate_audio": True,
            "seed": None,
            "status": "in_progress",
            "current_stage": current_stage,
            "assets": [],
            "shots": [],
            "assembly": None,
            "thumbnail_url": None,
            "created_at": now,
            "updated_at": now,
        }
    )


class _StoryboardRows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> "_StoryboardRows":
        return self

    def all(self) -> list[Any]:
        return list(self.rows)


class _StoryboardIdempotencyDb:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.commits = 0
        self.rollbacks = 0
        self.release_lock: Any = None

    async def execute(self, _statement: Any) -> _StoryboardRows:
        return _StoryboardRows(self.rows)

    async def commit(self) -> None:
        self.commits += 1
        if self.release_lock is not None and self.release_lock.locked():
            self.release_lock.release()

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self.release_lock is not None and self.release_lock.locked():
            self.release_lock.release()


def test_storyboard_paid_idempotency_key_normalization() -> None:
    resolve = storyboard_idempotency.resolve_client_idempotency_key

    assert resolve("request-1") == "request-1"
    assert resolve(None, "request-1") == "request-1"
    assert resolve("request-1", "request-1") == "request-1"

    with pytest.raises(HTTPException) as missing:
        resolve(None)
    assert missing.value.status_code == 422
    assert missing.value.detail["error"]["code"] == "idempotency_key_required"

    with pytest.raises(HTTPException) as mismatch:
        resolve("header-key", "body-key")
    assert mismatch.value.status_code == 422
    assert mismatch.value.detail["error"]["code"] == "idempotency_key_mismatch"

    for invalid in ("", " request-1", "request-1 ", "请求-1"):
        with pytest.raises(HTTPException) as invalid_key:
            resolve(invalid)
        assert invalid_key.value.status_code == 422
        assert invalid_key.value.detail["error"]["code"] == (
            "idempotency_key_invalid"
        )


@pytest.mark.asyncio
async def test_storyboard_paid_operation_replays_original_response_and_tasks() -> None:
    run = SimpleNamespace(
        id="run-1",
        metadata_jsonb={},
    )
    operation = storyboard_idempotency.paid_storyboard_operation(
        user_id="user-1",
        idempotency_key="client-request-1",
        operation_namespace=storyboard_idempotency.ASSET_GENERATE_OPERATION,
        payload={"run_id": "run-1", "step_id": "asset-1", "body": {}},
    )
    response = _storyboard_run_out()
    storyboard_idempotency.record_paid_operation(
        run,  # type: ignore[arg-type]
        operation,
        response=response,
        task_ids=["generation-1"],
        child_task_keys={"asset:asset-1:image": "task-key-1"},
        created_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
    )

    replay = await storyboard_idempotency.find_paid_operation(
        _StoryboardIdempotencyDb([run]),  # type: ignore[arg-type]
        operation,
    )

    assert replay is not None
    assert replay.response == response
    assert replay.task_ids == ("generation-1",)
    assert replay.child_task_keys == {"asset:asset-1:image": "task-key-1"}
    assert "client-request-1" not in repr(run.metadata_jsonb)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_namespace", "payload"),
    (
        (
            storyboard_idempotency.ASSET_GENERATE_OPERATION,
            {"run_id": "run-1", "step_id": "asset-1", "body": {"prompt": "new"}},
        ),
        (
            storyboard_idempotency.KEYFRAME_GENERATE_OPERATION,
            {"run_id": "run-1", "step_id": "asset-1", "body": {}},
        ),
    ),
)
async def test_storyboard_paid_operation_rejects_key_reuse_conflicts(
    operation_namespace: str,
    payload: dict[str, Any],
) -> None:
    run = SimpleNamespace(id="run-1", metadata_jsonb={})
    original = storyboard_idempotency.paid_storyboard_operation(
        user_id="user-1",
        idempotency_key="client-request-1",
        operation_namespace=storyboard_idempotency.ASSET_GENERATE_OPERATION,
        payload={"run_id": "run-1", "step_id": "asset-1", "body": {}},
    )
    storyboard_idempotency.record_paid_operation(
        run,  # type: ignore[arg-type]
        original,
        response=_storyboard_run_out(),
        task_ids=["generation-1"],
        child_task_keys={},
        created_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
    )
    conflicting = storyboard_idempotency.paid_storyboard_operation(
        user_id="user-1",
        idempotency_key="client-request-1",
        operation_namespace=operation_namespace,
        payload=payload,
    )

    with pytest.raises(HTTPException) as excinfo:
        await storyboard_idempotency.find_paid_operation(
            _StoryboardIdempotencyDb([run]),  # type: ignore[arg-type]
            conflicting,
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "idempotency_conflict"


def test_storyboard_batch_child_task_keys_are_deterministic() -> None:
    operation = storyboard_idempotency.paid_storyboard_operation(
        user_id="user-1",
        idempotency_key="batch-request-1",
        operation_namespace=storyboard_idempotency.KEYFRAME_GENERATE_ALL_OPERATION,
        payload={"run_id": "run-1"},
    )

    first = storyboard_idempotency.child_task_idempotency_key(
        operation,
        "shot:shot-1:keyframe",
    )
    replay = storyboard_idempotency.child_task_idempotency_key(
        operation,
        "shot:shot-1:keyframe",
    )
    sibling = storyboard_idempotency.child_task_idempotency_key(
        operation,
        "shot:shot-2:keyframe",
    )

    assert replay == first
    assert sibling != first
    assert first.startswith("sb:")
    assert len(first) <= 96


@pytest.mark.asyncio
async def test_storyboard_paid_operation_race_executes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id="run-1", metadata_jsonb={})
    db = _StoryboardIdempotencyDb([run])
    transaction_lock = asyncio.Lock()
    db.release_lock = transaction_lock
    action_calls = 0
    response = _storyboard_run_out()
    operation = storyboard_idempotency.paid_storyboard_operation(
        user_id="user-1",
        idempotency_key="race-request-1",
        operation_namespace=storyboard_idempotency.ASSET_GENERATE_OPERATION,
        payload={"run_id": "run-1", "step_id": "asset-1", "body": {}},
    )

    async def lock_key(
        _db: Any,
        _namespace: str,
        _user_id: str,
        _key: str,
    ) -> None:
        await transaction_lock.acquire()

    async def action(
        paid: storyboard_idempotency.PaidStoryboardOperation,
    ) -> StoryboardRunOut:
        nonlocal action_calls
        action_calls += 1
        await asyncio.sleep(0)
        storyboard_idempotency.record_paid_operation(
            run,  # type: ignore[arg-type]
            paid,
            response=response,
            task_ids=["generation-1"],
            child_task_keys={"asset:asset-1:image": "task-key-1"},
            created_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        )
        await db.commit()
        return response

    monkeypatch.setattr(storyboard_idempotency, "lock_user_key", lock_key)

    first, second = await asyncio.gather(
        storyboard_idempotency.execute_paid_operation(
            db,  # type: ignore[arg-type]
            operation,
            action,
        ),
        storyboard_idempotency.execute_paid_operation(
            db,  # type: ignore[arg-type]
            operation,
            action,
        ),
    )

    assert first == response
    assert second == response
    assert action_calls == 1
    assert db.commits == 2


@pytest.mark.asyncio
async def test_storyboard_ambiguous_post_commit_retry_replays_without_new_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id="run-1", metadata_jsonb={})
    db = _StoryboardIdempotencyDb([run])
    response = _storyboard_run_out(current_stage="videos")
    operation = storyboard_idempotency.paid_storyboard_operation(
        user_id="user-1",
        idempotency_key="ambiguous-request-1",
        operation_namespace=storyboard_idempotency.SHOT_SUBMIT_OPERATION,
        payload={"run_id": "run-1", "step_id": "shot-1", "body": {}},
    )

    async def no_lock(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def committed_then_lost(
        paid: storyboard_idempotency.PaidStoryboardOperation,
    ) -> StoryboardRunOut:
        storyboard_idempotency.record_paid_operation(
            run,  # type: ignore[arg-type]
            paid,
            response=response,
            task_ids=["video-generation-1"],
            child_task_keys={"shot:shot-1:video": "video-task-key-1"},
            created_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        )
        await db.commit()
        raise TimeoutError("response lost after commit")

    async def must_not_run(
        _paid: storyboard_idempotency.PaidStoryboardOperation,
    ) -> StoryboardRunOut:
        raise AssertionError("retry must replay the committed operation")

    monkeypatch.setattr(storyboard_idempotency, "lock_user_key", no_lock)

    with pytest.raises(TimeoutError):
        await storyboard_idempotency.execute_paid_operation(
            db,  # type: ignore[arg-type]
            operation,
            committed_then_lost,
        )
    replay = await storyboard_idempotency.execute_paid_operation(
        db,  # type: ignore[arg-type]
        operation,
        must_not_run,
    )

    assert replay == response
    assert db.commits == 2


def test_decode_cursor_requires_timezone_aware_timestamp() -> None:
    assert storyboards._decode_cursor(None) is None  # noqa: SLF001

    timestamp = datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc)
    assert storyboards._decode_cursor(f"{timestamp.isoformat()}|run-1") == (  # noqa: SLF001
        timestamp,
        "run-1",
    )

    with pytest.raises(HTTPException) as excinfo:
        storyboards._decode_cursor("2026-06-11T09:30:00|run-1")  # noqa: SLF001

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_cursor"


def test_shots_from_script_splits_script_and_provides_fallback() -> None:
    shots = storyboards._shots_from_script("开场建立场景。展示产品动作！结尾收束")  # noqa: SLF001

    assert [shot.title for shot in shots] == ["镜头 01", "镜头 02", "镜头 03"]
    assert shots[0].shot_type == "establishing shot"
    assert all(
        shot.duration_s == storyboards.STORYBOARD_DEFAULT_DURATION_S for shot in shots
    )

    fallback = storyboards._shots_from_script("")  # noqa: SLF001
    assert len(fallback) == 3
    assert fallback[0].visual


def test_shot_source_hash_changes_when_asset_reference_changes() -> None:
    shot = SimpleNamespace(
        input_json={
            "title": "镜头 01",
            "visual": "主角拿起产品",
            "reference_notes": "保持白色包装",
            "keyframe_prompt": "电影感首帧",
            "asset_ids": ["asset-1"],
        }
    )
    asset = SimpleNamespace(
        id="asset-1",
        input_json={"revision": 1},
        output_json={"image_id": "image-1"},
        approved_at=None,
    )

    original = storyboards._shot_source_hash(shot, {"asset-1": asset})  # noqa: SLF001
    revision_changed = storyboards._shot_source_hash(  # noqa: SLF001
        shot,
        {
            "asset-1": SimpleNamespace(
                id="asset-1",
                input_json={"revision": 2},
                output_json={"image_id": "image-1"},
                approved_at=None,
            )
        },
    )
    image_changed = storyboards._shot_source_hash(  # noqa: SLF001
        shot,
        {
            "asset-1": SimpleNamespace(
                id="asset-1",
                input_json={"revision": 1},
                output_json={"image_id": "image-2"},
                approved_at=None,
            )
        },
    )

    assert revision_changed != original
    assert image_changed != original


@pytest.mark.parametrize(
    "field",
    ("purpose", "narration", "shot_type", "camera_move", "transition"),
)
def test_shot_source_hash_tracks_all_prompt_inputs(field: str) -> None:
    source = {
        "title": "镜头 01",
        "purpose": "推进情节",
        "narration": "旁白",
        "visual": "主角拿起产品",
        "shot_type": "medium shot",
        "camera_move": "slow dolly in",
        "transition": "cut",
        "reference_notes": "保持白色包装",
        "keyframe_prompt": "电影感首帧",
        "asset_ids": [],
    }
    original = storyboards._shot_source_hash(  # noqa: SLF001
        SimpleNamespace(input_json=source),
        {},
    )
    changed = dict(source)
    changed[field] = f"{source[field]} changed"

    assert (
        storyboards._shot_source_hash(  # noqa: SLF001
            SimpleNamespace(input_json=changed),
            {},
        )
        != original
    )


def test_assembly_replay_policy_allows_failed_attempt_retry() -> None:
    output = {"assembly_fingerprint": "fingerprint-1"}

    for status in ("waiting", "compositing", "done"):
        assert storyboards._assembly_request_is_replay(  # noqa: SLF001
            SimpleNamespace(status=status),
            output,
            "fingerprint-1",
        )

    assert not storyboards._assembly_request_is_replay(  # noqa: SLF001
        SimpleNamespace(status="failed"),
        output,
        "fingerprint-1",
    )


def test_clear_shot_video_output_removes_stale_video_fields() -> None:
    cleaned = storyboards._clear_shot_video_output(  # noqa: SLF001
        {
            "video_generation_id": "video-gen-1",
            "video_id": "video-1",
            "video_status": "running",
            "video_progress_stage": "fetching",
            "video_progress_pct": 80,
            "video_submission": {"idempotency_key": "sb:old"},
            "keyframe_image_id": "image-1",
            "notes": "keep",
        }
    )

    assert cleaned == {"keyframe_image_id": "image-1", "notes": "keep"}


def test_storyboard_video_idempotency_uses_submission_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storyboards, "new_uuid7", lambda: "nonce-1")
    step = SimpleNamespace(
        id="shot-1",
        input_json={"keyframe_source_hash": "mutable-source-hash"},
        output_json={
            "keyframe_generation_id": "keyframe-gen-1",
            "keyframe_image_id": "image-1",
        },
    )

    key, fingerprint = storyboards._resolve_storyboard_video_idempotency_key(  # noqa: SLF001
        run_id="run-1",
        step=step,  # type: ignore[arg-type]
        keyframe_image_id="image-1",
        requested_key=None,
    )

    assert key.startswith("sb:run-1:shot-1:v:")
    assert "mutable-source-hash" not in key
    assert len(key) <= 96

    step.output_json["video_submission"] = {
        "fingerprint": fingerprint,
        "idempotency_key": key,
    }
    replay_key, replay_fingerprint = (
        storyboards._resolve_storyboard_video_idempotency_key(  # noqa: SLF001
            run_id="run-1",
            step=step,  # type: ignore[arg-type]
            keyframe_image_id="image-1",
            requested_key=None,
        )
    )

    assert replay_key == key
    assert replay_fingerprint == fingerprint


def test_storyboard_assembly_fingerprint_and_idempotency_are_stable() -> None:
    first = storyboards._storyboard_assembly_fingerprint(  # noqa: SLF001
        ["video-gen-1", "video-gen-2"]
    )
    replay = storyboards._storyboard_assembly_fingerprint(  # noqa: SLF001
        ["video-gen-1", "video-gen-2"]
    )
    reordered = storyboards._storyboard_assembly_fingerprint(  # noqa: SLF001
        ["video-gen-2", "video-gen-1"]
    )

    assert replay == first
    assert reordered != first
    assert storyboards._storyboard_assembly_idempotency_key(  # noqa: SLF001
        run_id="run-1",
        fingerprint=first,
    ) == storyboards._storyboard_assembly_idempotency_key(  # noqa: SLF001
        run_id="run-1",
        fingerprint=replay,
    )


def test_scheduled_assembly_waiting_status_is_reported_as_compositing() -> None:
    assembly = SimpleNamespace(status="waiting")

    assert (
        storyboards._assembly_status_for_response(  # noqa: SLF001
            assembly,  # type: ignore[arg-type]
            {"assembly_attempt_token": "attempt-1"},
        )
        == "compositing"
    )
    assert (
        storyboards._assembly_status_for_response(  # noqa: SLF001
            assembly,  # type: ignore[arg-type]
            {},
        )
        == "waiting"
    )


def test_assembly_replay_uses_explicit_lease_expiry() -> None:
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    assembly = SimpleNamespace(status="compositing")
    output = {
        "assembly_fingerprint": "fingerprint-1",
        "assembly_claimed_at": (now - timedelta(minutes=1)).isoformat(),
        "assembly_heartbeat_at": now.isoformat(),
        "assembly_lease_expires_at": (now + timedelta(seconds=1)).isoformat(),
    }

    assert storyboards._assembly_request_is_replay(  # noqa: SLF001
        assembly,  # type: ignore[arg-type]
        output,
        "fingerprint-1",
        now=now,
    )

    output["assembly_lease_expires_at"] = now.isoformat()
    assert storyboards._assembly_attempt_is_stale(  # noqa: SLF001
        assembly,  # type: ignore[arg-type]
        output,
        now=now,
    )
    assert not storyboards._assembly_request_is_replay(  # noqa: SLF001
        assembly,  # type: ignore[arg-type]
        output,
        "fingerprint-1",
        now=now,
    )


@pytest.mark.asyncio
async def test_assemble_sequential_replay_returns_status_without_reenqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(id="run-1", current_step="shots")
    shots = [
        SimpleNamespace(
            id="shot-1",
            step_key="shot:shot-1",
            status="done",
            input_json={"index": 0},
            output_json={"video_generation_id": "video-gen-1"},
            created_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            id="shot-2",
            step_key="shot:shot-2",
            status="done",
            input_json={"index": 1},
            output_json={"video_generation_id": "video-gen-2"},
            created_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        ),
    ]
    assembly = SimpleNamespace(
        id="assembly-1",
        status="waiting",
        output_json={},
        task_ids=[],
    )

    class Db:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.commits = 0

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            self.added[-1].id = "outbox-1"

        async def commit(self) -> None:
            self.commits += 1

    class Pool:
        def __init__(self) -> None:
            self.enqueued: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
            self.enqueued.append((args, kwargs))

    db = Db()
    pool = Pool()
    published: list[str] = []

    async def get_run(*_args: Any, **_kwargs: Any) -> Any:
        return run

    async def no_sync(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def load_steps(*_args: Any, **_kwargs: Any) -> list[Any]:
        return shots

    async def get_assembly(*_args: Any, **_kwargs: Any) -> Any:
        return assembly

    async def build_out(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            assembly=SimpleNamespace(
                status=storyboards._assembly_status_for_response(  # noqa: SLF001
                    assembly, assembly.output_json
                )
            )
        )

    async def get_pool() -> Pool:
        return pool

    async def publish(
        _user_id: str,
        _run_id: str,
        event_name: str,
        _data: dict[str, Any],
    ) -> None:
        published.append(event_name)

    monkeypatch.setattr(storyboards, "_get_run", get_run)
    monkeypatch.setattr(storyboards, "_sync_storyboard_outputs", no_sync)
    monkeypatch.setattr(storyboards, "_load_steps", load_steps)
    monkeypatch.setattr(storyboards, "_assembly_step", get_assembly)
    monkeypatch.setattr(storyboards, "_build_run_out", build_out)
    monkeypatch.setattr(storyboards, "get_arq_pool", get_pool)
    monkeypatch.setattr(storyboards, "_publish_storyboard_event", publish)
    fixed_now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(storyboards, "_now", lambda: fixed_now)
    attempts = iter(("attempt-1", "attempt-2"))
    monkeypatch.setattr(storyboards, "new_uuid7", lambda: next(attempts))

    first = await storyboards.assemble_storyboard(
        "run-1",
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )
    replay = await storyboards.assemble_storyboard(
        "run-1",
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )
    assembly.status = "compositing"
    assembly.output_json.update(
        {
            "assembly_claimed_at": fixed_now.isoformat(),
            "assembly_heartbeat_at": fixed_now.isoformat(),
            "assembly_lease_expires_at": (
                fixed_now
                + timedelta(seconds=storyboards.STORYBOARD_ASSEMBLY_WORKER_LEASE_S)
            ).isoformat(),
        }
    )
    compositing_replay = await storyboards.assemble_storyboard(
        "run-1",
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )
    assembly.status = "failed"
    failed_retry = await storyboards.assemble_storyboard(
        "run-1",
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert first.assembly.status == "compositing"
    assert replay.assembly.status == "compositing"
    assert compositing_replay.assembly.status == "compositing"
    assert failed_retry.assembly.status == "compositing"
    assert len(db.added) == 2
    assert len(pool.enqueued) == 2
    assert pool.enqueued[0][0][:3] == (
        "run_storyboard_assembly",
        "run-1",
        "attempt-1",
    )
    assert pool.enqueued[1][0][:3] == (
        "run_storyboard_assembly",
        "run-1",
        "attempt-2",
    )
    assert published == ["storyboard.assembling", "storyboard.assembling"]
    assert db.commits == 4
    fingerprint = storyboards._storyboard_assembly_fingerprint(  # noqa: SLF001
        ["video-gen-1", "video-gen-2"]
    )
    assert assembly.output_json["assembly_fingerprint"] == fingerprint
    assert assembly.output_json["assembly_idempotency_key"] == (
        storyboards._storyboard_assembly_idempotency_key(  # noqa: SLF001
            run_id="run-1",
            fingerprint=fingerprint,
        )
    )
    assert db.added[0].payload["assembly_attempt_token"] == "attempt-1"
    assert db.added[1].payload["assembly_attempt_token"] == "attempt-2"


@pytest.mark.parametrize("attempt_status", ("waiting", "compositing"))
@pytest.mark.asyncio
async def test_stale_assembly_claim_requeues_after_worker_sigkill(
    monkeypatch: pytest.MonkeyPatch,
    attempt_status: str,
) -> None:
    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    fingerprint = storyboards._storyboard_assembly_fingerprint(  # noqa: SLF001
        ["video-gen-1", "video-gen-2"]
    )
    run = SimpleNamespace(id="run-1", current_step="assembly")
    shots = [
        SimpleNamespace(
            id="shot-1",
            step_key="shot:shot-1",
            status="done",
            input_json={"index": 0},
            output_json={"video_generation_id": "video-gen-1"},
            created_at=now - timedelta(minutes=2),
        ),
        SimpleNamespace(
            id="shot-2",
            step_key="shot:shot-2",
            status="done",
            input_json={"index": 1},
            output_json={"video_generation_id": "video-gen-2"},
            created_at=now - timedelta(minutes=1),
        ),
    ]
    claimed_at = (
        (now - timedelta(minutes=3)).isoformat()
        if attempt_status == "compositing"
        else None
    )
    assembly = SimpleNamespace(
        id="assembly-1",
        status=attempt_status,
        updated_at=now - timedelta(minutes=10),
        output_json={
            "segment_ids": ["video-gen-1", "video-gen-2"],
            "assembly_fingerprint": fingerprint,
            "assembly_attempt_token": "attempt-old",
            "assembly_enqueued_at": (now - timedelta(minutes=10)).isoformat(),
            "assembly_claimed_at": claimed_at,
            "assembly_heartbeat_at": claimed_at,
            "assembly_lease_expires_at": (now - timedelta(seconds=1)).isoformat(),
            "assembly_recovery_count": 2,
        },
        task_ids=["outbox-old"],
    )

    class Db:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.commits = 0

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            self.added[-1].id = "outbox-new"

        async def commit(self) -> None:
            self.commits += 1

    class Pool:
        def __init__(self) -> None:
            self.enqueued: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
            self.enqueued.append((args, kwargs))

    db = Db()
    pool = Pool()
    published: list[tuple[str, dict[str, Any]]] = []

    async def get_run(*_args: Any, **_kwargs: Any) -> Any:
        return run

    async def no_sync(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def load_steps(*_args: Any, **_kwargs: Any) -> list[Any]:
        return shots

    async def get_assembly(*_args: Any, **_kwargs: Any) -> Any:
        return assembly

    async def build_out(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            assembly=SimpleNamespace(
                status=storyboards._assembly_status_for_response(  # noqa: SLF001
                    assembly,
                    assembly.output_json,
                )
            )
        )

    async def get_pool() -> Pool:
        return pool

    async def publish(
        _user_id: str,
        _run_id: str,
        event_name: str,
        data: dict[str, Any],
    ) -> None:
        published.append((event_name, data))

    monkeypatch.setattr(storyboards, "_get_run", get_run)
    monkeypatch.setattr(storyboards, "_sync_storyboard_outputs", no_sync)
    monkeypatch.setattr(storyboards, "_load_steps", load_steps)
    monkeypatch.setattr(storyboards, "_assembly_step", get_assembly)
    monkeypatch.setattr(storyboards, "_build_run_out", build_out)
    monkeypatch.setattr(storyboards, "get_arq_pool", get_pool)
    monkeypatch.setattr(storyboards, "_publish_storyboard_event", publish)
    monkeypatch.setattr(storyboards, "_now", lambda: now)
    monkeypatch.setattr(storyboards, "new_uuid7", lambda: "attempt-new")

    recovered = await storyboards.assemble_storyboard(
        "run-1",
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert recovered.assembly.status == "compositing"
    assert assembly.status == attempt_status
    assert assembly.output_json["assembly_attempt_token"] == "attempt-new"
    assert assembly.output_json["assembly_claimed_at"] is None
    assert assembly.output_json["assembly_heartbeat_at"] is None
    assert assembly.output_json["assembly_recovery_count"] == 3
    assert assembly.output_json["assembly_recovery_reason"] == "lease_expired"
    assert assembly.output_json["assembly_superseded_attempt_token"] == "attempt-old"
    assert assembly.task_ids == ["outbox-new"]
    assert db.commits == 1
    assert db.added[0].payload["assembly_recovered"] is True
    assert db.added[0].payload["assembly_attempt_token"] == "attempt-new"
    assert pool.enqueued[0][0][:3] == (
        "run_storyboard_assembly",
        "run-1",
        "attempt-new",
    )
    assert published == [
        (
            "storyboard.assembling",
            {
                "segment_ids": ["video-gen-1", "video-gen-2"],
                "assembly_fingerprint": fingerprint,
                "assembly_attempt_token": "attempt-new",
                "recovered": True,
            },
        )
    ]


def test_storyboard_next_cursor_uses_last_returned_row() -> None:
    source = inspect.getsource(storyboard_queries.list_storyboards)

    assert "encode_cursor(page[-1])" in source
    assert "encode_cursor(rows[limit])" not in source


def test_generate_all_keyframes_validates_batch_before_creating_tasks() -> None:
    source = inspect.getsource(storyboard_image_submission.generate_all_keyframes)

    assert "shots_not_approved" in source
    assert source.index("shots_not_approved") < source.index(
        "create_storyboard_image_task"
    )


def test_storyboard_status_sync_recovers_orphan_video_generation() -> None:
    source = inspect.getsource(storyboards._recover_storyboard_video_generations)  # noqa: SLF001

    assert "workflow_run_id" in source
    assert "workflow_step_key" in source
    assert "as_string()" in source
    assert "storyboard_video_submission_fingerprint" in source


@pytest.mark.asyncio
async def test_storyboard_video_recovery_requires_current_submission_fingerprint() -> (
    None
):
    step = SimpleNamespace(
        step_key="shot:1",
        input_json={"keyframe_source_hash": "source-new"},
        output_json={
            "keyframe_generation_id": "keyframe-new",
            "keyframe_image_id": "image-new",
        },
    )
    expected_fingerprint = storyboards._storyboard_video_submission_fingerprint(  # noqa: SLF001
        step=step,  # type: ignore[arg-type]
        keyframe_image_id="image-new",
    )
    rows = [
        SimpleNamespace(
            id="video-old",
            upstream_request={
                "workflow_step_key": "shot:1",
                "storyboard_video_submission_fingerprint": "old-fingerprint",
            },
        ),
        SimpleNamespace(
            id="video-new",
            upstream_request={
                "workflow_step_key": "shot:1",
                "storyboard_video_submission_fingerprint": expected_fingerprint,
            },
        ),
    ]

    class Result:
        def scalars(self) -> "Result":
            return self

        def all(self) -> list[SimpleNamespace]:
            return rows

    class Db:
        async def execute(self, _stmt):
            return Result()

    recovered = await storyboards._recover_storyboard_video_generations(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        run=SimpleNamespace(id="run-1", user_id="user-1"),  # type: ignore[arg-type]
        steps=[step],  # type: ignore[list-item]
    )

    assert recovered == {"shot:1": rows[1]}


def test_storyboard_image_task_helper_does_not_commit_before_step_link() -> None:
    source = inspect.getsource(
        storyboard_image_submission.create_storyboard_image_task
    )

    assert "await db.commit()" not in source
    assert "StoryboardImageTask(" in source


def test_generate_all_keyframes_does_not_call_single_route_handler() -> None:
    source = inspect.getsource(storyboard_image_submission.generate_all_keyframes)
    publish_source = inspect.getsource(storyboard_tasks.publish_storyboard_image_tasks)

    assert "generate_shot_keyframe(" not in source
    assert "runtime.publish_image_tasks" in source
    assert "Semaphore(STORYBOARD_KEYFRAME_PARALLELISM)" in publish_source


def test_paid_storyboard_entrypoints_use_durable_operation_wrapper() -> None:
    for handler in (
        storyboard_image_submission.generate_asset,
        storyboard_image_submission.generate_shot_keyframe,
        storyboard_image_submission.generate_all_keyframes,
        storyboard_video_submission.submit_shot,
        storyboard_video_submission.submit_all_shots,
    ):
        source = inspect.getsource(handler)
        assert "execute_paid_operation" in source
        assert "resolve_client_idempotency_key" in source


def test_storyboard_video_submission_commits_task_and_step_together() -> None:
    create_source = inspect.getsource(
        storyboard_video_submission.create_storyboard_video_task
    )
    single_source = inspect.getsource(storyboard_video_submission.submit_shot)
    batch_source = inspect.getsource(storyboard_video_submission.submit_all_shots)

    assert "defer_commit=True" in create_source
    assert "deferred_publish_payload=publish_payload" in create_source
    assert single_source.index("_record_operation(") < single_source.index(
        "await db.commit()"
    )
    assert batch_source.index("_record_operation(") < batch_source.index(
        "await db.commit()"
    )
    assert "submit_shot(" not in batch_source


def test_storyboards_transport_stays_below_route_ceiling() -> None:
    source = inspect.getsource(storyboards)

    assert len(source.splitlines()) < 800
    assert "await db.commit()" not in source
    assert "OutboxEvent(" not in source
    assert "return await commands." in source
    assert "return await image_submission." in source
    assert "return await video_submission." in source
    assert "return await assembly." in source


def test_storyboard_assembly_commits_outbox_before_enqueue() -> None:
    source = inspect.getsource(storyboard_assembly_commands.assemble_storyboard)
    scheduling = source[source.index("db.add(outbox)") :]

    assert scheduling.index("db.add(outbox)") < scheduling.index("await db.commit()")
    assert scheduling.index("await db.commit()") < scheduling.index(
        "runtime.arq_pool()"
    )


def test_storyboards_router_is_mounted() -> None:
    source = inspect.getsource(main._include_app_routers)  # noqa: SLF001

    assert "storyboards" in source
    assert "include_router(storyboards.router)" in source
