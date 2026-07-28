from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    case,
    create_engine,
    select,
)

from app.config import settings
from app.images.adapters.local_capacity import (
    _effective_global_peak_bytes,
    configured_process_count,
)
from app.images.adapters.redis_capacity import build_capacity
from app.images.adapters.sqlalchemy_repository import (
    _reconcile_candidate_condition,
    _reconcile_priority,
)
from app.images.application import http_routes, reconcile_runtime
from app.images.composition import (
    ImageRouteComposition,
    compose_image_routes,
    create_image_route_lifespan,
    get_upload_command_service,
)


def test_reconcile_candidates_do_not_starve_behind_old_ready_rows() -> None:
    metadata = MetaData()
    image_rows = Table(
        "images",
        metadata,
        Column("id", String, primary_key=True),
        Column("artifact_status", String, nullable=False),
        Column("reconcile_after", DateTime(timezone=True)),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    stale = now - timedelta(hours=1)
    future = now + timedelta(hours=1)
    rows = [
        {
            "id": f"ready-old-{index:03d}",
            "artifact_status": "ready",
            "reconcile_after": None,
            "updated_at": stale - timedelta(days=10),
        }
        for index in range(150)
    ]
    rows.extend(
        [
            {
                "id": "publishing-due",
                "artifact_status": "publishing",
                "reconcile_after": now - timedelta(seconds=1),
                "updated_at": stale,
            },
            {
                "id": "staging-stale",
                "artifact_status": "staging",
                "reconcile_after": None,
                "updated_at": stale,
            },
            {
                "id": "processing-stale",
                "artifact_status": "processing",
                "reconcile_after": None,
                "updated_at": stale,
            },
            {
                "id": "ready-due",
                "artifact_status": "ready",
                "reconcile_after": now - timedelta(seconds=1),
                "updated_at": stale,
            },
            {
                "id": "ready-future",
                "artifact_status": "ready",
                "reconcile_after": future,
                "updated_at": stale,
            },
        ]
    )
    with engine.begin() as connection:
        connection.execute(image_rows.insert(), rows)
        condition = _reconcile_candidate_condition(
            image_rows.c.artifact_status,
            image_rows.c.reconcile_after,
            image_rows.c.updated_at,
            due_before=now,
            stale_before=now - timedelta(minutes=5),
        )
        result = (
            connection.execute(
                select(image_rows.c.id)
                .where(condition)
                .order_by(
                    _reconcile_priority(image_rows.c.artifact_status),
                    case((image_rows.c.reconcile_after.is_(None), 1), else_=0),
                    image_rows.c.reconcile_after.asc(),
                    image_rows.c.updated_at.asc(),
                    image_rows.c.id.asc(),
                )
                .limit(100)
            )
            .scalars()
            .all()
        )

    assert result == [
        "publishing-due",
        "staging-stale",
        "processing-stale",
        "ready-due",
    ]


class _LeaseRedis:
    def __init__(self) -> None:
        self.value: str | None = None

    async def set(self, _key: str, value: str, **kwargs: Any) -> bool:
        assert kwargs == {
            "ex": reconcile_runtime._RECONCILE_LEASE_TTL_SECONDS,
            "nx": True,
        }
        if self.value is not None:
            return False
        self.value = value
        return True

    async def eval(self, script: str, keys: int, *args: str) -> int:
        assert keys == 1
        _lock_key, token, *_rest = args
        if "DEL" in script:
            if self.value == token:
                self.value = None
                return 1
            return 0
        if self.value != token:
            return 0
        return 1


@pytest.mark.asyncio
async def test_only_one_reconciler_runs_across_competing_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _LeaseRedis()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class _Reconciler:
        async def run_once(self, *, lease_guard: Any) -> Any:
            nonlocal calls
            assert lease_guard is not None
            calls += 1
            entered.set()
            await release.wait()
            return SimpleNamespace(
                marked_ready=1,
                marked_failed=0,
                rebuilt_reference=0,
                deleted_staged=0,
                deferred=0,
                scanned=1,
            )

    reconciler = _Reconciler()
    monkeypatch.setattr(reconcile_runtime, "get_redis", lambda: redis)
    monkeypatch.setattr(
        reconcile_runtime,
        "build_image_artifact_reconciler",
        lambda: reconciler,
    )
    monkeypatch.setattr(
        reconcile_runtime,
        "_next_reconcile_fence",
        lambda: asyncio.sleep(0, result=1),
    )

    first = asyncio.create_task(reconcile_runtime.run_image_artifact_reconciler_once())
    await entered.wait()
    second_result = await reconcile_runtime.run_image_artifact_reconciler_once()
    release.set()
    first_result = await first

    assert second_result == 0
    assert first_result == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_upload_service_is_owned_by_the_router_lifespan() -> None:
    marker = object()
    calls = 0

    def build() -> ImageRouteComposition:
        nonlocal calls
        calls += 1
        return ImageRouteComposition(
            upload_command_service=marker,  # type: ignore[arg-type]
        )

    app = FastAPI()
    app.include_router(APIRouter(lifespan=create_image_route_lifespan(build)))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/images/upload",
            "headers": [],
            "app": app,
        }
    )

    with pytest.raises(RuntimeError, match="not active"):
        get_upload_command_service(request)

    async with app.router.lifespan_context(app):
        assert get_upload_command_service(request) is marker
        assert get_upload_command_service(request) is marker
        assert calls == 1

    with pytest.raises(RuntimeError, match="not active"):
        get_upload_command_service(request)


