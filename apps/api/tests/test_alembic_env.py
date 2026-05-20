from __future__ import annotations

from pathlib import Path


def test_alembic_commits_timeout_setup_before_migration_transaction() -> None:
    """Guard against SQLAlchemy 2 autobegin rolling back successful migrations."""
    source = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text(
        encoding="utf-8"
    )
    timeout_pos = source.index("SET statement_timeout")
    commit_pos = source.index("connection.commit()", timeout_pos)
    configure_pos = source.index("context.configure(connection=connection", timeout_pos)

    assert timeout_pos < commit_pos < configure_pos


def test_users_active_email_unique_migration_uses_safe_postgres_ordering() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0025_users_active_email_unique.py"
    ).read_text(encoding="utf-8")

    upgrade_pos = source.index("def upgrade()")
    pg_create_pos = source.index("postgresql_concurrently=True", upgrade_pos)
    drop_constraint_pos = source.index(
        'op.drop_constraint("users_email_key"', upgrade_pos
    )
    downgrade_pos = source.index("def downgrade()")
    duplicate_guard_pos = source.index("duplicate is not None", downgrade_pos)
    recreate_constraint_pos = source.index(
        'op.create_unique_constraint("users_email_key"', downgrade_pos
    )
    concurrent_drop_pos = source.index("postgresql_concurrently=True", downgrade_pos)

    assert "autocommit_block()" in source
    assert pg_create_pos < drop_constraint_pos
    assert duplicate_guard_pos < recreate_constraint_pos < concurrent_drop_pos
