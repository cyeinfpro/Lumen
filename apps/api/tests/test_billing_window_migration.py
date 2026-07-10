from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "apps/api/alembic/versions/0041_billing_window_usage_ledger.py"


def test_billing_window_migration_backfills_recent_credential_usage() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "INSERT INTO billing_window_usage_events" in source
    assert "wallet_transactions" in source
    assert "api_key_id" in source
    assert "interval '7 days'" in source
    assert "CASE" in source
    assert "~ '^[0-9]+$'" in source
    assert "ELSE GREATEST(-amount_micro, 0)" in source
