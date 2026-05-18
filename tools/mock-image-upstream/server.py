#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse


SAMPLE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
SAMPLE_PNG_BYTES = base64.b64decode(SAMPLE_PNG_B64)
DEFAULT_SCENARIO = "success_b64"
DEFAULT_SLOW_DELAY_MS = 31_000

SCENARIOS = frozenset(
    {
        "success_b64",
        "success_url",
        "unauthorized_401",
        "rate_limit_429",
        "server_error_500",
        "invalid_json",
        "slow_response",
        "revised_prompt",
        "url_404",
        "url_expired",
        "url_cors_blocked",
        "async_success",
        "async_failed",
        "actual_size_missing",
    }
)
SCENARIO_ALIASES = {
    "success": "success_b64",
    "b64": "success_b64",
    "url": "success_url",
    "401": "unauthorized_401",
    "unauthorized": "unauthorized_401",
    "429": "rate_limit_429",
    "rate_limit": "rate_limit_429",
    "500": "server_error_500",
    "server_error": "server_error_500",
    "slow": "slow_response",
    "timeout": "slow_response",
    "prompt_revised": "revised_prompt",
    "async": "async_success",
    "job_success": "async_success",
    "job_failed": "async_failed",
    "missing_actual_size": "actual_size_missing",
}


@dataclass
class MockState:
    scenario: str = DEFAULT_SCENARIO
    delay_ms: int = 0
    slow_delay_ms: int = DEFAULT_SLOW_DELAY_MS
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_job_id: int = 1
    lock: threading.Lock = field(default_factory=threading.Lock)


class MockImageUpstreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], state: MockState):
        super().__init__(server_address, MockImageUpstreamHandler)
        self.state = state


def normalize_scenario(value: str | None) -> str | None:
    if not value:
        return None
    key = value.strip()
    key = SCENARIO_ALIASES.get(key, key)
    if key in SCENARIOS:
        return key
    return None


def create_server(
    host: str = "127.0.0.1",
    port: int = 8787,
    state: MockState | None = None,
) -> MockImageUpstreamServer:
    if state is None:
        state = MockState()
    normalized = normalize_scenario(state.scenario)
    if normalized is None:
        raise ValueError(f"unknown scenario: {state.scenario!r}")
    state.scenario = normalized
    return MockImageUpstreamServer((host, port), state)


