#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from http.client import HTTPResponse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


FORBIDDEN_RESPONSE_KEYS = {"text", "summary_text", "prompt", "system_prompt", "upstream_request"}


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    csrf: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], Any]:
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), _read_json(resp)
    except HTTPError as exc:
        return exc.code, dict(exc.headers), _read_json(exc)
    except URLError as exc:
        raise SystemExit(f"request failed: {exc}") from exc


def _read_json(resp: HTTPResponse | HTTPError) -> Any:
    raw = resp.read()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def _assert_no_summary_or_prompt_payload(label: str, payload: Any) -> None:
    keys = _walk_keys(payload)
    leaked = sorted(keys & FORBIDDEN_RESPONSE_KEYS)
    if leaked:
        raise SystemExit(f"{label} leaked forbidden response keys: {', '.join(leaked)}")
    rendered = json.dumps(payload, ensure_ascii=False)
    if "data:image/" in rendered:
        raise SystemExit(f"{label} leaked an inline image data URL")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke test Lumen manual context compaction endpoint."
    )
    parser.add_argument("--base-url", required=True, help="API base URL, e.g. http://localhost:8000")
    parser.add_argument("--token", default=None, help="Bearer token if the API uses Authorization")
    parser.add_argument("--csrf", default=None, help="CSRF token for POST when required")
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--target-tokens", type=int, default=1200)
    parser.add_argument("--extra-instruction", default="Smoke test: preserve IDs and user decisions.")
    parser.add_argument("--skip-force", action="store_true", help="Only run dry_run and context checks")
    args = parser.parse_args()

    conv_path = f"/conversations/{args.conversation_id}"
    compact_path = f"{conv_path}/compact"
    context_path = f"{conv_path}/context"

    status, _headers, context_before = _json_request(
        args.base_url, context_path, token=args.token
    )
    if status != 200:
        raise SystemExit(f"GET {context_path} returned {status}: {context_before}")
    _assert_no_summary_or_prompt_payload("context before", context_before)

    status, _headers, dry_run = _json_request(
        args.base_url,
        compact_path,
        method="POST",
        token=args.token,
        csrf=args.csrf,
        body={"dry_run": True, "target_tokens": args.target_tokens},
    )
    if status != 200:
        raise SystemExit(f"dry_run compact returned {status}: {dry_run}")
    _assert_no_summary_or_prompt_payload("dry_run compact", dry_run)
    print(json.dumps({"dry_run": dry_run}, ensure_ascii=False, indent=2))

    if not args.skip_force:
        started = time.monotonic()
        status, headers, compact = _json_request(
            args.base_url,
            compact_path,
            method="POST",
            token=args.token,
            csrf=args.csrf,
            body={
                "force": True,
                "target_tokens": args.target_tokens,
                "extra_instruction": args.extra_instruction,
            },
            timeout=75.0,
        )
        if status != 200:
            retry_after = headers.get("Retry-After")
            suffix = f" Retry-After={retry_after}" if retry_after else ""
            raise SystemExit(f"force compact returned {status}:{suffix} {compact}")
        _assert_no_summary_or_prompt_payload("force compact", compact)
        if not isinstance(compact, dict) or compact.get("ok") is not True:
            raise SystemExit(f"force compact did not return ok=true: {compact}")
        print(
            json.dumps(
                {"force": compact, "elapsed_s": round(time.monotonic() - started, 2)},
                ensure_ascii=False,
                indent=2,
            )
        )

    status, _headers, context_after = _json_request(
        args.base_url, context_path, token=args.token
    )
    if status != 200:
        raise SystemExit(f"GET {context_path} after compact returned {status}: {context_after}")
    _assert_no_summary_or_prompt_payload("context after", context_after)
    print(json.dumps({"context_after": context_after}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
