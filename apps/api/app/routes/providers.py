"""管理员 Provider Pool 管理与探活。

GET  /admin/providers       — 列出 provider（API Key 脱敏）
PUT  /admin/providers       — 结构化保存（支持 key 保留）
POST /admin/providers/probe — 手动探活（支持按名称过滤）
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderProxyDefinition,
    build_legacy_provider,
    endpoint_kind_allowed,
    parse_proxy_item,
    resolve_provider_proxy_url,
)
from lumen_core.models import SystemSetting
from lumen_core.runtime_settings import get_spec, validate_providers
from lumen_core.schemas import (
    ProviderItemOut,
    ProviderProbeResult,
    ProviderProxyOut,
    ProviderStatsItem,
    ProviderStatsOut,
    ProvidersOut,
    ProvidersProbeIn,
    ProvidersProbeOut,
    ProvidersUpdateIn,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import AdminUser, verify_csrf

router = APIRouter(prefix="/admin/providers", tags=["admin-providers"])

_PROBE_TIMEOUT_S = 15.0
_PROVIDERS_MAX_LEN = 65536
_PROBE_MODEL = "gpt-5.4-mini"
_PROBE_INSTRUCTIONS = "You are a precise calculator. Return only the final integer."
_PROBE_INPUT = (
    "What is 99 times 99? Reply with only the integer result, "
    "no words, no explanation."
)


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


def _responses_url(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


def _mask_key(key: str) -> str:
    if not key or len(key) <= 8:
        return "****"
    return key[:4] + "..." + key[-4:]


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def _normalize_proxy_type(value: str, *, fallback: bool = False) -> str:
    raw = (value or "socks5").strip().lower()
    if raw in {"s5", "socks", "socks5", "socks5h"}:
        return "socks5"
    if raw == "ssh":
        return "ssh"
    return "socks5" if fallback else raw


def _safe_int(value: object, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _legacy_env_providers_raw() -> str | None:
    legacy = build_legacy_provider(
        base_url=(
            os.environ.get("UPSTREAM_BASE_URL")
            or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    if legacy is None:
        return None
    return json.dumps(
        [
            {
                "name": legacy.name,
                "base_url": legacy.base_url,
                "api_key": legacy.api_key,
                "priority": legacy.priority,
                "weight": legacy.weight,
                "enabled": legacy.enabled,
                "image_jobs_enabled": False,
                "image_jobs_endpoint": "auto",
                "image_jobs_endpoint_lock": False,
                "image_jobs_base_url": "",
                "image_concurrency": 1,
            }
        ],
        ensure_ascii=False,
    )


async def _read_providers(
    db: AsyncSession,
) -> tuple[str | None, str]:
    """返回 (raw_json, source)。source 为 "db" | "env" | "none"。"""
    row = (
        await db.execute(
            select(SystemSetting.value).where(SystemSetting.key == "providers")
        )
    ).scalar_one_or_none()
    if row is not None and row != "":
        return row, "db"
    spec = get_spec("providers")
    if spec:
        env_val = os.environ.get(spec.env_fallback)
        if env_val is not None and env_val != "":
            return env_val, "env"
    legacy = _legacy_env_providers_raw()
    if legacy is not None:
        return legacy, "env"
    return None, "none"


def _parse_config(raw: str) -> tuple[list[dict], list[dict]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [], []
    if isinstance(value, list):
        return [it for it in value if isinstance(it, dict)], []
    if not isinstance(value, dict):
        return [], []
    providers = value.get("providers", [])
    proxies = value.get("proxies", [])
    if not isinstance(providers, list):
        providers = []
    if not isinstance(proxies, list):
        proxies = []
    return (
        [it for it in providers if isinstance(it, dict)],
        [it for it in proxies if isinstance(it, dict)],
    )


def _parse_items(raw: str) -> list[dict]:
    items, _ = _parse_config(raw)
    return items


def _to_out(it: dict, idx: int) -> ProviderItemOut:
    endpoint = _normalize_image_jobs_endpoint(it.get("image_jobs_endpoint"))
    return ProviderItemOut(
        name=it.get("name") or f"provider-{idx}",
        base_url=it.get("base_url", ""),
        api_key_hint=_mask_key(it.get("api_key", "")),
        priority=_safe_int(it.get("priority"), 0),
        weight=_safe_int(it.get("weight"), 1, minimum=1),
        enabled=bool(it.get("enabled", True)),
        proxy=it.get("proxy") if isinstance(it.get("proxy"), str) else None,
        image_jobs_enabled=bool(it.get("image_jobs_enabled", False)),
        image_jobs_endpoint=endpoint,
        image_jobs_endpoint_lock=_normalize_image_jobs_endpoint_lock(
            it.get("image_jobs_endpoint_lock"), endpoint
        ),
        image_jobs_base_url=_normalize_image_jobs_base_url(
            it.get("image_jobs_base_url")
        ),
        image_concurrency=_normalize_image_concurrency(
            it.get("image_concurrency")
        ),
    )


_IMAGE_JOBS_ENDPOINT_VALUES = {"auto", "generations", "responses"}


def _normalize_image_jobs_endpoint(raw: Any) -> str:
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in _IMAGE_JOBS_ENDPOINT_VALUES:
            return value
    return "auto"


def _normalize_image_jobs_endpoint_lock(raw: Any, endpoint: str) -> bool:
    # auto 时 lock 没有意义——避免 UI 残留 lock=true 但 endpoint 改回 auto 的脏配置。
    if endpoint == "auto":
        return False
    return bool(raw)


def _normalize_image_jobs_base_url(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    if not (value.startswith("http://") or value.startswith("https://")):
        return ""
    return value


_IMAGE_CONCURRENCY_MAX = 32


def _normalize_image_concurrency(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(_IMAGE_CONCURRENCY_MAX, value))


def _to_proxy_out(it: dict, idx: int) -> ProviderProxyOut:
    proxy_type = _normalize_proxy_type(
        it.get("type") or it.get("protocol") or "socks5",
        fallback=True,
    )
    port = _safe_int(
        it.get("port"),
        22 if proxy_type == "ssh" else 1080,
        minimum=1,
    )
    port = min(65535, port)
    return ProviderProxyOut(
        name=it.get("name") or f"proxy-{idx}",
        type=proxy_type,
        host=it.get("host", ""),
        port=port,
        username=it.get("username") if isinstance(it.get("username"), str) else None,
        password_hint=_mask_secret(it.get("password")),
        private_key_path=(
            it.get("private_key_path")
            if isinstance(it.get("private_key_path"), str)
            else None
        ),
        enabled=bool(it.get("enabled", True)),
    )


@router.get("", response_model=ProvidersOut)
async def list_providers(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProvidersOut:
    raw, source = await _read_providers(db)
    if not raw:
        return ProvidersOut(items=[], source=source)
    items, proxies = _parse_config(raw)
    return ProvidersOut(
        items=[_to_out(it, i) for i, it in enumerate(items)],
        proxies=[_to_proxy_out(it, i) for i, it in enumerate(proxies)],
        source=source,
    )


@router.put(
    "",
    response_model=ProvidersOut,
    dependencies=[Depends(verify_csrf)],
)
async def update_providers(
    body: ProvidersUpdateIn,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProvidersOut:
    # 清空场景
    if not body.items:
        existing = (
            await db.execute(
                select(SystemSetting).where(SystemSetting.key == "providers")
            )
        ).scalar_one_or_none()
        if existing:
            await db.delete(existing)
        await write_audit(
            db,
            event_type="admin.providers.clear",
            user_id=admin.id,
            actor_email_hash=hash_email(admin.email),
            actor_ip_hash=request_ip_hash(request),
            details={},
        )
        await db.commit()
        from .admin_models import invalidate_admin_models_cache

        invalidate_admin_models_cache()
        return ProvidersOut(items=[], proxies=[], source="none")

    # 名称去重
    seen_names: set[str] = set()
    for it in body.items:
        n = it.name.strip()
        if not n:
            raise _http("invalid_request", "provider 名称不能为空", 422)
        if n in seen_names:
            raise _http("invalid_request", f"provider 名称重复：{n}", 422)
        seen_names.add(n)

    seen_proxy_names: set[str] = set()
    for it in body.proxies:
        n = it.name.strip()
        if not n:
            raise _http("invalid_request", "proxy 名称不能为空", 422)
        if n in seen_proxy_names:
            raise _http("invalid_request", f"proxy 名称重复：{n}", 422)
        seen_proxy_names.add(n)

    # 读旧 key 用于保留
    old_raw, _ = await _read_providers(db)
    old_keys: dict[str, str] = {}
    old_proxy_passwords: dict[str, str] = {}
    if old_raw:
        old_items, old_proxies = _parse_config(old_raw)
        for it in old_items:
            name = it.get("name", "")
            key = it.get("api_key", "")
            if isinstance(name, str) and isinstance(key, str) and name.strip() and key:
                old_keys[name.strip()] = key
        for it in old_proxies:
            name = it.get("name", "")
            password = it.get("password", "")
            if (
                isinstance(name, str)
                and isinstance(password, str)
                and name.strip()
                and password
            ):
                old_proxy_passwords[name.strip()] = password

    proxy_arr: list[dict] = []
    for it in body.proxies:
        proxy_name = it.name.strip()
        proxy_type = _normalize_proxy_type(it.type)
        proxy_password = it.password.strip()
        if proxy_password == "" and proxy_name in old_proxy_passwords:
            proxy_password = old_proxy_passwords[proxy_name]
        proxy_arr.append(
            {
                "name": proxy_name,
                "type": proxy_type,
                "host": it.host.strip(),
                "port": it.port,
                "username": (it.username or "").strip() or None,
                "password": proxy_password,
                "private_key_path": (it.private_key_path or "").strip() or None,
                "enabled": it.enabled,
            }
        )
    proxy_names = {it["name"] for it in proxy_arr}

    arr: list[dict] = []
    for it in body.items:
        provider_name = it.name.strip()
        api_key = it.api_key.strip()
        if api_key == "" and provider_name in old_keys:
            api_key = old_keys[provider_name]
        if not api_key:
            raise _http(
                "invalid_request",
                f"provider「{provider_name}」缺少 api_key",
                422,
            )
        provider_proxy = (it.proxy or "").strip() or None
        if provider_proxy and provider_proxy not in proxy_names:
            raise _http(
                "invalid_request",
                f"provider「{provider_name}」引用了不存在的代理：{provider_proxy}",
                422,
            )
        endpoint = _normalize_image_jobs_endpoint(it.image_jobs_endpoint)
        row = {
            "name": provider_name,
            "base_url": it.base_url.strip(),
            "api_key": api_key,
            "priority": it.priority,
            "weight": max(1, it.weight),
            "enabled": it.enabled,
            "image_jobs_enabled": it.image_jobs_enabled,
            "image_jobs_endpoint": endpoint,
            "image_jobs_endpoint_lock": _normalize_image_jobs_endpoint_lock(
                it.image_jobs_endpoint_lock, endpoint
            ),
            "image_jobs_base_url": _normalize_image_jobs_base_url(
                it.image_jobs_base_url
            ),
            "image_concurrency": _normalize_image_concurrency(it.image_concurrency),
        }
        if provider_proxy:
            row["proxy"] = provider_proxy
        arr.append(row)

    raw_json = json.dumps(
        {"providers": arr, "proxies": proxy_arr},
        ensure_ascii=False,
    )
    if len(raw_json) > _PROVIDERS_MAX_LEN:
        raise _http(
            "invalid_request",
            f"providers JSON 超过 {_PROVIDERS_MAX_LEN} 字符",
            422,
        )

    try:
        validate_providers(raw_json)
    except ValueError as exc:
        raise _http("invalid_request", str(exc), 422) from exc

    existing = (
        await db.execute(
            select(SystemSetting).where(SystemSetting.key == "providers")
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(SystemSetting(key="providers", value=raw_json))
    else:
        existing.value = raw_json

    await write_audit(
        db,
        event_type="admin.providers.update",
        user_id=admin.id,
        actor_email_hash=hash_email(admin.email),
        actor_ip_hash=request_ip_hash(request),
        details={"count": len(arr), "names": [it["name"] for it in arr]},
    )
    await db.commit()
    from .admin_models import invalidate_admin_models_cache

    invalidate_admin_models_cache()

    out: list[ProviderItemOut] = []
    for it in arr:
        endpoint = _normalize_image_jobs_endpoint(it.get("image_jobs_endpoint"))
        out.append(
            ProviderItemOut(
                name=it["name"],
                base_url=it["base_url"],
                api_key_hint=_mask_key(it["api_key"]),
                priority=it["priority"],
                weight=it["weight"],
                enabled=it["enabled"],
                proxy=it.get("proxy"),
                image_jobs_enabled=bool(it.get("image_jobs_enabled", False)),
                image_jobs_endpoint=endpoint,
                image_jobs_endpoint_lock=_normalize_image_jobs_endpoint_lock(
                    it.get("image_jobs_endpoint_lock"), endpoint
                ),
                image_jobs_base_url=_normalize_image_jobs_base_url(
                    it.get("image_jobs_base_url")
                ),
                image_concurrency=_normalize_image_concurrency(
                    it.get("image_concurrency")
                ),
            )
        )
    proxies_out = [_to_proxy_out(it, i) for i, it in enumerate(proxy_arr)]
    return ProvidersOut(items=out, proxies=proxies_out, source="db")


# ---------------------------------------------------------------------------
# 探活
# ---------------------------------------------------------------------------

def _extract_response_output_text(payload: object) -> str:
    """Extract text from a non-streaming Responses API payload."""
    if not isinstance(payload, dict):
        return ""

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    chunks: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text") or part.get("output_text")
                if isinstance(text, str) and text:
                    chunks.append(text)
    if chunks:
        return "".join(chunks)

    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return ""


def _extract_sse_output_text(raw: str) -> str:
    chunks: list[str] = []
    buffer = raw.replace("\r\n", "\n")
    for raw_event in buffer.split("\n\n"):
        data_lines: list[str] = []
        for line in raw_event.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        delta = obj.get("delta")
        if isinstance(delta, str) and delta:
            chunks.append(delta)
            continue

        text = obj.get("text") or obj.get("output_text")
        if isinstance(text, str) and text:
            chunks.append(text)
            continue

        for key in ("response", "item", "part"):
            nested = obj.get(key)
            nested_text = _extract_response_output_text(nested)
            if nested_text:
                chunks.append(nested_text)
                break

    return "".join(chunks)


async def _probe_one(
    base_url: str,
    api_key: str,
    *,
    proxy: ProviderProxyDefinition | None = None,
) -> tuple[bool, int, str | None]:
    url = _responses_url(base_url)
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    body = {
        "model": _PROBE_MODEL,
        "instructions": _PROBE_INSTRUCTIONS,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": _PROBE_INPUT}],
            }
        ],
        "stream": False,
        "store": False,
    }
    t0 = time.monotonic()
    try:
        proxy_url = await resolve_provider_proxy_url(proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_PROBE_TIMEOUT_S),
            proxy=proxy_url,
        ) as client:
            resp = await client.post(url, json=body, headers=headers)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status_code >= 400:
            return False, latency, f"HTTP {resp.status_code}"
        try:
            payload = resp.json()
            text = _extract_response_output_text(payload)
        except Exception:  # noqa: BLE001
            text = _extract_sse_output_text(resp.text)
            if not text:
                return False, latency, "bad_json"
        if "9801" in text:
            return True, latency, None
        return False, latency, "wrong_answer"
    except httpx.TimeoutException:
        latency = int((time.monotonic() - t0) * 1000)
        return False, latency, "timeout"
    except Exception as exc:
        latency = int((time.monotonic() - t0) * 1000)
        return False, latency, type(exc).__name__


def _probe_blocked_by_endpoint_lock(it: dict[str, Any]) -> bool:
    endpoint = _normalize_image_jobs_endpoint(it.get("image_jobs_endpoint"))
    if endpoint == "auto":
        return False
    probe_view = {
        "image_jobs_endpoint": endpoint,
        "image_jobs_endpoint_lock": _normalize_image_jobs_endpoint_lock(
            it.get("image_jobs_endpoint_lock"), endpoint
        ),
    }
    return not endpoint_kind_allowed(probe_view, "responses")


@router.post(
    "/probe",
    response_model=ProvidersProbeOut,
    dependencies=[Depends(verify_csrf)],
)
async def probe_providers(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: ProvidersProbeIn | None = None,
) -> ProvidersProbeOut:
    raw, _ = await _read_providers(db)
    if not raw:
        return ProvidersProbeOut(items=[], probed_at=None)
    items, proxy_items = _parse_config(raw)
    if not items:
        return ProvidersProbeOut(items=[], probed_at=None)
    proxy_by_name: dict[str, ProviderProxyDefinition] = {}
    for i, proxy_item in enumerate(proxy_items):
        try:
            parsed = parse_proxy_item(proxy_item, index=i)
        except Exception:  # noqa: BLE001
            continue
        proxy_by_name[parsed.name] = parsed

    names_filter = set(body.names) if body and body.names else None

    async def _do(it: dict, idx: int) -> ProviderProbeResult:
        name = it.get("name") or f"provider-{idx}"
        base_url = it.get("base_url", "")
        api_key = it.get("api_key", "")

        if names_filter and name not in names_filter:
            return ProviderProbeResult(
                name=name, ok=False, status="skipped"
            )

        if not bool(it.get("enabled", True)):
            return ProviderProbeResult(
                name=name, ok=False, status="disabled"
            )

        if _probe_blocked_by_endpoint_lock(it):
            return ProviderProbeResult(
                name=name,
                ok=False,
                status="skipped",
                error="endpoint_locked_to_generations",
            )

        if not base_url or not api_key:
            return ProviderProbeResult(
                name=name,
                ok=False,
                error="missing config",
                status="unhealthy",
            )

        proxy_name = it.get("proxy")
        proxy = (
            proxy_by_name.get(proxy_name)
            if isinstance(proxy_name, str) and proxy_name
            else None
        )
        ok, latency, err = await _probe_one(base_url, api_key, proxy=proxy)
        return ProviderProbeResult(
            name=name,
            ok=ok,
            latency_ms=latency,
            error=err,
            status="healthy" if ok else "unhealthy",
        )

    results = await asyncio.gather(
        *[_do(it, i) for i, it in enumerate(items)]
    )
    now = datetime.now(timezone.utc).isoformat()
    return ProvidersProbeOut(items=list(results), probed_at=now)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=ProviderStatsOut)
async def provider_stats(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProviderStatsOut:
    """从 Redis 读取 per-provider 请求统计；从 DB 读取自动探活间隔设置。"""
    from ..redis_client import get_redis

    raw, _ = await _read_providers(db)
    if not raw:
        return ProviderStatsOut(
            items=[], auto_probe_interval=120, auto_image_probe_interval=0
        )

    provider_names = [
        it.get("name", f"provider-{i}")
        for i, it in enumerate(_parse_items(raw))
    ]

    r = get_redis()
    items: list[ProviderStatsItem] = []
    grand_total = 0

    for name in provider_names:
        key = f"lumen:provider_stats:{name}"
        vals = await r.hgetall(key)
        total = int(vals.get("total", 0))
        success = int(vals.get("success", 0))
        fail = int(vals.get("fail", 0))
        grand_total += total
        items.append(ProviderStatsItem(
            name=name,
            total=total,
            success=success,
            fail=fail,
            success_rate=success / total if total > 0 else 0.0,
        ))

    for it in items:
        it.traffic_pct = it.total / grand_total if grand_total > 0 else 0.0

    # 读取自动探活间隔（文本 + image 各一个开关）
    interval_rows = (
        await db.execute(
            select(SystemSetting.key, SystemSetting.value).where(
                SystemSetting.key.in_(
                    [
                        "providers.auto_probe_interval",
                        "providers.auto_image_probe_interval",
                    ]
                )
            )
        )
    ).all()
    interval_map = {row.key: row.value for row in interval_rows}

    def _to_int(val: str | None, default: int) -> int:
        if val is None or val == "":
            return default
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    interval = _to_int(interval_map.get("providers.auto_probe_interval"), 120)
    image_interval = _to_int(
        interval_map.get("providers.auto_image_probe_interval"), 0
    )

    return ProviderStatsOut(
        items=items,
        auto_probe_interval=interval,
        auto_image_probe_interval=image_interval,
    )


__all__ = ["router"]
