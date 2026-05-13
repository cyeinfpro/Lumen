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