@pytest.mark.asyncio
async def test_upload_and_variant_services_share_process_resources(
    tmp_path: Path,
) -> None:
    composition = compose_image_routes(
        storage_root=str(tmp_path),
        redis=object(),
        session_factory=object(),  # type: ignore[arg-type]
    )
    variant_service = composition.variant_service
    assert variant_service is not None
    try:
        assert composition.upload_command_service.artifacts is variant_service.artifacts
        assert composition.upload_command_service.capacity is variant_service.capacity
        assert (
            composition.upload_command_service.storage_capacity
            is variant_service.storage_capacity
        )
        assert (
            composition.upload_command_service.processing_executor
            is variant_service.processing_executor
        )
    finally:
        await composition.upload_command_service.aclose()


def test_registered_upload_route_uses_composed_service_dependency() -> None:
    route = next(
        route
        for route in http_routes.router.routes
        if isinstance(route, APIRoute) and route.path == "/upload"
    )

    assert route.endpoint is http_routes.upload_image
    assert get_upload_command_service in {
        dependency.call for dependency in route.dependant.dependencies
    }


def test_lumen_api_workers_controls_capacity_scaling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "WEB_CONCURRENCY",
        "GUNICORN_WORKERS",
        "UVICORN_WORKERS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LUMEN_API_WORKERS", "4")
    assert configured_process_count() == 4


def test_capacity_scaling_uses_compose_worker_default_when_env_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "LUMEN_API_WORKERS",
        "WEB_CONCURRENCY",
        "GUNICORN_WORKERS",
        "UVICORN_WORKERS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert configured_process_count() == 2


def test_implicit_capacity_default_accepts_one_large_upload() -> None:
    assert (
        _effective_global_peak_bytes(
            512 * 1024 * 1024,
            explicitly_configured=False,
        )
        == 1536 * 1024 * 1024
    )
    assert (
        _effective_global_peak_bytes(
            512 * 1024 * 1024,
            explicitly_configured=True,
        )
        == 512 * 1024 * 1024
    )


def test_process_guard_keeps_full_limit_while_degraded_fallback_is_scaled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUMEN_API_WORKERS", "2")
    monkeypatch.setattr(settings, "image_upload_global_concurrency", 4)
    monkeypatch.setattr(
        settings,
        "image_upload_global_peak_bytes",
        1536 * 1024 * 1024,
    )
    capacity = build_capacity(object())

    assert capacity.process_guard.limits.max_concurrency == 4
    assert capacity.process_guard.limits.max_peak_bytes == 1536 * 1024 * 1024
    assert capacity.global_capacity.fallback.limits.max_concurrency == 2
    assert capacity.global_capacity.fallback.limits.max_peak_bytes == 768 * 1024 * 1024
