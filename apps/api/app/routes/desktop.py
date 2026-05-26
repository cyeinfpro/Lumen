"""Desktop-only auth, settings, and diagnostics facade."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.desktop_runtime import (
    desktop_bootstrap_marker,
    desktop_data_root,
    desktop_logs_root,
    desktop_provider_metadata_path,
    desktop_settings_path,
)
from lumen_core.schemas import (
    ProviderItemOut,
    ProviderStatsOut,
    ProvidersOut,
    ProvidersProbeIn,
    ProvidersProbeOut,
    ProvidersUpdateIn,
    SystemSettingsOut,
    SystemSettingsUpdateIn,
    UserOut,
)
from lumen_core.runtime_settings import get_spec, parse_value

from ..db import get_db
from ..deps import CurrentUser
from ..runtime_settings import get_settings_view, update_settings
from . import auth as auth_routes
from . import providers as providers_routes


router = APIRouter(tags=["desktop"])

_DESKTOP_WRITABLE_SETTING_KEYS = {
    "providers.auto_probe_interval",
    "providers.auto_image_probe_interval",
}


class DesktopBootstrapStatusOut(BaseModel):
    complete: bool
    data_root: str
    settings: dict[str, Any] = Field(default_factory=dict)
    disk_free_bytes: int | None = None


class DesktopBootstrapCompleteIn(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)


class DesktopReadyOut(BaseModel):
    status: str
    runtime: str = "desktop"
    data_root: str
    checked_at: datetime


class DesktopDiagnosticsOut(BaseModel):
    data_root: str
    logs_root: str
    settings_path: str
    provider_metadata_path: str
    bootstrap_complete: bool
    disk_free_bytes: int | None = None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _disk_free(path: Path) -> int | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def _http_error(code: str, message: str, http: int = 400, **details: Any):
    from fastapi import HTTPException

    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(status_code=http, detail={"error": error})


@router.get("/system/desktop-ready", response_model=DesktopReadyOut)
async def desktop_ready() -> DesktopReadyOut:
    root = desktop_data_root()
    return DesktopReadyOut(
        status="ok",
        data_root=str(root),
        checked_at=datetime.now(timezone.utc),
    )


@router.get("/auth/me", response_model=UserOut)
async def desktop_me(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    return await auth_routes._user_out_with_runtime_defaults(user, db)


@router.get("/auth/csrf", response_model=auth_routes.CsrfOut)
async def desktop_csrf() -> auth_routes.CsrfOut:
    return auth_routes.CsrfOut(csrf_token="desktop-local-token")


@router.post("/auth/logout", status_code=204)
async def desktop_logout() -> None:
    return None


@router.get("/settings/bootstrap-status", response_model=DesktopBootstrapStatusOut)
async def bootstrap_status() -> DesktopBootstrapStatusOut:
    root = desktop_data_root()
    marker = desktop_bootstrap_marker()
    settings = _read_json(desktop_settings_path())
    return DesktopBootstrapStatusOut(
        complete=marker.is_file(),
        data_root=str(root),
        settings=settings,
        disk_free_bytes=_disk_free(root),
    )


@router.post("/settings/bootstrap-complete", response_model=DesktopBootstrapStatusOut)
async def bootstrap_complete(
    body: DesktopBootstrapCompleteIn,
) -> DesktopBootstrapStatusOut:
    root = desktop_data_root()
    _write_json(desktop_settings_path(), body.settings)
    marker = desktop_bootstrap_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    return DesktopBootstrapStatusOut(
        complete=True,
        data_root=str(root),
        settings=_read_json(desktop_settings_path()),
        disk_free_bytes=_disk_free(root),
    )


@router.get("/settings/diagnostics", response_model=DesktopDiagnosticsOut)
async def diagnostics() -> DesktopDiagnosticsOut:
    root = desktop_data_root()
    return DesktopDiagnosticsOut(
        data_root=str(root),
        logs_root=str(desktop_logs_root()),
        settings_path=str(desktop_settings_path()),
        provider_metadata_path=str(desktop_provider_metadata_path()),
        bootstrap_complete=desktop_bootstrap_marker().is_file(),
        disk_free_bytes=_disk_free(root),
    )


@router.get("/settings/system", response_model=SystemSettingsOut)
async def desktop_system_settings(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemSettingsOut:
    _ = user
    return SystemSettingsOut(items=await get_settings_view(db))


@router.put("/settings/system", response_model=SystemSettingsOut)
async def update_desktop_system_settings(
    body: SystemSettingsUpdateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SystemSettingsOut:
    _ = user
    pairs: list[tuple[str, str]] = []
    invalid: list[dict[str, Any]] = []
    for item in body.items:
        if item.key not in _DESKTOP_WRITABLE_SETTING_KEYS:
            invalid.append({"key": item.key, "reason": "desktop_unsupported"})
            continue
        spec = get_spec(item.key)
        if spec is None:
            invalid.append({"key": item.key, "reason": "unknown_key"})
            continue
        if len(item.value) > 2048:
            invalid.append({"key": item.key, "reason": "value_too_long"})
            continue
        try:
            parse_value(spec, item.value)
        except (TypeError, ValueError) as exc:
            invalid.append(
                {
                    "key": item.key,
                    "reason": f"invalid_{spec.parser.__name__}",
                    "message": str(exc),
                }
            )
            continue
        pairs.append((item.key, item.value))
    if invalid:
        raise _http_error(
            "invalid_request",
            "one or more setting items are invalid",
            422,
            errors=invalid,
        )
    await update_settings(db, pairs)
    await db.commit()
    return SystemSettingsOut(items=await get_settings_view(db))


@router.get("/settings/providers", response_model=ProvidersOut)
async def list_desktop_providers(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProvidersOut:
    return await providers_routes.list_providers(user, db)


@router.put("/settings/providers", response_model=ProvidersOut)
async def update_desktop_providers(
    body: ProvidersUpdateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProvidersOut:
    return await providers_routes.update_providers(body, request, user, db)


@router.patch(
    "/settings/providers/{provider_name}/enabled", response_model=ProviderItemOut
)
async def patch_desktop_provider_enabled(
    provider_name: str,
    body: providers_routes.ProviderEnabledPatchIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProviderItemOut:
    return await providers_routes.patch_provider_enabled(
        provider_name, body, request, user, db
    )


@router.post("/settings/providers/probe", response_model=ProvidersProbeOut)
async def probe_desktop_providers(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: ProvidersProbeIn | None = None,
) -> ProvidersProbeOut:
    return await providers_routes.probe_providers(user, db, body)


@router.get("/settings/providers/stats", response_model=ProviderStatsOut)
async def desktop_provider_stats(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProviderStatsOut:
    return await providers_routes.provider_stats(user, db)