class MockImageUpstreamHandler(BaseHTTPRequestHandler):
    server: MockImageUpstreamServer

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            f"[mock-image-upstream] {self.address_string()} "
            f"{self.command} {self.path} - {fmt % args}"
        )

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_common_headers(cors=True)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,HEAD,OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization,Content-Type,X-Mock-Image-Scenario,X-Mock-Delay-Ms",
        )
        self.end_headers()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/assets/"):
            self._handle_asset(parsed, head_only=True)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head_only=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "scenario": self.server.state.scenario,
                    "scenarios": sorted(SCENARIOS),
                },
            )
            return
        if parsed.path == "/scenarios":
            self._send_json(
                HTTPStatus.OK,
                {
                    "default": self.server.state.scenario,
                    "scenarios": sorted(SCENARIOS),
                    "aliases": dict(sorted(SCENARIO_ALIASES.items())),
                },
            )
            return
        if parsed.path == "/scenario":
            self._send_json(HTTPStatus.OK, {"scenario": self.server.state.scenario})
            return
        if parsed.path.startswith("/scenario/"):
            self._switch_scenario(parsed.path.removeprefix("/scenario/"))
            return
        if parsed.path.startswith("/assets/"):
            self._handle_asset(parsed)
            return
        if parsed.path.startswith("/v1/image-jobs/"):
            self._handle_image_job_poll(parsed)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/scenario/"):
            self._switch_scenario(parsed.path.removeprefix("/scenario/"))
            return
        if parsed.path == "/v1/refs":
            self._handle_ref_upload(parsed)
            return
        if parsed.path in {"/v1/images/generations", "/v1/images/edits"}:
            self._handle_images_api(parsed)
            return
        if parsed.path == "/v1/responses":
            self._handle_responses_api(parsed)
            return
        if parsed.path == "/v1/image-jobs":
            self._handle_image_job_submit(parsed)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json_body(self) -> dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _query(self, parsed: Any) -> dict[str, list[str]]:
        return parse_qs(parsed.query, keep_blank_values=True)

    def _query_one(self, parsed: Any, name: str) -> str | None:
        values = self._query(parsed).get(name)
        if not values:
            return None
        return values[-1]

    def _active_scenario(self, parsed: Any) -> str:
        override = (
            self._query_one(parsed, "scenario")
            or self.headers.get("X-Mock-Image-Scenario")
            or self.server.state.scenario
        )
        normalized = normalize_scenario(override)
        if normalized is None:
            raise ValueError(f"unknown scenario: {override!r}")
        return normalized

    def _delay_for(self, parsed: Any, scenario: str) -> int:
        raw = self._query_one(parsed, "delay_ms") or self.headers.get("X-Mock-Delay-Ms")
        if raw is not None:
            try:
                return max(0, int(raw))
            except ValueError:
                return 0
        if scenario == "slow_response":
            return max(0, int(self.server.state.slow_delay_ms))
        return max(0, int(self.server.state.delay_ms))

    def _maybe_sleep(self, parsed: Any, scenario: str) -> None:
        delay_ms = self._delay_for(parsed, scenario)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

    def _base_url(self) -> str:
        host = self.headers.get("Host")
        if not host:
            bind_host, bind_port = self.server.server_address[:2]
            if bind_host in {"", "0.0.0.0", "::"}:
                bind_host = "127.0.0.1"
            host = f"{bind_host}:{bind_port}"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}"

    def _asset_url(self, name: str) -> str:
        return f"{self._base_url()}/assets/{quote(name)}"

    def _send_common_headers(self, *, cors: bool = True) -> None:
        self.send_header("Server", "lumen-mock-image-upstream")
        self.send_header("Cache-Control", "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")

    def _send_json(
        self,
        status: int | HTTPStatus,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self._send_common_headers(cors=True)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_raw(
        self,
        status: int | HTTPStatus,
        body: bytes,
        *,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._send_common_headers(cors=True)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_invalid_json(self) -> None:
        self._send_raw(
            HTTPStatus.OK,
            b'{"data":[{"b64_json":',
            content_type="application/json; charset=utf-8",
        )

    def _send_error_scenario(self, scenario: str) -> bool:
        if scenario == "unauthorized_401":
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {
                    "error": {
                        "message": "mock unauthorized",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )
            return True
        if scenario == "rate_limit_429":
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {
                    "error": {
                        "message": "mock rate limit exceeded",
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                    }
                },
                headers={"Retry-After": "2"},
            )
            return True
        if scenario == "server_error_500":
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": {
                        "message": "mock upstream server error",
                        "type": "server_error",
                        "code": "server_error",
                    }
                },
            )
            return True
        if scenario == "invalid_json":
            self._send_invalid_json()
            return True
        return False

    def _switch_scenario(self, raw_name: str) -> None:
        name = normalize_scenario(raw_name)
        if name is None:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": f"unknown scenario: {raw_name}", "scenarios": sorted(SCENARIOS)},
            )
            return
        with self.server.state.lock:
            self.server.state.scenario = name
        self._send_json(HTTPStatus.OK, {"scenario": name})

    def _handle_asset(self, parsed: Any, *, head_only: bool = False) -> None:
        path = parsed.path
        cors = True
        if path.endswith("/missing.png"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "mock image missing"})
            return
        if path.endswith("/expired.png"):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "mock image url expired", "code": "url_expired"},
            )
            return
        if path.endswith("/cors-blocked.png"):
            cors = False
        self.send_response(HTTPStatus.OK)
        self._send_common_headers(cors=cors)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(SAMPLE_PNG_BYTES)))
        self.send_header("ETag", '"mock-image-upstream-sample"')
        self.end_headers()
        if not head_only:
            self.wfile.write(SAMPLE_PNG_BYTES)

    def _handle_ref_upload(self, parsed: Any) -> None:
        scenario = self._active_scenario(parsed)
        self._maybe_sleep(parsed, scenario)
        if self._send_error_scenario(scenario):
            return
        _ = self._read_body()
        expires_at = int(time.time()) + 3600
        self._send_json(
            HTTPStatus.OK,
            {
                "url": self._asset_url("reference.png"),
                "expires_at": expires_at,
                "bytes": len(SAMPLE_PNG_BYTES),
            },
        )

    def _image_data_item(self, scenario: str) -> dict[str, Any]:
        item: dict[str, Any] = {}
        if scenario == "success_url":
            item["url"] = self._asset_url("generated.png")
        elif scenario == "url_404":
            item["url"] = self._asset_url("missing.png")
        elif scenario == "url_expired":
            item["url"] = self._asset_url("expired.png")
        elif scenario == "url_cors_blocked":
            item["url"] = self._asset_url("cors-blocked.png")
        else:
            item["b64_json"] = SAMPLE_PNG_B64
        if scenario == "revised_prompt":
            item["revised_prompt"] = "mock revised prompt: sharper product photo"
        if scenario != "actual_size_missing":
            item["actual_size"] = "1x1"
        return item

    def _images_payload(self, scenario: str) -> dict[str, Any]:
        return {
            "created": int(time.time()),
            "data": [self._image_data_item(scenario)],
            "usage": {"images": 1},
        }

    def _handle_images_api(self, parsed: Any) -> None:
        try:
            scenario = self._active_scenario(parsed)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._maybe_sleep(parsed, scenario)
        if self._send_error_scenario(scenario):
            return
        _ = self._read_body()
        self._send_json(HTTPStatus.OK, self._images_payload(scenario))

    def _responses_payload(self, scenario: str, request_body: dict[str, Any]) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": "ig_mock_1",
            "type": "image_generation_call",
            "status": "completed",
            "result": SAMPLE_PNG_B64,
        }
        if scenario == "revised_prompt":
            item["revised_prompt"] = "mock revised prompt: sharper product photo"
        if scenario in {"success_url", "url_404", "url_expired", "url_cors_blocked"}:
            item["url"] = self._image_data_item(scenario).get("url")
        return {
            "id": "resp_mock_1",
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": request_body.get("model") or "mock-responses-model",
            "output": [item],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    def _send_sse_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if "type" not in payload:
            payload = {"type": event_type, **payload}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"event: {event_type}\n".encode("utf-8"))
        self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _handle_responses_api(self, parsed: Any) -> None:
        try:
            scenario = self._active_scenario(parsed)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        request_body = self._read_json_body()
        self._maybe_sleep(parsed, scenario)
        if self._send_error_scenario(scenario):
            return

        wants_stream = (
            "text/event-stream" in (self.headers.get("Accept") or "")
            or bool(request_body.get("stream"))
        )
        payload = self._responses_payload(scenario, request_body)
        if not wants_stream:
            self._send_json(HTTPStatus.OK, payload)
            return

        self.send_response(HTTPStatus.OK)
        self._send_common_headers(cors=True)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()
        self._send_sse_event(
            "response.created",
            {"type": "response.created", "response": {"id": payload["id"]}},
        )
        self._send_sse_event(
            "response.image_generation_call.partial_image",
            {
                "type": "response.image_generation_call.partial_image",
                "partial_image_index": 0,
                "partial_image": SAMPLE_PNG_B64[:48],
            },
        )
        output_item = payload["output"][0]
        self._send_sse_event(
            "response.output_item.done",
            {"type": "response.output_item.done", "item": output_item},
        )
        self._send_sse_event(
            "response.completed",
            {"type": "response.completed", "response": payload},
        )

    def _handle_image_job_submit(self, parsed: Any) -> None:
        try:
            scenario = self._active_scenario(parsed)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._maybe_sleep(parsed, scenario)
        if self._send_error_scenario(scenario):
            return
        payload = self._read_json_body()
        try:
            polls_before_done = max(0, int(self._query_one(parsed, "polls") or "1"))
        except ValueError:
            polls_before_done = 1
        with self.server.state.lock:
            job_id = f"job_{self.server.state.next_job_id}"
            self.server.state.next_job_id += 1
            self.server.state.jobs[job_id] = {
                "job_id": job_id,
                "scenario": scenario,
                "polls_seen": 0,
                "polls_before_done": polls_before_done,
                "endpoint_used": payload.get("endpoint") or "/v1/images/generations",
                "request_type": payload.get("request_type") or "generations",
                "created_at": int(time.time()),
            }
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "job_id": job_id,
                "status": "queued",
                "poll_url": f"{self._base_url()}/v1/image-jobs/{quote(job_id)}",
            },
        )

    def _handle_image_job_poll(self, parsed: Any) -> None:
        job_id = parsed.path.removeprefix("/v1/image-jobs/")
        with self.server.state.lock:
            job = self.server.state.jobs.get(job_id)
            if job is not None:
                job["polls_seen"] += 1
                snapshot = dict(job)
            else:
                snapshot = None
        if snapshot is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "mock image job not found", "job_id": job_id},
            )
            return

        scenario = snapshot["scenario"]
        self._maybe_sleep(parsed, scenario)
        if snapshot["polls_seen"] <= snapshot["polls_before_done"]:
            self._send_json(
                HTTPStatus.OK,
                {
                    "job_id": job_id,
                    "status": "running",
                    "progress": 0.5,
                    "endpoint_used": snapshot["endpoint_used"],
                },
            )
            return
        if scenario == "async_failed":
            self._send_json(
                HTTPStatus.OK,
                {
                    "job_id": job_id,
                    "status": "failed",
                    "error": "mock async job failed",
                    "error_class": "upstream_http_error",
                    "upstream_status": 500,
                    "upstream_body": {
                        "error": {
                            "message": "mock async upstream failed",
                            "type": "server_error",
                            "code": "server_error",
                        }
                    },
                    "endpoint_used": snapshot["endpoint_used"],
                },
            )
            return

        image_name = "generated.png"
        if scenario == "url_404":
            image_name = "missing.png"
        elif scenario == "url_expired":
            image_name = "expired.png"
        self._send_json(
            HTTPStatus.OK,
            {
                "job_id": job_id,
                "status": "succeeded",
                "endpoint_used": snapshot["endpoint_used"],
                "images": [
                    {
                        "url": self._asset_url(image_name),
                        "expires_at": int(time.time()) + 3600,
                        "bytes": len(SAMPLE_PNG_BYTES),
                        "width": 1,
                        "height": 1,
                        "format": "png",
                    }
                ],
            },
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a local mock upstream for Lumen image stability checks."
    )
    parser.add_argument("--host", default=os.getenv("MOCK_IMAGE_UPSTREAM_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MOCK_IMAGE_UPSTREAM_PORT", "8787")),
    )
    parser.add_argument(
        "--scenario",
        default=os.getenv("MOCK_IMAGE_UPSTREAM_SCENARIO", DEFAULT_SCENARIO),
        help=f"default scenario; one of: {', '.join(sorted(SCENARIOS))}",
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=int(os.getenv("MOCK_IMAGE_UPSTREAM_DELAY_MS", "0")),
        help="delay every handled upstream response by N milliseconds",
    )
    parser.add_argument(
        "--slow-delay-ms",
        type=int,
        default=int(
            os.getenv("MOCK_IMAGE_UPSTREAM_SLOW_DELAY_MS", str(DEFAULT_SLOW_DELAY_MS))
        ),
        help="delay used by the slow_response scenario",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scenario = normalize_scenario(args.scenario)
    if scenario is None:
        print(f"Unknown scenario: {args.scenario}", flush=True)
        print("Available scenarios:", ", ".join(sorted(SCENARIOS)), flush=True)
        return 2
    state = MockState(
        scenario=scenario,
        delay_ms=max(0, args.delay_ms),
        slow_delay_ms=max(0, args.slow_delay_ms),
    )
    server = create_server(args.host, args.port, state)
    host, port = server.server_address[:2]
    print(
        "mock image upstream listening on "
        f"http://{host}:{port} scenario={state.scenario}",
        flush=True,
    )
    print("scenarios:", ", ".join(sorted(SCENARIOS)), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
