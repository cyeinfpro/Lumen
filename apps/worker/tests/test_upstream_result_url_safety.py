from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import Image as PILImage

from app.upstream_parts import upstream_impl as upstream
from app.upstream_parts import StagedImageFile, cleanup_owned_generated_payload
from lumen_core.url_security import (
    PublicHttpBodyTooLarge,
    PublicHttpDownload,
    PublicHttpStagedDownload,
)


TEST_UPSTREAM_RUNTIME = upstream.build_image_upstream_runtime()
TEST_UPSTREAM_SERVICES = TEST_UPSTREAM_RUNTIME.services


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (4, 3), (20, 40, 60)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_result_download_streams_to_owned_staged_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_download(
        url: str,
        **kwargs: Any,
    ) -> PublicHttpStagedDownload:
        destination = kwargs["destination"]
        destination.write_bytes(b"png-bytes")
        return PublicHttpStagedDownload(
            url=url,
            status_code=200,
            headers={"content-type": "image/png"},
            path=destination,
            size=9,
            sha256=upstream.hashlib.sha256(b"png-bytes").hexdigest(),
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "download_public_http_url_to_file",
        fake_download,
    )

    result = await TEST_UPSTREAM_SERVICES.direct.fetch_image_url_as_bytes(
        "https://cdn.example/result.png"
    )

    assert isinstance(result, StagedImageFile)
    assert result.path.read_bytes() == b"png-bytes"
    assert result.owned is True
    cleanup_owned_generated_payload(result)
    assert not result.path.exists()


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

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure, "download_public_http_url", fake_download
    )

    result = await TEST_UPSTREAM_SERVICES.image_jobs.download_image_job_result(
        client=UnsafeLegacyClient(),  # type: ignore[arg-type]
        image_url="http://image-job:8080/files/result.png",
        proxy_url="socks5://proxy.example:1080",
        allowed_base_url="http://image-job:8080/v1",
    )

    assert result == b"png-bytes"
    assert seen["max_bytes"] == TEST_UPSTREAM_SERVICES.core.IMAGE_JOB_DOWNLOAD_MAX_BYTES
    assert seen["max_redirects"] == 5
    assert seen["allow_http"] is True
    assert seen["allowed_private_origins"] == ("http://image-job:8080/v1",)


@pytest.mark.asyncio
async def test_result_download_rejects_non_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_result_url(url: str, **_kwargs: Any) -> PublicHttpStagedDownload:
        assert url == "http://169.254.169.254/latest/meta-data"
        raise ValueError("base_url host is not allowed")

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "download_public_http_url_to_file",
        reject_result_url,
    )

    with pytest.raises(upstream.UpstreamError) as excinfo:
        await TEST_UPSTREAM_SERVICES.direct.fetch_image_url_as_bytes(
            "http://169.254.169.254/latest/meta-data"
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.error_code == "invalid_value"
    assert excinfo.value.payload["path"] == "images/result"


@pytest.mark.asyncio
async def test_result_download_maps_stream_limit_to_stream_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def oversized(url: str, **_kwargs: Any) -> PublicHttpStagedDownload:
        raise PublicHttpBodyTooLarge(
            url=url,
            max_bytes=TEST_UPSTREAM_SERVICES.core.IMAGE_JOB_DOWNLOAD_MAX_BYTES,
            received_bytes=TEST_UPSTREAM_SERVICES.core.IMAGE_JOB_DOWNLOAD_MAX_BYTES + 1,
            status_code=200,
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure, "download_public_http_url", oversized
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "download_public_http_url_to_file",
        oversized,
    )

    with pytest.raises(upstream.UpstreamError) as excinfo:
        await TEST_UPSTREAM_SERVICES.direct.fetch_image_url_as_bytes(
            "https://cdn.example/oversized.png"
        )

    assert excinfo.value.status_code == 200
    assert excinfo.value.error_code == "stream_too_large"
    assert (
        excinfo.value.payload["bytes"]
        == TEST_UPSTREAM_SERVICES.core.IMAGE_JOB_DOWNLOAD_MAX_BYTES + 1
    )


@pytest.mark.asyncio
async def test_result_download_reports_final_redirect_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(
        _url: str,
        **kwargs: Any,
    ) -> PublicHttpStagedDownload:
        return PublicHttpStagedDownload(
            url="https://cdn.example/missing.png",
            status_code=404,
            headers={"content-type": "application/json"},
            path=kwargs["destination"],
            size=0,
            sha256="0" * 64,
            redirects=1,
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure, "download_public_http_url", missing
    )
    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "download_public_http_url_to_file",
        missing,
    )

    with pytest.raises(upstream.UpstreamError) as excinfo:
        await TEST_UPSTREAM_SERVICES.direct.fetch_image_url_as_bytes(
            "https://gateway.example/result.png"
        )

    assert excinfo.value.status_code == 404
    assert excinfo.value.error_code == "no_image_returned"
    assert excinfo.value.payload["final_url"] == "https://cdn.example/missing.png"


@pytest.mark.asyncio
async def test_result_download_accepts_valid_image_body_with_soft_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _png_bytes()

    async def soft_missing(
        url: str,
        **kwargs: Any,
    ) -> PublicHttpStagedDownload:
        destination = kwargs["destination"]
        destination.write_bytes(raw)
        return PublicHttpStagedDownload(
            url=url,
            status_code=404,
            headers={"content-type": "image/png"},
            path=destination,
            size=len(raw),
            sha256=upstream.hashlib.sha256(raw).hexdigest(),
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "download_public_http_url_to_file",
        soft_missing,
    )

    result = await TEST_UPSTREAM_SERVICES.direct.fetch_image_url_as_bytes(
        "https://cdn.example/soft-404.png"
    )

    assert isinstance(result, StagedImageFile)
    assert result.path.read_bytes() == raw
    cleanup_owned_generated_payload(result)
    assert not result.path.exists()


@pytest.mark.asyncio
async def test_result_download_rejects_soft_404_non_image_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b'{"error":"not found"}'

    async def soft_missing(
        url: str,
        **kwargs: Any,
    ) -> PublicHttpStagedDownload:
        destination = kwargs["destination"]
        destination.write_bytes(raw)
        return PublicHttpStagedDownload(
            url=url,
            status_code=404,
            headers={"content-type": "image/png"},
            path=destination,
            size=len(raw),
            sha256=upstream.hashlib.sha256(raw).hexdigest(),
        )

    monkeypatch.setattr(
        TEST_UPSTREAM_SERVICES.infrastructure,
        "download_public_http_url_to_file",
        soft_missing,
    )

    with pytest.raises(upstream.UpstreamError) as excinfo:
        await TEST_UPSTREAM_SERVICES.direct.fetch_image_url_as_bytes(
            "https://cdn.example/soft-404.html"
        )

    assert excinfo.value.status_code == 404
    assert excinfo.value.error_code == "no_image_returned"
