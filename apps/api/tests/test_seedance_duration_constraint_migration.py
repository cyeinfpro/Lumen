from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0066_seedance_duration_constraint_online.py"
)


def test_seedance_duration_constraint_uses_online_postgres_validation() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "0066_seedance_duration_online"' in source
    assert 'down_revision: str | None = "0065_seedance_25_defaults"' in source
    assert "postgresql_not_valid=True" in source
    assert "VALIDATE CONSTRAINT" in source
    assert "RENAME CONSTRAINT" not in source
