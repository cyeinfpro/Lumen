"""Fail-closed guard for destructive Telegram schema downgrades."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import sqlalchemy as sa


TELEGRAM_CONTROL_REVISION = "0060_telegram_delivery_control"
_TELEGRAM_TABLES = (
    "telegram_control_commands",
    "telegram_delivery_attempts",
    "telegram_delivery_quarantines",
)


def _write_export(bind: Any, target: Path) -> None:
    if target.exists():
        raise RuntimeError(
            f"refusing to overwrite existing Telegram migration export: {target}"
        )
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, object] = {}
    for table_name in _TELEGRAM_TABLES:
        rows = bind.execute(
            sa.text(f"SELECT * FROM {table_name}")
        ).mappings().all()
        payload[table_name] = [dict(row) for row in rows]

    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                payload,
                default=str,
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def guard_telegram_downgrade(
    bind: Any,
    *,
    pending_revision_ids: set[str],
) -> None:
    """Require an explicit export and reject unresolved quarantine loss."""
    if TELEGRAM_CONTROL_REVISION not in pending_revision_ids:
        return

    target_raw = os.environ.get("LUMEN_MIGRATION_EXPORT_PATH", "").strip()
    if not target_raw:
        raise RuntimeError(
            "Telegram downgrade requires the explicit export command; "
            "set LUMEN_MIGRATION_EXPORT_PATH"
        )

    unresolved = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_delivery_quarantines "
            "WHERE status <> 'resolved'"
        )
    ).scalar_one()
    if unresolved:
        raise RuntimeError(
            "refusing downgrade with unresolved Telegram quarantines; "
            "resolve them before rollback"
        )

    active_commands = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_control_commands "
            "WHERE status IN ('pending','published')"
        )
    ).scalar_one()
    if active_commands:
        raise RuntimeError("refusing downgrade with active Telegram commands")

    uncertain_deliveries = bind.execute(
        sa.text(
            "SELECT count(*) FROM telegram_delivery_attempts "
            "WHERE state IN ('dispatching','delivery_result_unknown')"
        )
    ).scalar_one()
    if uncertain_deliveries:
        raise RuntimeError(
            "refusing downgrade with uncertain Telegram deliveries"
        )

    command_columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("telegram_control_commands")
    }
    if "effect_status" in command_columns:
        active_effects = bind.execute(
            sa.text(
                "SELECT count(*) FROM telegram_control_commands "
                "WHERE effect_status IN ('pending','running')"
            )
        ).scalar_one()
        if active_effects:
            raise RuntimeError(
                "refusing downgrade with active Telegram control effects"
            )

    _write_export(bind, Path(target_raw))
