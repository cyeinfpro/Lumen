from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, create_engine, inspect, text

from lumen_core.canvas_models import (
    CanvasAssetRef,
    CanvasDocument,
    CanvasExecutionTask,
    CanvasMutation,
    CanvasNodeExecution,
    CanvasNodeSelection,
    CanvasRun,
    CanvasRunEvent,
    CanvasTaskTerminalReceipt,
    CanvasVersion,
)
from lumen_core.models import Base


CANVAS_TABLES = {
    "canvas_documents",
    "canvas_mutations",
    "canvas_versions",
    "canvas_runs",
    "canvas_node_executions",
    "canvas_execution_tasks",
    "canvas_task_terminal_receipts",
    "canvas_node_selections",
    "canvas_asset_refs",
    "canvas_run_events",
}


def test_canvas_models_are_loaded_into_base_metadata_with_core_invariants() -> None:
    assert CANVAS_TABLES <= set(Base.metadata.tables)
    assert CanvasDocument.__table__.c.revision.nullable is False
    assert CanvasNodeSelection.__table__.primary_key.columns.keys() == [
        "canvas_id",
        "node_id",
    ]

    mutation_constraints = {
        constraint.name for constraint in CanvasMutation.__table__.constraints
    }
    task_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in CanvasExecutionTask.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    receipt_constraints = {
        constraint.name
        for constraint in CanvasTaskTerminalReceipt.__table__.constraints
    }
    asset_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in CanvasAssetRef.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "uq_canvas_mutations_client_mutation" in mutation_constraints
    assert "uq_canvas_mutations_result_revision" in mutation_constraints
    assert (
        "generation_id IS NOT NULL"
        in task_checks["ck_canvas_execution_tasks_task_owner"]
    )
    assert (
        "video_generation_id IS NOT NULL"
        in task_checks["ck_canvas_execution_tasks_task_owner"]
    )
    assert "uq_canvas_task_terminal_receipts_epoch" in receipt_constraints
    assert "image_id IS NOT NULL" in asset_checks["ck_canvas_asset_refs_asset"]
    assert "scope = 'execution'" in asset_checks["ck_canvas_asset_refs_owner"]


def test_canvas_model_tables_compile_and_create_on_sqlite() -> None:
    engine = create_engine("sqlite://")
    tables = [Base.metadata.tables[name] for name in sorted(CANVAS_TABLES)]

    Base.metadata.create_all(engine, tables=tables)

    assert CANVAS_TABLES <= set(inspect(engine).get_table_names())
    assert {
        "uq_canvas_execution_tasks_generation",
        "uq_canvas_execution_tasks_completion",
        "uq_canvas_execution_tasks_video_generation",
    } <= {
        index["name"] for index in inspect(engine).get_indexes("canvas_execution_tasks")
    }


def _load_migration():
    root = Path(__file__).resolve().parents[3]
    path = root / "apps/api/alembic/versions/0044_infinite_canvas.py"
    spec = importlib.util.spec_from_file_location(
        "migration_0044_infinite_canvas", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


def test_canvas_migration_static_guard_and_sqlite_round_trip() -> None:
    migration, path = _load_migration()
    source = path.read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0043_billing_consistency"' in source
    assert '"canvas_documents"' in source
    assert '"canvas_task_terminal_receipts"' in source
    assert '"ck_canvas_execution_tasks_task_owner"' in source
    assert "sqlite_where=" in source
    assert "fk_canvas_documents_last_version" in source

    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        for table_name in (
            "users",
            "conversations",
            "images",
            "videos",
            "generations",
            "completions",
            "video_generations",
        ):
            connection.execute(
                text(f"CREATE TABLE {table_name} (id VARCHAR(36) PRIMARY KEY)")
            )
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        migration.upgrade()

        assert CANVAS_TABLES <= set(inspect(connection).get_table_names())
        foreign_keys = inspect(connection).get_foreign_keys("canvas_documents")
        assert any(
            item["name"] == "fk_canvas_documents_last_version"
            and item["referred_table"] == "canvas_versions"
            for item in foreign_keys
        )
        connection.execute(text("INSERT INTO users (id) VALUES ('user-1')"))
        connection.execute(
            text(
                """
                INSERT INTO canvas_documents (
                    id,
                    user_id,
                    title,
                    description,
                    graph_schema_version,
                    graph_jsonb,
                    revision
                )
                VALUES (
                    'canvas-1',
                    'user-1',
                    '',
                    '',
                    1,
                    '{}',
                    1
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO canvas_versions (
                    id,
                    canvas_id,
                    user_id,
                    source_revision,
                    version_no,
                    kind,
                    graph_schema_version,
                    graph_hash,
                    graph_jsonb,
                    selection_hash
                )
                VALUES (
                    'version-1',
                    'canvas-1',
                    'user-1',
                    1,
                    1,
                    'named',
                    1,
                    :graph_hash,
                    '{}',
                    :selection_hash
                )
                """
            ),
            {"graph_hash": "a" * 64, "selection_hash": "b" * 64},
        )
        connection.execute(
            text(
                """
                UPDATE canvas_documents
                SET last_version_id = 'version-1'
                WHERE id = 'canvas-1'
                """
            )
        )

        migration.downgrade()
        assert not (CANVAS_TABLES & set(inspect(connection).get_table_names()))


def test_canvas_models_public_classes_match_the_ten_persistence_tables() -> None:
    classes = {
        CanvasDocument,
        CanvasMutation,
        CanvasVersion,
        CanvasRun,
        CanvasNodeExecution,
        CanvasExecutionTask,
        CanvasTaskTerminalReceipt,
        CanvasNodeSelection,
        CanvasAssetRef,
        CanvasRunEvent,
    }

    assert {model.__tablename__ for model in classes} == CANVAS_TABLES
