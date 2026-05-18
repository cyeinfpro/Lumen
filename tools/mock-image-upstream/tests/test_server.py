from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


def load_server_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mock_image_upstream_server", SERVER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mock_server() -> Iterator[tuple[str, ModuleType]]:
    module = load_server_module()
    state = module.MockState(scenario="success_b64", slow_delay_ms=10)
    server = module.create_server("127.0.0.1", 0, state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", module
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 2,
) -> tuple[int, dict[str, str], bytes]:
    body = None
    req_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(
        base_url + path,
        data=body,
        headers=req_headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, dict(response.headers.items()), response.read()


def request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 2,
) -> dict[str, Any]:
    status, _, raw = request(
        base_url,
        path,
        method=method,
        payload=payload,
        headers=headers,
        timeout=timeout,
    )
    assert status < 400
    decoded = json.loads(raw.decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def post_image_generation(base_url: str, scenario: str) -> dict[str, Any]:
    return request_json(
        base_url,
        f"/v1/images/generations?scenario={scenario}",
        method="POST",
        payload={"model": "gpt-image-1", "prompt": "smoke", "size": "1024x1024"},
    )


def test_health_and_success_b64(mock_server: tuple[str, ModuleType]) -> None:
    base_url, _ = mock_server
    health = request_json(base_url, "/health")

    assert health["ok"] is True
    assert "success_b64" in health["scenarios"]

    payload = post_image_generation(base_url, "success_b64")

    first = payload["data"][0]
    assert first["b64_json"]
    assert first["actual_size"] == "1x1"
    assert payload["usage"]["images"] == 1


def test_success_url_and_broken_url_scenarios(
    mock_server: tuple[str, ModuleType],
) -> None:
    base_url, _ = mock_server
    switch = request_json(base_url, "/scenario/success_url")
    assert switch["scenario"] == "success_url"

    payload = request_json(
        base_url,
        "/v1/images/generations",
        method="POST",
        payload={"model": "gpt-image-1", "prompt": "url", "size": "1024x1024"},
    )
    image_url = payload["data"][0]["url"]
    status, headers, raw = request("", image_url)
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert raw

    missing = post_image_generation(base_url, "url_404")["data"][0]["url"]
    with pytest.raises(urllib.error.HTTPError) as missing_error:
        request("", missing)
    assert missing_error.value.code == 404

    expired = post_image_generation(base_url, "url_expired")["data"][0]["url"]
    with pytest.raises(urllib.error.HTTPError) as expired_error:
        request("", expired)
    assert expired_error.value.code == 403


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("unauthorized_401", 401),
        ("rate_limit_429", 429),
        ("server_error_500", 500),
    ],
)
def test_error_scenarios(
    mock_server: tuple[str, ModuleType],
    scenario: str,
    expected_status: int,
) -> None:
    base_url, _ = mock_server

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        post_image_generation(base_url, scenario)

    assert exc_info.value.code == expected_status
    payload = json.loads(exc_info.value.read().decode("utf-8"))
    assert payload["error"]["code"]


def test_invalid_json_and_slow_response(mock_server: tuple[str, ModuleType]) -> None:
    base_url, _ = mock_server

    _, _, raw = request(
        base_url,
        "/v1/images/generations?scenario=invalid_json",
        method="POST",
        payload={"model": "gpt-image-1", "prompt": "bad json", "size": "1024x1024"},
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw.decode("utf-8"))

    started = time.monotonic()
    payload = request_json(
        base_url,
        "/v1/images/generations?scenario=slow_response&delay_ms=20",
        method="POST",
        payload={"model": "gpt-image-1", "prompt": "slow", "size": "1024x1024"},
    )
    elapsed = time.monotonic() - started
    assert elapsed >= 0.015
    assert payload["data"][0]["b64_json"]


def test_responses_sse_with_revised_prompt(mock_server: tuple[str, ModuleType]) -> None:
    base_url, _ = mock_server

    status, headers, raw = request(
        base_url,
        "/v1/responses?scenario=revised_prompt",
        method="POST",
        headers={"Accept": "text/event-stream"},
        payload={
            "model": "gpt-5.1",
            "stream": True,
            "input": [],
            "tools": [{"type": "image_generation"}],
        },
    )

    text = raw.decode("utf-8")
    assert status == 200
    assert headers["Content-Type"].startswith("text/event-stream")
    assert "response.image_generation_call.partial_image" in text
    assert "response.output_item.done" in text
    assert "mock revised prompt" in text
    assert "response.completed" in text


def test_async_image_job_submit_poll_result(
    mock_server: tuple[str, ModuleType],
) -> None:
    base_url, _ = mock_server

    submit = request_json(
        base_url,
        "/v1/image-jobs?scenario=async_success&polls=1",
        method="POST",
        payload={
            "request_type": "generations",
            "endpoint": "/v1/images/generations",
            "body": {"prompt": "async smoke"},
        },
    )
    job_id = submit["job_id"]

    running = request_json(base_url, f"/v1/image-jobs/{job_id}")
    assert running["status"] == "running"

    done = request_json(base_url, f"/v1/image-jobs/{job_id}")
    assert done["status"] == "succeeded"
    assert done["endpoint_used"] == "/v1/images/generations"
    image_url = done["images"][0]["url"]

    status, headers, raw = request("", image_url)
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert raw
