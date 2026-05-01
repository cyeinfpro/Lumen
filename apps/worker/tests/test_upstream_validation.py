from __future__ import annotations

import logging
import math
import sys

import pytest


def test_curl_timeout_arg_rounds_up_and_never_disables_timeout() -> None:
    from app import upstream

    assert upstream._curl_timeout_arg(0.1) == "1"
    assert upstream._curl_timeout_arg(0.999) == "1"
    assert upstream._curl_timeout_arg(1.1) == "2"
    assert upstream._curl_timeout_arg(10.0) == "10"
    assert upstream._curl_timeout_arg(-5.0) == "1"
    assert upstream._curl_timeout_arg(math.inf) == "1"


def test_upstream_error_detail_summary_redacts_sensitive_text() -> None:
    from app import upstream

    summary = upstream._summarize_upstream_error_detail(
        {
            "code": "policy_violation",
            "type": "invalid_request_error",
            "message": "blocked for owner@example.com with key sk-secret123456",
            "raw_payload": {"prompt": "private"},
        }
    )

    assert isinstance(summary, dict)
    assert summary["code"] == "policy_violation"
    assert "[email]" in summary["message"]
    assert "[api_key]" in summary["message"]
    assert "owner@example.com" not in summary["message"]
    assert "sk-secret123456" not in summary["message"]


def test_configure_pil_max_image_pixels_logs_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app import upstream

    class BrokenPIL:
        MAX_IMAGE_PIXELS = upstream._MAX_REFERENCE_IMAGE_PIXELS + 1

        def __setattr__(self, _name: str, _value: object) -> None:
            raise RuntimeError("cannot configure")

    monkeypatch.setattr(upstream, "PILImage", BrokenPIL())

    with caplog.at_level(logging.WARNING, logger=upstream.logger.name):
        upstream._configure_pil_max_image_pixels()

    assert "failed to configure PIL MAX_IMAGE_PIXELS" in caplog.text


@pytest.mark.asyncio
async def test_validate_provider_base_url_allows_http_private_and_internal_hosts() -> None:
    from app.validation import validate_provider_base_url

    assert await validate_provider_base_url("http://127.0.0.1:8000/v1/") == (
        "http://127.0.0.1:8000/v1"
    )
    assert await validate_provider_base_url("http://internal-api.local/v1/") == (
        "http://internal-api.local/v1"
    )


@pytest.mark.asyncio
async def test_validate_provider_base_url_rejects_unsupported_scheme() -> None:
    from app.validation import (
        ProviderBaseUrlValidationError,
        validate_provider_base_url,
    )

    with pytest.raises(ProviderBaseUrlValidationError) as exc_info:
        await validate_provider_base_url("ftp://127.0.0.1/v1")

    assert exc_info.value.error_code == "invalid_provider_base_url"


@pytest.mark.asyncio
async def test_provider_pool_validation_does_not_import_upstream() -> None:
    from app import provider_pool

    sys.modules.pop("app.upstream", None)

    assert (
        await provider_pool._validate_provider_base_url("https://8.8.8.8/v1/")
        == "https://8.8.8.8/v1"
    )
    assert "app.upstream" not in sys.modules
