"""Fix Volcano Seedance 2.0 Mini upstream model id.

Revision ID: 0037_seedance_mini_alias
Revises: 0036_seedance_20_mini
Create Date: 2026-06-28
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


revision: str = "0037_seedance_mini_alias"
down_revision: str | None = "0036_seedance_20_mini"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SETTING_KEY = "video.providers"
_OLD_MODEL = "dreamina-seedance-2-0-mini-260615"
_NEW_MODEL = "doubao-seedance-2-0-mini-260615"


def _load(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _provider_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("providers"), list):
        return value["providers"]
    return []


def _replace_volcano_model(value: Any, *, old: str, new: str) -> bool:
    changed = False
    for item in _provider_items(value):
        if not isinstance(item, dict):
            continue
        raw_kind = item.get("kind", "volcano")
        kind = raw_kind.strip().lower() if isinstance(raw_kind, str) else ""
        if kind != "volcano":
            continue
        models = item.get("models")
        if not isinstance(models, dict):
            continue
        for key, model in list(models.items()):
            if isinstance(key, str) and model == old:
                models[key] = new
                changed = True
    return changed


def _current_setting() -> str | None:
    raw = (
        op.get_bind()
        .execute(
            sa.text("SELECT value FROM system_settings WHERE key = :key"),
            {"key": _SETTING_KEY},
        )
        .scalar_one_or_none()
    )
    return raw if isinstance(raw, str) else None


def _write_setting(value: str) -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE system_settings
            SET value = :value,
                updated_at = CURRENT_TIMESTAMP
            WHERE key = :key
            """
        ),
        {"key": _SETTING_KEY, "value": value},
    )


def _replace_setting_model(*, old: str, new: str) -> None:
    value = _load(_current_setting())
    if value is None:
        return
    if _replace_volcano_model(value, old=old, new=new):
        _write_setting(_dump(value))


def upgrade() -> None:
    _replace_setting_model(old=_OLD_MODEL, new=_NEW_MODEL)


def downgrade() -> None:
    _replace_setting_model(old=_NEW_MODEL, new=_OLD_MODEL)
