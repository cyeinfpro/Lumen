"""FastAPI error adapter for the shared Volcano asset client."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from lumen_core import volcano_assets as _shared

from ..proxy_pool import resolve_provider_proxy_url

httpx = _shared.httpx


def _http_from_service_error(exc: _shared.VolcanoAssetServiceError) -> HTTPException:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details:
        error["details"] = exc.details
    if exc.retry_after_ms is not None:
        error["retry_after_ms"] = exc.retry_after_ms
    return HTTPException(
        status_code=exc.status_code,
        detail={"error": error},
        headers=exc.headers,
    )


class VolcanoAssetClient(_shared.VolcanoAssetClient):
    def __init__(self, provider: Any) -> None:
        async def _resolve(proxy: Any) -> str | None:
            return await resolve_provider_proxy_url(proxy)

        super().__init__(provider, proxy_resolver=_resolve)

    async def request(self, action: str, body: dict[str, Any]) -> Any:
        try:
            return await super().request(action, body)
        except _shared.VolcanoAssetServiceError as exc:
            raise _http_from_service_error(exc) from exc


__all__ = ["VolcanoAssetClient"]
