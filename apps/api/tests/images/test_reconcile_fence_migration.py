from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from lumen_core.model_entities.media_workflows import ImageReconcileEpoch
from lumen_core.models import Image


MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0049_image_reconcile_fence.py"
)


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "image_reconcile_fence_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_image_model_declares_nonnegative_reconcile_fence() -> None:
    column = Image.__table__.c.reconcile_fence
    assert column.nullable is False
    assert any(
        constraint.name == "ck_images_reconcile_fence"
        for constraint in Image.__table__.constraints
    )
    assert ImageReconcileEpoch.__table__.c.value.nullable is False


def test_reconcile_fence_migration_upgrades_and_downgrades_sqlite() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "images",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
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
            assert "reconcile_fence" in columns
            assert sa.inspect(connection).has_table("image_reconcile_epochs")
            epoch_table = sa.table(
                "image_reconcile_epochs",
                sa.column("id"),
                sa.column("value"),
            )
            epoch = connection.execute(
                sa.select(epoch_table.c.value).where(epoch_table.c.id == 1)
            ).scalar_one()
            assert epoch == 0
            migration.downgrade()
            downgraded = {
                column["name"]
                for column in sa.inspect(connection).get_columns("images")
            }
            assert "reconcile_fence" not in downgraded
            assert not sa.inspect(connection).has_table("image_reconcile_epochs")
        finally:
            migration.op = original
