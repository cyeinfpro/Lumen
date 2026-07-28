"""Production workflow HTTP router."""

from fastapi import APIRouter

from ...workflows.transport.http import (
    apparel,
    model_library,
    poster,
    projects as project_routes,
)
from .projects import router as project_query_router


router = APIRouter()
router.include_router(project_query_router)
router.include_router(
    apparel.entry_router,
    prefix="/workflows",
    tags=["workflows"],
)
router.include_router(
    project_routes.core_router,
    prefix="/workflows",
    tags=["workflows"],
)
router.include_router(
    apparel.project_router,
    prefix="/workflows",
    tags=["workflows"],
)
router.include_router(
    project_routes.actions_router,
    prefix="/workflows",
    tags=["workflows"],
)
router.include_router(
    model_library.router,
    prefix="/workflows",
    tags=["workflows"],
)
router.include_router(
    poster.router,
    prefix="/workflows",
    tags=["workflows"],
)


__all__ = ["router"]
