from __future__ import annotations

import pytest

from app.config import validate_image_job_base_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9000",
        "http://localhost:9000",
        "http://image-job:9000",
        "http://10.0.0.8:9000",
        "http://image-job.internal:9000",
    ],
)
def test_private_image_job_http_is_allowed(url: str) -> None:
    assert validate_image_job_base_url(url) == url


def test_public_image_job_http_is_rejected_at_startup() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_image_job_base_url("http://image-job.vendor.io")


def test_public_image_job_https_is_allowed() -> None:
    url = "https://image-job.vendor.io"
    assert validate_image_job_base_url(url) == url
