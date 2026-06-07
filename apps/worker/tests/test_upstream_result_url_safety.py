from __future__ import annotations

import pytest

from app import upstream


@pytest.mark.asyncio
async def test_result_download_url_allows_same_origin_image_job_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_resolver(*_args, **_kwargs):
        raise AssertionError("same-origin image job URL should not hit DNS guard")

    monkeypatch.setattr(upstream, "resolve_public_http_target", fail_resolver)

    await upstream._ensure_result_download_url(
        "http://image-job:8080/files/result.png",
        path="image-jobs/result",
        allowed_base_url="http://image-job:8080/v1",
    )


@pytest.mark.asyncio
async def test_result_download_url_rejects_non_public_result_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject_result_url(url: str, **kwargs):
        assert url == "http://169.254.169.254/latest/meta-data"
        assert kwargs["allow_private"] is False
        raise ValueError("base_url host is not allowed")

    monkeypatch.setattr(upstream, "resolve_public_http_target", reject_result_url)

    with pytest.raises(upstream.UpstreamError) as excinfo:
        await upstream._ensure_result_download_url(
            "http://169.254.169.254/latest/meta-data",
            path="images/result",
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.error_code == "invalid_value"
    assert excinfo.value.payload["path"] == "images/result"
