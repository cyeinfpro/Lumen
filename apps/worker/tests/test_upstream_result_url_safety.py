from __future__ import annotations

from typing import Any

import pytest

from app import upstream
from lumen_core.url_security import (
    PublicHttpBodyTooLarge,
    PublicHttpDownload,
)


@pytest.mark.asyncio
async def test_image_job_result_uses_bounded_dns_pinned_downloader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_download(url: str, **kwargs: Any) -> PublicHttpDownload:
        seen["url"] = url
        seen.update(kwargs)
        return PublicHttpDownload(
            url="http://image-job:8080/files/result.png",
            status_code=200,
            headers={"content-type": "image/png"},
            body=b"png-bytes",
        )

    class UnsafeLegacyClient:
        async def get(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("shared provider client must not download result URLs")

    monkeypatch.setattr(upstream, "download_public_http_url", fake_download)

    result = await upstream._download_image_job_result(
        client=UnsafeLegacyClient(),  # type: ignore[arg-type]
        image_url="http://image-job:8080/files/result.png",
        proxy_url="socks5://proxy.example:1080",
        allowed_base_url="http://image-job:8080/v1",
    )

    assert result == b"png-bytes"
    assert seen["max_bytes"] == upstream._IMAGE_JOB_DOWNLOAD_MAX_BYTES
    assert seen["max_redirects"] == 5
    assert seen["allow_http"] is True
    assert seen["allowed_private_origins"] == ("http://image-job:8080/v1",)


@pytest.mark.asyncio
async def test_result_download_rejects_non_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_result_url(url: str, **_kwargs: Any) -> PublicHttpDownload:
        assert url == "http://169.254.169.254/latest/meta-data"
        raise ValueError("base_url host is not allowed")

    monkeypatch.setattr(upstream, "download_public_http_url", reject_result_url)

    with pytest.raises(upstream.UpstreamError) as excinfo:
        await upstream._fetch_image_url_as_bytes(
            "http://169.254.169.254/latest/meta-data"
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.error_code == "invalid_value"
    assert excinfo.value.payload["path"] == "images/result"


@pytest.mark.asyncio
async def test_result_download_maps_stream_limit_to_stream_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def oversized(url: str, **_kwargs: Any) -> PublicHttpDownload:
        raise PublicHttpBodyTooLarge(
            url=url,
            max_bytes=upstream._IMAGE_JOB_DOWNLOAD_MAX_BYTES,
            received_bytes=upstream._IMAGE_JOB_DOWNLOAD_MAX_BYTES + 1,
            status_code=200,
        )

    monkeypatch.setattr(upstream, "download_public_http_url", oversized)

    with pytest.raises(upstream.UpstreamError) as excinfo:
        await upstream._fetch_image_url_as_bytes("https://cdn.example/oversized.png")

    assert excinfo.value.status_code == 200
    assert excinfo.value.error_code == "stream_too_large"
    assert excinfo.value.payload["bytes"] == upstream._IMAGE_JOB_DOWNLOAD_MAX_BYTES + 1


@pytest.mark.asyncio
async def test_result_download_reports_final_redirect_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(_url: str, **_kwargs: Any) -> PublicHttpDownload:
        return PublicHttpDownload(
            url="https://cdn.example/missing.png",
            status_code=404,
            headers={"content-type": "application/json"},
            body=b'{"error":"missing"}',
            redirects=1,
        )

    monkeypatch.setattr(upstream, "download_public_http_url", missing)

    with pytest.raises(upstream.UpstreamError) as excinfo:
        await upstream._fetch_image_url_as_bytes("https://gateway.example/result.png")

    assert excinfo.value.status_code == 404
    assert excinfo.value.error_code == "upstream_error"
    assert excinfo.value.payload["final_url"] == "https://cdn.example/missing.png"
