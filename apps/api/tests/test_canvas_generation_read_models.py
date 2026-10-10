from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from lumen_core.canvas_task_state import canvas_task_recovery
from lumen_core.models import Image, Video

from app.canvas_services.asset_descriptors import asset_descriptor
from app.canvas_services.billing_projection import ledger_summary, task_estimate
from app.canvas_services.run_serialization import execution_task_dict, output_dict


@pytest.mark.parametrize(
    "status", ["submitting", "submit_unknown", "submitted", "running"]
)
def test_live_task_cannot_be_silently_resubmitted(status):
    recovery = canvas_task_recovery(
        SimpleNamespace(status=status), task_kind="video_generation"
    )
    assert recovery["can_query"] is True
    assert recovery["can_generate_new"] is False
    assert recovery["automatic_resubmit"] is False
    if status == "submit_unknown":
        assert recovery["state"] == "submission_unknown"


def test_cancel_is_request_and_missing_owner_cannot_start_new_candidate():
    recovery = canvas_task_recovery(
        SimpleNamespace(
            status="running", cancel_requested_at=datetime.now(timezone.utc)
        ),
        task_kind="generation",
    )
    assert recovery["state"] == "cancel_requested"
    assert recovery["can_cancel"] is False
    assert not canvas_task_recovery(None, task_kind="generation")["can_generate_new"]


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled", "expired"])
def test_terminal_task_allows_explicit_new_candidate_only(status):
    recovery = canvas_task_recovery(
        SimpleNamespace(status=status), task_kind="generation"
    )
    assert recovery["can_generate_new"] is True
    assert recovery["automatic_resubmit"] is False


def test_asset_descriptor_preserves_original_identity_and_nullable_metadata():
    video = Video(
        id="v",
        sha256="a" * 64,
        width=0,
        height=0,
        duration_ms=0,
        size_bytes=100,
        mime="video/mp4",
        metadata_jsonb={},
    )
    descriptor = asset_descriptor(video)
    assert descriptor["asset_id"] == "v"
    assert descriptor["source_sha256"] == "a" * 64
    assert descriptor["preparation_state"] == "pending"
    assert descriptor["width"] is None
    assert descriptor["duration_ms"] is None
    assert descriptor["locators"]["thumb"] is None
    assert "storage_key" not in descriptor


def test_image_locators_are_purpose_specific():
    image = Image(
        id="i",
        sha256="a" * 64,
        width=4096,
        height=2048,
        size_bytes=9000,
        mime="image/png",
        artifact_status="ready",
    )
    descriptor = asset_descriptor(image)
    assert descriptor["preparation_state"] == "ready"
    assert descriptor["locators"]["thumb"].endswith("thumb256")
    assert descriptor["locators"]["preview"].endswith("preview1024")
    assert output_dict({"image_id": "i"})["thumb_url"].endswith("thumb256")


def test_stale_video_preparation_cannot_override_new_original():
    video = Video(
        id="v",
        sha256="a" * 64,
        width=100,
        height=100,
        duration_ms=1000,
        size_bytes=100,
        mime="video/mp4",
        metadata_jsonb={
            "canvas_preparation": {"source_sha256": "b" * 64, "state": "failed"}
        },
    )
    assert asset_descriptor(video)["preparation_state"] == "ready"
    video.metadata_jsonb["canvas_preparation"]["source_sha256"] = "a" * 64
    assert asset_descriptor(video)["preparation_state"] == "failed"


def test_image_estimate_is_snapshot_based_and_unknown_is_not_zero():
    assert task_estimate(None) is None
    assert task_estimate(SimpleNamespace(upstream_request={})) is None
    owner = SimpleNamespace(
        upstream_request={
            "billing_pricing_snapshot": {"kind": "image", "unit_price_micro": 10000},
            "billing_rate_multiplier_x10000": 10009,
        }
    )
    assert task_estimate(owner) == 10009


def tx(identifier, kind, amount, **meta):
    return SimpleNamespace(id=identifier, kind=kind, amount_micro=amount, meta=meta)


def test_ledger_uses_actual_service_cost_not_refunded_balance_delta():
    hold = tx("hold", "hold", -100, hold_delta=100)
    settle = tx("settle", "settle", 40, actual_micro=60, hold_delta=-100)
    result = ledger_summary([hold, settle, settle], estimate=100)
    assert result["reserved_micro"] == 0
    assert result["actual_cost_micro"] == 60
    assert result["estimated_cost_micro"] == 100


def test_missing_ledger_and_release_are_not_fabricated_free_generations():
    assert ledger_summary([], estimate=None)["actual_cost_micro"] is None
    assert ledger_summary([], estimate=None)["reserved_micro"] is None
    result = ledger_summary(
        [
            tx("hold", "hold", -100, hold_delta=100),
            tx("release", "release", 100, hold_delta=-100),
        ],
        estimate=100,
    )
    assert result["actual_cost_micro"] is None
    assert result["reserved_micro"] == 0


def test_image_owner_details_and_only_real_progress_are_serialized():
    now = datetime.now(timezone.utc)
    task = SimpleNamespace(
        id="task",
        task_kind="generation",
        status="queued",
        generation_id="image-gen",
        video_generation_id=None,
        completion_id=None,
        created_at=now,
        updated_at=now,
    )
    owner = SimpleNamespace(
        status="running",
        progress_stage="saving",
        progress_pct=True,
        model="image-model",
        size_requested="1024x1024",
    )
    result = execution_task_dict(task, owner)
    assert result["status"] == "running"
    assert result["model"] == "image-model"
    assert result["progress_pct"] is None
    assert result["recovery"]["state"] == "saving_artifact"


