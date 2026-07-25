"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

import request_bodies

from .adapters.filesystem_artifacts import ArtifactFailure
from .application.auth import (
    AuthFailure,
    authenticate,
    upstream_credential,
)
from .application.job_service import JobServiceFailure
from .application.reference_service import ReferenceFailure
from .application.result_service import ResultFailure
from .config import ImageJobSettings
from .runtime import ImageJobRuntime, create_runtime


LOG = logging.getLogger("image-job")


def _http_failure(exc: Exception) -> HTTPException:
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", "internal image-job error")
    return HTTPException(status_code=status_code, detail=detail)


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
        await app_runtime.startup()
        try:
            yield
        finally:
            await app_runtime.shutdown()

    app = FastAPI(title="sub2api image job sidecar", lifespan=lifespan)
    app.state.runtime = app_runtime

    def caller(request: Request):
        try:
            identity = authenticate(request.headers, app_runtime.settings)
        except AuthFailure as exc:
            raise _http_failure(exc) from exc
        if identity.legacy:
            app_runtime.legacy_auth_requests_total += 1
        return identity

    async def readiness_response() -> Response:
        ready, _failures = await app_runtime.readiness()
        return Response(
            content='{"status":"ready"}' if ready else '{"status":"not_ready"}',
            status_code=200 if ready else 503,
            media_type="application/json",
        )

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health", include_in_schema=False)
    async def health() -> Response:
        return await readiness_response()

    @app.get("/ready", include_in_schema=False)
    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> Response:
        return await readiness_response()

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> PlainTextResponse:
        caller(request)
        return PlainTextResponse(
            await app_runtime.metrics_text(),
            media_type="text/plain; version=0.0.4",
        )

    @app.post("/v1/image-jobs")
    async def create_image_job(request: Request) -> dict[str, Any]:
        identity = caller(request)
        try:
            upstream = upstream_credential(request.headers, identity)
            raw = await request_bodies.read_request_body_bounded(
                request,
                max_bytes=app_runtime.settings.max_request_bytes,
            )
            if not raw:
                raise JobServiceFailure(400, "empty JSON body")
            raw_payload, payload = app_runtime.jobs.parse_payload(raw)
            key = app_runtime.jobs.idempotency_key(
                request.headers,
                raw_payload,
            )
            return await app_runtime.jobs.submit(
                caller=identity,
                upstream=upstream,
                payload=payload,
                idempotency_key=key,
            )
        except HTTPException:
            raise
        except (AuthFailure, JobServiceFailure) as exc:
            raise _http_failure(exc) from exc

    @app.get("/v1/image-jobs/{job_id}")
    async def get_image_job(
        job_id: str,
        request: Request,
    ) -> dict[str, Any]:
        identity = caller(request)
        upstream = None
        if request.headers.get("x-lumen-upstream-authorization"):
            try:
                upstream = upstream_credential(request.headers, identity)
            except AuthFailure as exc:
                raise _http_failure(exc) from exc
        try:
            return await app_runtime.jobs.results.get(
                job_id,
                identity,
                upstream,
            )
        except ResultFailure as exc:
            raise _http_failure(exc) from exc

    @app.post("/v1/refs")
    async def upload_reference(request: Request) -> dict[str, object]:
        identity = caller(request)
        try:
            raw = await request_bodies.read_request_body_bounded(
                request,
                max_bytes=app_runtime.settings.max_ref_bytes,
            )
            content_type = (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            return await app_runtime.references.upload(
                caller=identity,
                content_type=content_type,
                data=raw,
            )
        except (ReferenceFailure, ArtifactFailure) as exc:
            raise _http_failure(exc) from exc

    return app
