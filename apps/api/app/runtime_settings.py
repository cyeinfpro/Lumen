"""API 侧 system_settings 帮助层。

DB 中只持久化 SUPPORTED_SETTINGS 列表里的 key。读：DB 优先，env fallback；
写：upsert，value="" 表示删除该 key（让该 key fallback 到 env）。
"""

from __future__ import annotations

import os
import logging
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import SystemSetting
from lumen_core.runtime_settings import (
    SUPPORTED_SETTINGS,
    SettingSpec,
    get_spec,
    parse_value,
)
from lumen_core.schemas import SystemSettingItem

logger = logging.getLogger(__name__)

_IMAGE_PRIMARY_ROUTE_MAPPING: dict[str, tuple[str, str]] = {
    "responses": ("auto", "responses"),
    "image2": ("auto", "image2"),
    "image_jobs": ("image_jobs_only", "responses"),
    "dual_race": ("auto", "dual_race"),
}


def image_primary_route_to_parts(raw: str | None) -> tuple[str, str]:
    value = (raw or "").strip().lower()
    return _IMAGE_PRIMARY_ROUTE_MAPPING.get(value, ("auto", "responses"))


def _expand_legacy_image_route_pairs(
    items: Iterable[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Keep old clients that write image.primary_route in sync with new keys."""
    result = list(items)
    keys = {key for key, _ in result}
    for key, value in list(result):
        if key != "image.primary_route" or value == "":
            continue
        channel, engine = image_primary_route_to_parts(value)
        if "image.channel" not in keys:
            result.append(("image.channel", channel))
            keys.add("image.channel")
        if "image.engine" not in keys:
            result.append(("image.engine", engine))
            keys.add("image.engine")
    return result


async def get_setting(db: AsyncSession, spec: SettingSpec) -> str | None:
    """DB 优先，env fallback。返回 raw str；调用方自行 parse。"""
    row = (
        await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == spec.key)
        )
    ).scalar_one_or_none()
    if row is not None and row != "":
        return row
    env_val = os.environ.get(spec.env_fallback)
    if env_val is not None and env_val != "":
        return env_val
    return None


async def get_settings_view(db: AsyncSession) -> list[SystemSettingItem]:
    """遍历 SUPPORTED_SETTINGS，组装管理员视图。

    敏感 key 的 value 在响应里 mask 为 None，但 has_value=true 表示已配置。
    """
    rows = (
        await db.execute(
            select(SystemSetting.key, SystemSetting.value).where(
                SystemSetting.key.in_([s.key for s in SUPPORTED_SETTINGS])
            )
        )
    ).all()
    db_map: dict[str, str | None] = {k: v for k, v in rows}

    items: list[SystemSettingItem] = []
    for spec in SUPPORTED_SETTINGS:
        db_val = db_map.get(spec.key)
        env_val = os.environ.get(spec.env_fallback)
        # has_value: DB 非空 OR env 非空
        has_value = bool((db_val is not None and db_val != "")) or bool(
            env_val is not None and env_val != ""
        )
        # value 显示：DB 优先；敏感字段 mask 为 None
        if spec.sensitive:
            display_val: str | None = None
        else:
            if db_val is not None and db_val != "":
                display_val = db_val
            else:
                display_val = None
        items.append(
            SystemSettingItem(
                key=spec.key,
                value=display_val,
                has_value=has_value,
                is_sensitive=spec.sensitive,
                description=spec.description,
            )
        )
    return items


async def update_settings(
    db: AsyncSession, items: Iterable[tuple[str, str]]
) -> None:
    """批量 upsert：value="" 表示删除该 key。

    type-check 失败应在调用方先做（route 层），这里假设输入合法 key。
    单个事务，调用方负责 commit。
    """
    items_list = _expand_legacy_image_route_pairs(items)
    validated_items: list[tuple[str, str]] = []
    # 类型预校验
    for key, value in items_list:
        spec = get_spec(key)
        if spec is None:
            raise ValueError(f"unknown setting key: {key}")
        if value != "":
            parsed = parse_value(spec, value)  # 会抛 ValueError
        validated_items.append((key, value))

    for key, value in validated_items:
        if value == "":
            await db.execute(delete(SystemSetting).where(SystemSetting.key == key))
            continue

        existing = (
            await db.execute(
                select(SystemSetting).where(SystemSetting.key == key)
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(SystemSetting(key=key, value=value))
        else:
            existing.value = value


async def migrate_image_primary_route(db: AsyncSession) -> bool:
    """Backfill image.channel + image.engine from the deprecated primary_route.

    Idempotent: if either new key already exists in DB, no write is performed.
    The old image.primary_route row is deliberately retained for rollback.
    """
    rows = (
        await db.execute(
            select(SystemSetting.key, SystemSetting.value).where(
                SystemSetting.key.in_(
                    ["image.primary_route", "image.channel", "image.engine"]
                )
            )
        )
    ).all()
    values = {str(key): value for key, value in rows}
    old = values.get("image.primary_route")
    if not old or values.get("image.channel") or values.get("image.engine"):
        return False

    channel, engine = image_primary_route_to_parts(old)
    db.add(SystemSetting(key="image.channel", value=channel))
    db.add(SystemSetting(key="image.engine", value=engine))
    logger.info(
        "migrated image.primary_route=%s -> channel=%s engine=%s",
        old,
        channel,
        engine,
    )
    return True


__all__ = [
    "get_setting",
    "get_settings_view",
    "image_primary_route_to_parts",
    "migrate_image_primary_route",
    "update_settings",
]
