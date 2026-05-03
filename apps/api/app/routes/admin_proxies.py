"""管理后台「代理池」专用路由。

数据源：`system_settings.providers` JSON 中的 `proxies[]` 数组（与 `routes/providers.py`
共享同一行 DB）。

- GET   /admin/proxies                列出 + 附带 Redis 里的健康状态（last_latency_ms 等）
- PUT   /admin/proxies                CRUD（仅替换 proxies 数组，items 不动；password
                                       留空 = 保留旧值，避免编辑时擦掉）
- POST  /admin/proxies/test/{name}    用指定 proxy 发 HEAD 到 settings 配的 test_target，
                                       返回 latency_ms 并写到 Redis 健康缓存
- POST  /admin/proxies/test-all       批量测全部
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import SystemSetting
from lumen_core.providers import (
    ProviderProxyDefinition,
    parse_proxy_item,
)
from lumen_core.runtime_settings import get_spec, validate_providers
from lumen_core.schemas import ProviderProxyIn

from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..proxy_pool import (
    DEFAULT_TEST_TARGET,
    get_health,
    measure_latency,
    set_health,
)
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ._admin_common import admin_http as _http, write_admin_audit
from .providers import _parse_config, _read_providers

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/proxies", tags=["admin-proxies"])


class ProxyHealthOut(BaseModel):
    name: str
    type: str
    host: str
    port: int
    username: str | None = None
    private_key_path: str | None = None
    has_password: bool = False  # 编辑表单用：知道是否有旧密码可保留
    enabled: bool = True
    last_latency_ms: float | None = None
    last_tested_at: str | None = None
    last_target: str | None = None
    in_cooldown: bool = False


class ProxyListOut(BaseModel):
    items: list[ProxyHealthOut]
    test_target: str


class ProxyTestOut(BaseModel):
    name: str
    target: str
    latency_ms: float
    ok: bool
    error: str | None = None


async def _resolve_test_target(db: AsyncSession) -> str:
    spec = get_spec("proxies.test_target")
    if spec is None:
        return DEFAULT_TEST_TARGET
    raw = await get_setting(db, spec)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return DEFAULT_TEST_TARGET


async def _load_proxies(db: AsyncSession) -> list[ProviderProxyDefinition]:
    raw, _source = await _read_providers(db)
    if not raw:
        return []
    _items, proxy_raw = _parse_config(raw)
    out: list[ProviderProxyDefinition] = []
    for i, p in enumerate(proxy_raw):
        try:
            out.append(parse_proxy_item(p, index=i))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip bad proxy idx=%d err=%s", i, exc)
    return out


async def _load_full_config(db: AsyncSession) -> dict:
    """读 system_settings.providers 的原始 JSON（含 password 等敏感字段）。"""
    raw, _source = await _read_providers(db)
    if not raw:
        return {"providers": [], "proxies": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"providers": [], "proxies": []}
    if not isinstance(data, dict):
        return {"providers": [], "proxies": []}
    if "providers" not in data:
        data["providers"] = []
    if "proxies" not in data:
        data["proxies"] = []
    return data


# ---------- list ----------


@router.get("", response_model=ProxyListOut)
async def list_proxies(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProxyListOut:
    config = await _load_full_config(db)
    raw_proxies = config.get("proxies") or []
    parsed: list[ProviderProxyDefinition] = []
    has_password_by_name: dict[str, bool] = {}
    username_by_name: dict[str, str | None] = {}
    pkpath_by_name: dict[str, str | None] = {}
    for i, p in enumerate(raw_proxies):
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "")
        has_password_by_name[name] = bool((p.get("password") or "").strip()) if isinstance(p.get("password"), str) else False
        username_by_name[name] = p.get("username") if isinstance(p.get("username"), str) and p.get("username") else None
        pkpath_by_name[name] = p.get("private_key_path") if isinstance(p.get("private_key_path"), str) and p.get("private_key_path") else None
        try:
            parsed.append(parse_proxy_item(p, index=i))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skip bad proxy idx=%d err=%s", i, exc)

    target = await _resolve_test_target(db)
    redis = get_redis()
    items: list[ProxyHealthOut] = []
    for p in parsed:
        h = await get_health(redis, p.name)
        in_cd = bool(await redis.exists(f"lumen:proxy:cooldown:{p.name}"))
        items.append(
            ProxyHealthOut(
                name=p.name,
                type=p.protocol,
                host=p.host,
                port=p.port,
                username=username_by_name.get(p.name),
                private_key_path=pkpath_by_name.get(p.name),
                has_password=has_password_by_name.get(p.name, False),
                enabled=p.enabled,
                last_latency_ms=h.get("last_latency_ms") if isinstance(h.get("last_latency_ms"), (int, float)) else None,
                last_tested_at=h.get("last_tested_at"),
                last_target=h.get("last_target"),
                in_cooldown=in_cd,
            )
        )
    return ProxyListOut(items=items, test_target=target)


# ---------- update (CRUD) ----------


class ProxiesUpdateIn(BaseModel):
    items: list[ProviderProxyIn]


@router.put(
    "",
    response_model=ProxyListOut,
    dependencies=[Depends(verify_csrf)],
)
async def update_proxies(
    body: ProxiesUpdateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProxyListOut:
    """替换 proxies 数组。providers items 不动。

    敏感字段保留：password 留空 → 用旧值；private_key_path 留空 → 用旧值。
    其它字段（host/port/username/enabled）以请求里的为准。
    """
    config = await _load_full_config(db)

    # 旧 proxies map：用 name 索引，便于保留 password / private_key
    old_by_name: dict[str, dict] = {}
    for p in config.get("proxies") or []:
        if isinstance(p, dict) and isinstance(p.get("name"), str):
            old_by_name[p["name"]] = p

    # 名称去重
    seen_names: set[str] = set()
    for it in body.items:
        n = it.name.strip()
        if not n:
            raise _http("invalid_proxy", "代理名称不能为空", 422)
        if n in seen_names:
            raise _http("duplicate_proxy", f"代理名称重复：{n}", 422)
        seen_names.add(n)

    # 构造新 proxies；保留旧 password / private_key_path 当请求侧留空
    new_proxies: list[dict] = []
    for it in body.items:
        d = it.model_dump()
        d["name"] = it.name.strip()
        old = old_by_name.get(d["name"]) or {}
        if not (d.get("password") or "").strip() and old.get("password"):
            d["password"] = old["password"]
        if not d.get("private_key_path") and old.get("private_key_path"):
            d["private_key_path"] = old["private_key_path"]
        new_proxies.append(d)

    config["proxies"] = new_proxies
    new_raw = json.dumps(config, ensure_ascii=False)

    # 用现有 validator 跑校验（会同时校 providers 和 proxies）
    try:
        validated = validate_providers(new_raw)
    except ValueError as exc:
        raise _http("invalid_config", str(exc), 422) from exc

    existing = (
        await db.execute(
            select(SystemSetting).where(SystemSetting.key == "providers")
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(SystemSetting(key="providers", value=validated))
    else:
        existing.value = validated

    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.proxies.update",
        details={"count": len(new_proxies), "names": sorted(seen_names)},
    )
    await db.commit()
    logger.info("admin proxies updated count=%d by user=%s", len(new_proxies), admin.id)

    return await list_proxies(_admin=admin, db=db)


# ---------- test latency ----------


class ProxyTestIn(BaseModel):
    target: str | None = None  # 不填则用 settings 配的；都没就用默认


@router.post(
    "/test/{name}",
    response_model=ProxyTestOut,
    dependencies=[Depends(verify_csrf)],
)
async def test_proxy(
    name: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: ProxyTestIn | None = None,
) -> ProxyTestOut:
    proxies = await _load_proxies(db)
    target_proxy = next((p for p in proxies if p.name == name), None)
    if target_proxy is None:
        raise _http("not_found", f"proxy '{name}' not found", 404)
    target = (body.target.strip() if body and body.target else "") or await _resolve_test_target(db)

    redis = get_redis()
    latency_ms, err = await measure_latency(target_proxy, target=target)
    ok = err is None
    if ok:
        await set_health(redis, name, latency_ms=latency_ms, target=target)
    logger.info(
        "admin proxy test name=%s target=%s ok=%s latency_ms=%.1f err=%s",
        name, target, ok, latency_ms, err,
    )
    return ProxyTestOut(
        name=name,
        target=target,
        latency_ms=round(latency_ms, 1),
        ok=ok,
        error=err,
    )


# ---------- test all ----------


@router.post(
    "/test-all",
    response_model=list[ProxyTestOut],
    dependencies=[Depends(verify_csrf)],
)
async def test_all_proxies(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: ProxyTestIn | None = None,
) -> list[ProxyTestOut]:
    proxies = await _load_proxies(db)
    target = (body.target.strip() if body and body.target else "") or await _resolve_test_target(db)
    redis = get_redis()

    async def _one(p: ProviderProxyDefinition) -> ProxyTestOut:
        latency_ms, err = await measure_latency(p, target=target)
        ok = err is None
        if ok:
            await set_health(redis, p.name, latency_ms=latency_ms, target=target)
        return ProxyTestOut(
            name=p.name,
            target=target,
            latency_ms=round(latency_ms, 1),
            ok=ok,
            error=err,
        )

    return await asyncio.gather(*[_one(p) for p in proxies])
