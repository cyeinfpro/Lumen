from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image


def load_app_module():
    asyncio.set_event_loop(asyncio.new_event_loop())
    path = Path(__file__).resolve().parents[1] / "app.py"
    module_dir = str(path.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
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


def test_validate_payload_preserves_image_edit_input_transport() -> None:
    app = load_app_module()

    payload = app.validate_payload(
        {
            "endpoint": "/v1/images/edits",
            "body": {"prompt": "edit", "images": []},
            "image_edit_input_transport": "FILE",
        }
    )

    assert payload["image_edit_input_transport"] == "file"


def test_authorization_scheme_is_normalized_and_hashes_only_credential() -> None:
    app = load_app_module()
    request = SimpleNamespace(headers={"authorization": "bEaReR   sk-shared-secret"})

    normalized = app.require_auth(request)

    assert normalized == "Bearer sk-shared-secret"
    assert app.auth_hash(normalized) == hashlib.sha256(b"sk-shared-secret").hexdigest()
    assert app.auth_hash("bearer sk-shared-secret") == app.auth_hash(normalized)


def _tiny_png_b64() -> str:
    buf = BytesIO()
    Image.new("RGB", (2, 2), color=(128, 128, 128)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _FakeSseResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_bytes(self, chunk_size: int | None = None):
        _ = chunk_size
        for line in self._lines:
            await asyncio.sleep(0)
            yield line.encode("utf-8") + b"\n"


def test_responses_stream_partial_only_is_retryable_network_failure(
    monkeypatch,
) -> None:
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
        app.extract_responses_stream_images(resp, SimpleNamespace(), job_id="job_final")
    )

    assert len(images) == 1
    assert images[0].data.startswith(b"\x89PNG")


def test_responses_stream_flushes_unterminated_final_image_at_eof() -> None:
    app = load_app_module()
    final_b64 = _tiny_png_b64()
    payload = (
        'data: {"type":"response.output_item.done","item":'
        '{"type":"image_generation_call","result":"'
        + final_b64
        + '"}}'
    ).encode("utf-8")

    class UnterminatedResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_bytes(self, chunk_size: int | None = None):
            _ = chunk_size
            yield payload

    images = asyncio.run(
        app.extract_responses_stream_images(
            UnterminatedResponse(),
            SimpleNamespace(),
            job_id="job_unterminated_final",
        )
    )

    assert len(images) == 1
    assert images[0].data.startswith(b"\x89PNG")


def test_responses_stream_flushes_unterminated_done_marker_at_eof() -> None:
    app = load_app_module()

    class UnterminatedDoneResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def aiter_bytes(self, chunk_size: int | None = None):
            _ = chunk_size
            yield b"data: [DONE]"

    with pytest.raises(app.JobFailure) as exc:
        asyncio.run(
            app.extract_responses_stream_images(
                UnterminatedDoneResponse(),
                SimpleNamespace(),
                job_id="job_unterminated_done",
            )
        )

    assert exc.value.upstream_body["saw_done"] is True


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


def test_call_upstream_image_edits_file_mode_uses_multipart(monkeypatch) -> None:
    app = load_app_module()
    app.RETRY_NETWORK_MAX = 0
    app.RETRY_RESPONSES_STREAM_MAX = 0
    app.RETRY_UPSTREAM_5XX_MAX = 0

    tiny_png = base64.b64decode(_tiny_png_b64())
    calls: list[dict[str, object]] = []
    response_content = (
        b'{"data":[{"b64_json":"' + _tiny_png_b64().encode("ascii") + b'"}]}'
    )

    class _MultipartResponse:
        status_code = 200
        headers = {
            "content-type": "application/json",
            "content-length": str(len(response_content)),
        }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def aiter_bytes(self, chunk_size: int | None = None):
            _ = chunk_size
            yield response_content

    class _MultipartClient:
        def stream(self, _method: str, url: str, **kwargs: object):
            calls.append({"url": url, **kwargs})
            return _MultipartResponse()

    app._http_client = _MultipartClient()
    row = {
        "payload_json": app.json_dump(
            {
                "endpoint": "/v1/images/edits",
                "body": {
                    "model": "gpt-image-2",
                    "prompt": "edit",
                    "images": [
                        {
                            "image_url": "data:image/png;base64,"
                            + base64.b64encode(tiny_png).decode("ascii")
                        }
                    ],
                },
                "request_type": "edits",
                "image_edit_input_transport": "file",
                "retention_days": 1,
            }
        ),
        "auth_header": "Bearer sk-test",
        "job_id": "job_file",
        "created_at": "2026-05-04T00:00:00+00:00",
        "retention_days": 1,
    }

    async def fake_save_images(*_args, **_kw):
        return [{"url": "saved"}]

    monkeypatch.setattr(app, "save_images", fake_save_images)
    status, images = asyncio.run(app.call_upstream(row))

    assert status == 200
    assert images == [{"url": "saved"}]
    assert calls
    call = calls[0]
    assert call["url"].endswith("/v1/images/edits")
    assert "json" not in call
    assert call["data"]["prompt"] == "edit"  # type: ignore[index]
    assert call["files"][0][0] == "image[]"  # type: ignore[index]
    assert call["headers"]["Authorization"] == "Bearer sk-test"  # type: ignore[index]
    assert call["headers"]["Idempotency-Key"] == app.upstream_idempotency_key(
        "job_file"
    )  # type: ignore[index]
    assert "Content-Type" not in call["headers"]  # type: ignore[operator]


def _tiny_gif_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (2, 2), color=(0, 0, 0)).save(buf, format="GIF")
    return buf.getvalue()


def test_candidate_filename_rejects_gif() -> None:
    app = load_app_module()
    candidate = app.ImageCandidate(data=_tiny_gif_bytes(), mime_type="image/gif")
    with pytest.raises(app.JobFailure) as excinfo:
        app._candidate_filename("ref-0", candidate)
    assert "gif" in str(excinfo.value).lower()
    assert excinfo.value.upstream_status == 400


def test_materialize_edit_input_files_serializes_dict_and_bool() -> None:
    """multipart 上传时 dict/bool 必须 JSON 序列化（不是 Python repr）。"""
    app = load_app_module()
    tiny_png = base64.b64decode(_tiny_png_b64())

    class _NoOpClient:
        async def get(self, *_a, **_kw):  # 不会被调用（已经有 image_url=data:）
            raise AssertionError("unexpected http call")

    body = {
        "prompt": "edit",
        "model": "gpt-image-2",
        "metadata": {"trace_id": "abc", "user": "u1"},
        "tags": ["a", "b"],
        "stream": True,
        "n": 2,
        "images": [
            {
                "image_url": "data:image/png;base64,"
                + base64.b64encode(tiny_png).decode("ascii")
            }
        ],
    }

    data, files = asyncio.run(app.materialize_edit_input_files(_NoOpClient(), body))

    assert data["prompt"] == "edit"
    assert data["model"] == "gpt-image-2"
    assert data["metadata"] == '{"trace_id":"abc","user":"u1"}'  # JSON, 不是 repr
    assert data["tags"] == '["a","b"]'
    assert data["stream"] == "true"  # 小写 json bool
    assert data["n"] == "2"
    assert len(files) == 1
    assert files[0][0] == "image[]"
