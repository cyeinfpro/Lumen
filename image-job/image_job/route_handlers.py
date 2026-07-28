"""FastAPI route handlers extracted from app_factory."""

from __future__ import annotations

from typing import Any

from fastapi import Request, Response
from fastapi.responses import PlainTextResponse

from . import http_bodies
from .application.auth import AuthFailure, authenticate, upstream_credential
from .application.job_service import JobServiceFailure
from .application.reference_service import ReferenceFailure
from .adapters.filesystem_artifacts import ArtifactFailure
from .application.result_service import ResultFailure
from .runtime import ImageJobRuntime


def _http_failure(exc: Exception):
    from fastapi import HTTPException

    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", "internal image-job error")
    return HTTPException(status_code=status_code, detail=detail)


def make_caller(runtime: ImageJobRuntime):
    """Return a dependency that authenticates the caller."""

    def caller(request: Request):
        try:
            identity = authenticate(request.headers, runtime.settings)
        except AuthFailure as exc:
            raise _http_failure(exc) from exc
        if identity.legacy:
            runtime.legacy_auth_requests_total += 1
        return identity

    return caller


async def livez_handler() -> dict[str, str]:
    return {"status": "ok"}


async def readiness_handler(runtime: ImageJobRuntime) -> Response:
    ready, _failures = await runtime.readiness()
    return Response(
        content='{"status":"ready"}' if ready else '{"status":"not_ready"}',
        status_code=200 if ready else 503,
        media_type="application/json",
    )


async def metrics_handler(
    request: Request, runtime: ImageJobRuntime, caller
) -> PlainTextResponse:
    caller(request)
    return PlainTextResponse(
        await runtime.metrics_text(),
        media_type="text/plain; version=0.0.4",
    )


async def create_image_job_handler(
    request: Request,
    runtime: ImageJobRuntime,
    identity,
) -> dict[str, Any]:
    from fastapi import HTTPException

    try:
        upstream = upstream_credential(request.headers, identity)
        raw = await http_bodies.read_request_body_bounded(
            request,
            max_bytes=runtime.settings.max_request_bytes,
        )
        if not raw:
            raise JobServiceFailure(400, "empty JSON body")
        raw_payload, payload = runtime.jobs.parse_payload(raw)
        key = runtime.jobs.idempotency_key(
            request.headers,
            raw_payload,
        )
        return await runtime.jobs.submit(
            caller=identity,
            upstream=upstream,
            payload=payload,
            idempotency_key=key,
        )
    except HTTPException:
        raise
    except (AuthFailure, JobServiceFailure) as exc:
        raise _http_failure(exc) from exc


async def get_image_job_handler(
    job_id: str,
    request: Request,
    runtime: ImageJobRuntime,
    identity,
) -> dict[str, Any]:
    upstream = None
    if request.headers.get("x-lumen-upstream-authorization"):
        try:
            upstream = upstream_credential(request.headers, identity)
        except AuthFailure as exc:
            raise _http_failure(exc) from exc
    try:
        return await runtime.jobs.results.get(
            job_id,
            identity,
            upstream,
        )
    except ResultFailure as exc:
        raise _http_failure(exc) from exc


async def upload_reference_handler(
    request: Request,
    runtime: ImageJobRuntime,
    identity,
) -> dict[str, object]:
    try:
        raw = await http_bodies.read_request_body_bounded(
            request,
            max_bytes=runtime.settings.max_ref_bytes,
        )
        content_type = (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        return await runtime.references.upload(
            caller=identity,
            content_type=content_type,
            data=raw,
        )
    except (ReferenceFailure, ArtifactFailure) as exc:
        raise _http_failure(exc) from exc