@pytest.mark.parametrize(
    ("after", "last", "seqs", "gap", "more"),
    [
        (0, 3, [1, 2], False, True),
        (2, 3, [3], False, False),
        (0, 3, [2, 3], True, False),
        (0, 3, [1, 3], True, False),
        (3, 3, [], False, False),
        (5, 3, [], True, False),
        (0, 3, [], True, False),
    ],
)
def test_event_cursor_gaps_require_snapshot_without_skipping(
    after, last, seqs, gap, more
):
    from app.canvas_services.history_service import event_batch

    batch = event_batch(
        [{"seq": seq} for seq in seqs], after_seq=after, last_event_seq=last
    )
    assert batch["snapshot_required"] is gap
    assert batch["has_more"] is more
    assert batch["next_after_seq"] == (seqs[-1] if seqs else after)


@pytest.mark.asyncio
async def test_asset_projection_excludes_foreign_and_deleted_and_deduplicates():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from lumen_core.models import Base
    from app.canvas_services.asset_descriptors import canvas_asset_descriptors

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[Image.__table__, Video.__table__]
            )
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        for identifier, owner, deleted in [
            ("owned", "u", None),
            ("foreign", "other", None),
            ("deleted", "u", datetime.now(timezone.utc)),
        ]:
            db.add(
                Image(
                    id=identifier,
                    user_id=owner,
                    source="uploaded",
                    storage_key=identifier,
                    mime="image/png",
                    width=10,
                    height=10,
                    size_bytes=100,
                    sha256="a" * 64,
                    deleted_at=deleted,
                )
            )
        await db.commit()
        assets = await canvas_asset_descriptors(
            db,
            user_id="u",
            graph={
                "nodes": [
                    {"config": {"image_id": value}}
                    for value in ["owned", "owned", "foreign", "deleted", "missing"]
                ]
            },
            executions=[SimpleNamespace(outputs_jsonb=[{"image_id": "owned"}])],
        )
        assert [asset["asset_id"] for asset in assets] == ["owned"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_image_owner_model_and_wallet_settlement_are_read_from_durable_rows():
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from lumen_core.models import Generation, WalletTransaction
    from lumen_core.canvas_models import CanvasExecutionTask
    from app.canvas_services.run_serialization import execution_tasks_by_execution

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    def create_tables(sync):
        for table in [
            Generation.__table__,
            WalletTransaction.__table__,
            CanvasExecutionTask.__table__,
        ]:
            ddl = str(CreateTable(table).compile(dialect=sqlite.dialect()))
            sync.exec_driver_sql(
                ddl.replace("DEFAULT (ARRAY[]::varchar[])", "DEFAULT '[]'")
            )

    async with engine.begin() as conn:
        await conn.run_sync(create_tables)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        generation = Generation(
            id="gen",
            message_id="message",
            user_id="u",
            action="generate",
            model="actual-model",
            prompt="private prompt",
            size_requested="1024x1024",
            aspect_ratio="1:1",
            status="running",
            progress_stage="saving",
            idempotency_key="gen",
            upstream_request={
                "api_key": "must-not-leak",
                "billing_pricing_snapshot": {"kind": "image", "unit_price_micro": 100},
                "billing_rate_multiplier_x10000": 10000,
            },
        )
        task = CanvasExecutionTask(
            id="task",
            execution_id="execution",
            ordinal=0,
            task_kind="generation",
            generation_id="gen",
            status="queued",
            idempotency_key="task",
            request_fingerprint="a" * 64,
            billing_ref_type="generation",
            billing_ref_id="gen",
        )
        settlement = WalletTransaction(
            id="settle",
            user_id="u",
            kind="settle",
            amount_micro=40,
            balance_after=40,
            hold_after=0,
            ref_type="generation",
            ref_id="gen",
            idempotency_key="settle",
            meta={"actual_micro": 60, "hold_delta": 0},
        )
        db.add_all([generation, task, settlement])
        await db.commit()
        projections = await execution_tasks_by_execution(
            db, [SimpleNamespace(id="execution", user_id="u")]
        )
        detail = projections["execution"][0]
        assert detail["model"] == "actual-model"
        assert detail["progress_stage"] == "saving"
        assert detail["billing"]["estimated_cost_micro"] == 100
        assert detail["billing"]["actual_cost_micro"] == 60
        assert "must-not-leak" not in str(detail)
        foreign = await execution_tasks_by_execution(
            db, [SimpleNamespace(id="execution", user_id="other")]
        )
        assert foreign["execution"][0]["model"] is None
        assert foreign["execution"][0]["billing"]["actual_cost_micro"] is None
    await engine.dispose()


def test_aggregate_keeps_incomplete_totals_unknown_and_deduplicates():
    from app.canvas_services.billing_projection import aggregate_task_billing

    task = {
        "id": "t1",
        "billing": {
            "estimated_cost_micro": 100,
            "reserved_micro": 0,
            "actual_cost_micro": 60,
        },
    }
    other = {
        "id": "t2",
        "billing": {
            "estimated_cost_micro": 200,
            "reserved_micro": 200,
            "actual_cost_micro": None,
        },
    }
    result = aggregate_task_billing([task, task, other])
    assert result["task_count"] == 2
    assert result["estimated_cost_micro"] == 300
    assert result["reserved_micro"] == 200
    assert result["actual_cost_micro"] is None
    assert result["known_actual_cost_micro"] == 60
