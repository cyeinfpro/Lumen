"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request

from .config import ImageJobSettings
from .observability import (
    REQUEST_ID_HEADER,
    bind_request_id,
    current_request_id,
    reset_request_id,
)
from .runtime import ImageJobRuntime, create_runtime
from . import route_handlers


LOG = logging.getLogger("image-job")


def create_app(
    runtime: ImageJobRuntime | None = None,
    settings: ImageJobSettings | None = None,
) -> FastAPI:
    if runtime is not None and settings is not None:
        raise ValueError("pass runtime or settings, not both")
    app_runtime = runtime or create_runtime(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logging.basicConfig(
            level="INFO",
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        try:
            await app_runtime.startup()
            yield
        finally:
            await app_runtime.shutdown()

    app = FastAPI(title="sub2api image job sidecar", lifespan=lifespan)
    app.state.runtime = app_runtime

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        # H-19：优先复用调用方带来的 request_id，跨服务日志才能串成一条链；
        # 没带就本地生成，保证每条请求至少在本服务内可追踪。
        token = bind_request_id(request.headers.get(REQUEST_ID_HEADER))
        try:
            response = await call_next(request)
        finally:
            request_id = current_request_id()
            reset_request_id(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    caller = route_handlers.make_caller(app_runtime)

    @app.get("/livez", include_in_schema=False)
    async def livez():
        return await route_handlers.livez_handler()

    @app.get("/health", include_in_schema=False)
    @app.get("/ready", include_in_schema=False)
    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        return await route_handlers.readiness_handler(app_runtime)

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        return await route_handlers.metrics_handler(request, app_runtime, caller)

    @app.post("/v1/image-jobs")
    async def create_image_job(request: Request, identity=Depends(caller)):
        return await route_handlers.create_image_job_handler(
            request, app_runtime, identity
        )

    @app.get("/v1/image-jobs/{job_id}")
    async def get_image_job(job_id: str, request: Request, identity=Depends(caller)):
        return await route_handlers.get_image_job_handler(
            job_id, request, app_runtime, identity
        )

    @app.delete("/v1/image-jobs/{job_id}")
    async def delete_image_job(
        job_id: str,
        request: Request,
        identity=Depends(caller),
    ):
        return await route_handlers.delete_image_job_handler(
            job_id,
            request,
            app_runtime,
            identity,
        )

    @app.post("/v1/refs")
    async def upload_reference(request: Request, identity=Depends(caller)):
        return await route_handlers.upload_reference_handler(
            request, app_runtime, identity
        )

    return app
