from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, MetaData, String, Table, create_engine, select

from app.images.adapters.sqlalchemy_repository import _reconcile_candidate_condition


def test_future_publishing_schedule_is_not_preempted_by_stale_timeout() -> None:
    metadata = MetaData()
    rows = Table(
        "image_rows",
        metadata,
        Column("id", String, primary_key=True),
        Column("artifact_status", String, nullable=False),
        Column("reconcile_after", DateTime(timezone=True)),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=1)
    with engine.begin() as connection:
        connection.execute(
            rows.insert(),
            [
                {
                    "id": "publishing-due",
                    "artifact_status": "publishing",
                    "reconcile_after": now - timedelta(seconds=1),
                    "updated_at": stale,
                },
                {
                    "id": "publishing-future",
                    "artifact_status": "publishing",
                    "reconcile_after": now + timedelta(hours=1),
                    "updated_at": stale,
                },
                {
                    "id": "publishing-unscheduled-stale",
                    "artifact_status": "publishing",
                    "reconcile_after": None,
                    "updated_at": stale,
                },
                {
                    "id": "ready-old-unscheduled",
                    "artifact_status": "ready",
                    "reconcile_after": None,
                    "updated_at": stale - timedelta(days=1),
                },
            ],
        )
        selected = connection.execute(
            select(rows.c.id).where(
                _reconcile_candidate_condition(
                    rows.c.artifact_status,
                    rows.c.reconcile_after,
                    rows.c.updated_at,
                    due_before=now,
                    stale_before=now - timedelta(minutes=5),
                )
            )
        ).scalars().all()

    assert set(selected) == {
        "publishing-due",
        "publishing-unscheduled-stale",
    }
