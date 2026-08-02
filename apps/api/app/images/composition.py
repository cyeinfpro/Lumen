"""Process-lifetime composition for image HTTP services."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from lumen_core.storage_capacity import build_storage_capacity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import settings
from ..db import SessionLocal
from ..redis_client import get_redis
from .adapters.filesystem_store import FileSystemArtifactStore
from .adapters.redis_capacity import build_capacity
from .adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from .adapters.sqlalchemy_variants import SQLAlchemyVariantRepository
from .application.create_variant import CreateVariantService
from .application.upload import UploadCommandService
from .processing.isolated import IsolatedImageProcessingExecutor


_STATE_ATTRIBUTE = "image_route_composition"


@dataclass(frozen=True)
class ImageRouteComposition:
    upload_command_service: UploadCommandService
    variant_service: CreateVariantService | None = None


ImageRouteCompositionFactory = Callable[[], ImageRouteComposition]
ImageRouteLifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def compose_image_routes(
    *,
    storage_root: str,
    redis: Any,
    session_factory: async_sessionmaker[AsyncSession],
) -> ImageRouteComposition:
    artifacts = FileSystemArtifactStore(storage_root)
    capacity = build_capacity(redis)
    configured_policy = settings.image_upload_capacity_degraded_policy.strip()
    degraded_policy = configured_policy or (
        "scaled_local"
        if settings.app_env.strip().lower() in {"dev", "development", "local", "test"}
        else "fail_closed"
    )
    storage_capacity = build_storage_capacity(
        redis,
        storage_root,
        minimum_free_bytes=settings.minimum_storage_free_bytes,
        lease_ttl_seconds=settings.image_upload_lease_ttl_seconds,
        degraded_policy=degraded_policy,
    )
    processing_executor = IsolatedImageProcessingExecutor(
        result_timeout_seconds=settings.image_processing_result_timeout_s
    )
    return ImageRouteComposition(
        upload_command_service=UploadCommandService(
            artifacts=artifacts,
            capacity=capacity,
            storage_capacity=storage_capacity,
            repository=SQLAlchemyImageRepository(session_factory),
            processing_executor=processing_executor,
        ),
        variant_service=CreateVariantService(
            artifacts=artifacts,
            capacity=capacity,
            storage_capacity=storage_capacity,
            repository=SQLAlchemyVariantRepository(session_factory),
            processing_executor=processing_executor,
        ),
    )


def compose_process_image_routes() -> ImageRouteComposition:
    return compose_image_routes(
        storage_root=settings.storage_root,
        redis=get_redis(),
        session_factory=SessionLocal,
    )


def create_image_route_lifespan(
    composition_factory: ImageRouteCompositionFactory = compose_process_image_routes,
) -> ImageRouteLifespan:
    # FastAPI merges this router lifespan inside the API lifespan. Each worker
    # therefore owns one upload service from startup until its shared Redis and
    # database resources begin shutting down.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if hasattr(app.state, _STATE_ATTRIBUTE):
            raise RuntimeError("image route composition is already active")
        composition = composition_factory()
        setattr(app.state, _STATE_ATTRIBUTE, composition)
        try:
            yield
        finally:
            close = getattr(composition.upload_command_service, "aclose", None)
            if callable(close):
                await close()
            if getattr(app.state, _STATE_ATTRIBUTE, None) is composition:
                delattr(app.state, _STATE_ATTRIBUTE)

    return lifespan


def get_image_route_composition(request: Request) -> ImageRouteComposition:
    composition = getattr(request.app.state, _STATE_ATTRIBUTE, None)
    if not isinstance(composition, ImageRouteComposition):
        raise RuntimeError("image route composition is not active")
    return composition


def get_upload_command_service(request: Request) -> UploadCommandService:
    return get_image_route_composition(request).upload_command_service


def get_variant_service(request: Request) -> CreateVariantService:
    service = get_image_route_composition(request).variant_service
    if service is None:
        raise RuntimeError("image variant service is not active")
    return service
