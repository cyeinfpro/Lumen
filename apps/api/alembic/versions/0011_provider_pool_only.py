"""Migrate upstream base/api settings into provider pool.

Revision ID: 0011_provider_pool_only
Revises: 0010_upstream_settings
Create Date: 2026-04-26
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import uuid

import sqlalchemy as sa
from alembic import op


revision: str = "0011_provider_pool_only"
down_revision: str | None = "0010_upstream_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_BASE_URL = "https://api.example.com"


def _read_setting(conn: sa.Connection, key: str) -> str | None:
    return conn.execute(
        sa.text("SELECT value FROM system_settings WHERE key = :key"),
        {"key": key},
    ).scalar_one_or_none()


def _upsert_setting(conn: sa.Connection, key: str, value: str) -> None:
    existing = conn.execute(
        sa.text("SELECT id FROM system_settings WHERE key = :key"),
        {"key": key},
    ).scalar_one_or_none()
    if existing is None:
        conn.execute(
            sa.text(
                """
                INSERT INTO system_settings (id, key, value)
                VALUES (:id, :key, :value)
                """
            ),
            {"id": str(uuid.uuid4()), "key": key, "value": value},
        )
    else:
        conn.execute(
            sa.text(
                """
                UPDATE system_settings
                SET value = :value, updated_at = now()
                WHERE key = :key
                """
            ),
            {"key": key, "value": value},
        )


def _parse_providers(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("providers system setting is not valid JSON") from exc
    if not isinstance(value, list):
        raise RuntimeError("providers system setting is not a JSON array")
    return [item for item in value if isinstance(item, dict)]


def _unique_name(existing: set[str]) -> str:
    if "default" not in existing:
        return "default"
    if "upstream-default" not in existing:
        return "upstream-default"
    i = 2
    while f"upstream-default-{i}" in existing:
        i += 1
    return f"upstream-default-{i}"


def _append_default_provider(
    providers: list[dict],
    *,
    base_url: str,
    api_key: str,
) -> list[dict]:
    base = base_url.strip().rstrip("/") or _DEFAULT_BASE_URL
    key = api_key.strip()
    if not key:
        return providers
    for item in providers:
        item_base = str(item.get("base_url") or "").strip().rstrip("/")
        item_key = str(item.get("api_key") or "").strip()
        if item_base == base and item_key == key:
            return providers
    result = list(providers)
    result.append(
        {
            "name": _unique_name(
                {
                    item.get("name", "")
                    for item in result
                    if isinstance(item.get("name"), str)
                }
            ),
            "base_url": base,
            "api_key": key,
            "priority": 0,
            "weight": 1,
            "enabled": True,
        }
    )
    return result


def upgrade() -> None:
    conn = op.get_bind()
    providers = _parse_providers(_read_setting(conn, "providers"))
    api_key = _read_setting(conn, "upstream.api_key")
    base_url = _read_setting(conn, "upstream.base_url") or _DEFAULT_BASE_URL

    merged = _append_default_provider(
        providers,
        base_url=base_url,
        api_key=api_key or "",
    )
    if merged != providers:
        _upsert_setting(conn, "providers", json.dumps(merged, ensure_ascii=False))

    conn.execute(
        sa.text(
            """
            DELETE FROM system_settings
            WHERE key IN ('upstream.base_url', 'upstream.api_key')
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    providers = _parse_providers(_read_setting(conn, "providers"))
    if not providers:
        return

    provider = next(
        (
            item
            for item in providers
            if item.get("name") in {"default", "upstream-default"}
        ),
        providers[0],
    )
    base_url = str(provider.get("base_url") or "").strip()
    api_key = str(provider.get("api_key") or "").strip()
    if base_url:
        _upsert_setting(conn, "upstream.base_url", base_url)
    if api_key:
        _upsert_setting(conn, "upstream.api_key", api_key)
