from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image
from io import BytesIO


def load_app_module():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
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


def test_validate_payload_strips_responses_partial_images() -> None:
    app = load_app_module()

    payload = app.validate_payload(
        {
            "endpoint": "/v1/responses",
            "body": {
                "tools": [
                    {
                        "type": "image_generation",
                        "action": "generate",
                        "partial_images": 3,
                    }
                ]
            },
        }
    )

    tool = payload["body"]["tools"][0]
    assert "partial_images" not in tool
    assert tool["output_format"] == "jpeg"


def _tiny_png_b64() -> str:
    buf = BytesIO()
    Image.new("RGB", (2, 2), color=(128, 128, 128)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _FakeSseResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            await asyncio.sleep(0)
            yield line


def test_responses_stream_partial_only_is_retryable_network_failure(monkeypatch) -> None:
    app = load_app_module()
    partial_b64 = _tiny_png_b64()
    resp = _FakeSseResponse(
        [
            'data: {"type":"response.image_generation_call.partial_image","partial_image_b64":"'
            + partial_b64
            + '"}',
            "",
        ]
    )

    touched: list[str] = []

    async def fake_touch(job_id: str) -> None:
        touched.append(job_id)

    monkeypatch.setattr(app, "touch_running", fake_touch)
    monkeypatch.setattr(app, "JOB_HEARTBEAT_INTERVAL_S", 0)

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.extract_responses_stream_images(
                resp, SimpleNamespace(), job_id="job_partial"
            )
        )

    assert exc.value.error_class == app.ERROR_CLASS_NETWORK
    assert exc.value.retryable is True
    assert "partial images" in exc.value.error
    assert touched
    assert set(touched) == {"job_partial"}


def test_responses_stream_final_image_succeeds() -> None:
    app = load_app_module()
    final_b64 = _tiny_png_b64()
    resp = _FakeSseResponse(
        [
            'data: {"type":"response.output_item.done","item":{"type":"image_generation_call","result":"'
            + final_b64
            + '"}}',
            "",
        ]
    )

    images = asyncio.run(
        app.extract_responses_stream_images(
            resp, SimpleNamespace(), job_id="job_final"
        )
    )

    assert len(images) == 1
    assert images[0].data.startswith(b"\x89PNG")


def test_call_upstream_retries_responses_stream_interruption(monkeypatch) -> None:
    app = load_app_module()
    app.RETRY_NETWORK_MAX = 0
    app.RETRY_RESPONSES_STREAM_MAX = 1
    app.RETRY_BACKOFF_S = 0
    app.RETRY_UPSTREAM_5XX_MAX = 0
    attempts: list[int] = []

    async def fake_once(*_args, **_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise app.JobFailure(
                "Responses stream ended before returning an image",
                retryable=True,
                error_class=app.ERROR_CLASS_NETWORK,
            )
        return 200, [{"url": "https://example.com/image.png"}]

    monkeypatch.setattr(app, "_call_upstream_once", fake_once)
    app._http_client = SimpleNamespace()
    row = {
        "payload_json": app.json_dump(
            {
                "endpoint": "/v1/responses",
                "body": {},
                "request_type": "responses",
                "retention_days": 1,
            }
        ),
        "auth_header": "Bearer sk-test",
        "job_id": "job_retry",
    }

    status, images = asyncio.run(app.call_upstream(row))

    assert status == 200
    assert images == [{"url": "https://example.com/image.png"}]
    assert len(attempts) == 2
