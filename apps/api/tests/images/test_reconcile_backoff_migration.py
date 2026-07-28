from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lumen_core.models import Image


MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0048_image_reconcile_backoff.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "image_reconcile_backoff_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_image_model_declares_reconcile_backoff_and_quarantine_fields() -> None:
    columns = Image.__table__.c
    assert columns.reconcile_attempts.nullable is False
    assert columns.last_reconcile_error_code.nullable is True
    assert columns.last_reconcile_error_at.nullable is True
    assert columns.quarantined_at.nullable is True


def test_reconcile_backoff_migration_upgrades_and_downgrades_sqlite() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "images",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("artifact_status", sa.String(24), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)
    migration = _load_migration()

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original = migration.op
        migration.op = operations
        try:
            migration.upgrade()
            columns = {
                column["name"]
                for column in sa.inspect(connection).get_columns("images")
            }
            assert {
                "reconcile_attempts",
                "last_reconcile_error_code",
                "last_reconcile_error_at",
                "quarantined_at",
            } <= columns
            migration.downgrade()
            downgraded = {
                column["name"]
                for column in sa.inspect(connection).get_columns("images")
            }
            assert "reconcile_attempts" not in downgraded
            assert "quarantined_at" not in downgraded
        finally:
            migration.op = original
