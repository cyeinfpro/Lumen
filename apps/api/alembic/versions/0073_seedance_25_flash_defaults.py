"""Add Seedance 2.5 Flash mappings, separate prices and conservative holds.

Official contract and price sources, verified 2026-11-16:
https://docs.volcengine.com/docs/ark/seedance-2-5?lang=zh
https://console.volcengine.com/ark/region:cn-beijing/docs/model-pricing
Configuration-only migration. Existing operator overrides are preserved.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from alembic import op
import sqlalchemy as sa

revision = "0073_seedance_25_flash_defaults"
down_revision = "0072_agent_runtime_contracts"
branch_labels = None
depends_on = None

_MODEL = "seedance-2.5-flash"
_UPSTREAM_MODEL = "doubao-seedance-2-5-flash-261115"
_ESTIMATE_SETTING_KEY = "video.token_hold_estimates"
_PROVIDER_SETTING_KEY = "video.providers"
_MAX_OUTPUT_PIXELS = {"480p": 992 * 432, "720p": 1112 * 834}
_ACTIONS = ("t2v", "i2v", "reference", "reference_image", "reference_video")
_PRICE_ROWS = tuple(
    (f"{action}_{resolution}", 28_000_000 if action == "reference_video" else 46_000_000)
    for action in _ACTIONS for resolution in _MAX_OUTPUT_PIXELS
)


def _load(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _official_estimates() -> dict[str, dict[str, int]]:
    # Reserve the full allowed 30 seconds of input video, across all aspect ratios.
    # Exact settlement still uses the upstream returned completion_tokens.
    return {
        action: {
            f"{resolution}:{duration}": (
                (duration + (30 if action == "reference_video" else 0))
                * pixels * 24 + 1023
            ) // 1024
            for resolution, pixels in _MAX_OUTPUT_PIXELS.items()
            for duration in range(4, 31)
        }
        for action in _ACTIONS
    }


def _merge_estimates(raw: str | None) -> str | None:
    value = _load(raw)
    if value is None and raw is None:
        value = {}
    if not isinstance(value, dict):
        return raw  # Never replace malformed operator configuration.
    model = value.setdefault(_MODEL, {})
    if not isinstance(model, dict):
        return raw
    for action, estimates in _official_estimates().items():
        action_map = model.setdefault(action, {})
        if isinstance(action_map, dict):
            for key, estimate in estimates.items():
                action_map.setdefault(key, estimate)
    return _dump(value)


def _merge_provider_models(raw: str | None) -> str | None:
    value = _load(raw)
    providers = value if isinstance(value, list) else (
        value.get("providers", []) if isinstance(value, dict) else []
    )
    if not isinstance(providers, list):
        return raw
    changed = False
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("kind", "volcano") != "volcano":
            continue
        models = provider.get("models")
        if not isinstance(models, dict) or _MODEL in models:
            continue  # Keep generic custom mappings authoritative.
        for action in ("t2v", "i2v", "reference"):
            key = f"{_MODEL}:{action}"
            if key not in models:
                models[key] = _UPSTREAM_MODEL
                changed = True
    return _dump(value) if changed else raw


def _setting(key: str) -> str | None:
    return op.get_bind().execute(
        sa.text("SELECT value FROM system_settings WHERE key = :key"), {"key": key}
    ).scalar_one_or_none()


def _save_setting(key: str, value: str | None) -> None:
    if value is None:
        return
    op.get_bind().execute(sa.text("""
        INSERT INTO system_settings (id, key, value) VALUES (:id, :key, :value)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
            updated_at = CURRENT_TIMESTAMP
    """), {"id": str(uuid5(NAMESPACE_URL, "lumen/settings/" + key)), "key": key, "value": value})


def upgrade() -> None:
    for variant, price in _PRICE_ROWS:
        op.get_bind().execute(sa.text("""
            INSERT INTO pricing_rules
                (id, scope, key, variant, unit, price_micro, enabled, note)
            VALUES (:id, 'video', :key, :variant, 'per_mtoken', :price, true, :note)
            ON CONFLICT (scope, key, variant, unit) DO NOTHING
        """), {
            "id": str(uuid5(NAMESPACE_URL, f"lumen/video/{_MODEL}/{variant}")),
            "key": _MODEL, "variant": variant, "price": price,
            "note": "Volcengine official list price 2026-11-13: Seedance 2.5 Flash",
        })
    _save_setting(_ESTIMATE_SETTING_KEY, _merge_estimates(_setting(_ESTIMATE_SETTING_KEY)))
    _save_setting(_PROVIDER_SETTING_KEY, _merge_provider_models(_setting(_PROVIDER_SETTING_KEY)))


def downgrade() -> None:
    # Configuration may have been customized or used by completed billed jobs.
    # Retain it rather than deleting operator data when rolling code back.
    pass
