"""Expand Agent session references and repair GPT-5.6 context defaults.

Revision ID: 0070_agent_session_context
Revises: 0069_agent_foundation
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision: str = "0070_agent_session_context"
down_revision: str | None = "0069_agent_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_CONTEXT_WINDOW = 128_000
_GPT_56_CONTEXT_WINDOW = 272_000


def _canonical_model_id(model_id: Any) -> str:
    value = str(model_id or "").strip().lower()
    return value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _provider_supports_model(
    provider: dict[str, Any],
    model_id: str,
) -> bool:
    expected = _canonical_model_id(model_id)
    models = provider.get("agent_models")
    return isinstance(models, list) and any(
        isinstance(model, str) and _canonical_model_id(model) == expected
        for model in models
    )


def _upgrade_provider_payload(raw: str, *, default_model: str) -> str | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    providers = payload.get("providers") if isinstance(payload, dict) else payload
    if (
        not isinstance(providers, list)
        or not _canonical_model_id(default_model).startswith("gpt-5.6")
    ):
        return None
    changed = False
    for provider in providers:
        if (
            not isinstance(provider, dict)
            or not _provider_supports_model(provider, default_model)
        ):
            continue
        value = provider.get("agent_context_window")
        if value == _LEGACY_CONTEXT_WINDOW:
            provider["agent_context_window"] = _GPT_56_CONTEXT_WINDOW
            changed = True
    if not changed:
        return None
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def upgrade() -> None:
    op.alter_column(
        "agent_runs",
        "reasoning_effort",
        server_default="max",
        existing_type=sa.String(length=16),
    )
    bind = op.get_bind()
    raw = bind.execute(
        sa.text("SELECT value FROM system_settings WHERE key = 'providers'")
    ).scalar_one_or_none()
    default_model = bind.execute(
        sa.text(
            "SELECT value FROM system_settings "
            "WHERE key = 'upstream.default_model'"
        )
    ).scalar_one_or_none()
    if not isinstance(raw, str) or not isinstance(default_model, str):
        return
    upgraded = _upgrade_provider_payload(raw, default_model=default_model)
    if upgraded is None:
        return
    bind.execute(
        sa.text(
            """
            UPDATE system_settings
            SET value = :value,
                updated_at = CURRENT_TIMESTAMP
            WHERE key = 'providers'
            """
        ),
        {"value": upgraded},
    )


def downgrade() -> None:
    op.alter_column(
        "agent_runs",
        "reasoning_effort",
        server_default=None,
        existing_type=sa.String(length=16),
    )
    # Context values may be operator-edited after upgrade; never clobber them.
