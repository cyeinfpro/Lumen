from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException


def load_app_module():
    path = Path(__file__).resolve().parents[1] / "app.py"
    spec = importlib.util.spec_from_file_location("image_job_app_under_test", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_validate_payload_normalizes_transparent_image_generation() -> None:
    app = load_app_module()

    payload = app.validate_payload(
        {
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "logo", "background": "transparent"},
        }
    )

    assert payload["request_type"] == "generations"
    assert payload["body"]["output_format"] == "png"
    assert "output_compression" not in payload["body"]


def test_validate_payload_rejects_unsupported_endpoint() -> None:
    app = load_app_module()

    with pytest.raises(HTTPException) as exc:
        app.validate_payload({"endpoint": "/v1/chat/completions", "body": {}})

    assert exc.value.status_code == 400
    assert exc.value.detail == "unsupported image endpoint"
