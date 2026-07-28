from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[4]
MIGRATION = ROOT / "apps/api/alembic/versions/0047_image_variant_claims.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "image_variant_claims_migration_under_test",
        MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_variant_claim_migration_upgrade_and_downgrade_sqlite() -> None:
    migration = _load_migration()
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE images (id VARCHAR(36) PRIMARY KEY)")
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = inspect(connection)
        assert "image_variant_claims" in inspector.get_table_names()
        columns = {
            column["name"]: column
            for column in inspector.get_columns("image_variant_claims")
        }
        assert set(columns) == {
            "image_id",
            "kind",
            "token",
            "source_key",
            "source_sha256",
            "lease_until",
            "retry_at",
            "error_code",
            "created_at",
            "updated_at",
        }
        assert columns["retry_at"]["nullable"] is True
        assert columns["error_code"]["nullable"] is True
        assert all(
            columns[name]["nullable"] is False
            for name in (
                "image_id",
                "kind",
                "token",
                "source_key",
                "source_sha256",
                "lease_until",
                "created_at",
                "updated_at",
            )
        )
        assert inspector.get_pk_constraint("image_variant_claims")[
            "constrained_columns"
        ] == ["image_id", "kind"]
        foreign_keys = inspector.get_foreign_keys("image_variant_claims")
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["constrained_columns"] == ["image_id"]
        assert foreign_keys[0]["referred_table"] == "images"
        assert foreign_keys[0]["referred_columns"] == ["id"]
        assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"
        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("image_variant_claims")
        }
        assert indexes["ix_image_variant_claims_lease"]["column_names"] == [
            "lease_until",
            "retry_at",
        ]
        assert indexes["ix_image_variant_claims_lease"]["unique"] == 0

        migration.downgrade()
        assert "image_variant_claims" not in inspect(connection).get_table_names()

    engine.dispose()


def test_variant_claim_migration_upgrade_and_downgrade_postgres() -> None:
    raw_url = os.environ.get("LUMEN_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip("LUMEN_TEST_POSTGRES_URL is required")
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("LUMEN_TEST_POSTGRES_URL must use PostgreSQL")
    sync_url = url.set(drivername="postgresql+psycopg2")
    migration = _load_migration()
    schema = f"variant_claim_{uuid.uuid4().hex}"
    engine = create_engine(sync_url)

    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}"')
            connection.exec_driver_sql(
                "CREATE TABLE images (id VARCHAR(36) PRIMARY KEY)"
            )
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            inspector = inspect(connection)
            assert "image_variant_claims" in inspector.get_table_names()
            assert inspector.get_pk_constraint("image_variant_claims")[
                "constrained_columns"
            ] == ["image_id", "kind"]
            foreign_keys = inspector.get_foreign_keys("image_variant_claims")
            assert len(foreign_keys) == 1
            assert foreign_keys[0]["constrained_columns"] == ["image_id"]
            assert foreign_keys[0]["referred_table"] == "images"
            assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"
            indexes = {
                index["name"]: index
                for index in inspector.get_indexes("image_variant_claims")
            }
            assert indexes["ix_image_variant_claims_lease"]["column_names"] == [
                "lease_until",
                "retry_at",
            ]

            migration.downgrade()
            assert "image_variant_claims" not in inspect(connection).get_table_names()
        finally:
            transaction.rollback()

    engine.dispose()
