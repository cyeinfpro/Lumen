from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = ROOT / "apps" / "api"

CSRF_EXEMPT_WRITE_ROUTES = {
    ("POST", "/auth/signup"): (
        "auth/bootstrap: creates the first session before a CSRF token exists"
    ),
    ("POST", "/auth/signup/byok"): (
        "auth/bootstrap: verifies BYOK signup token before a session exists"
    ),
    ("POST", "/auth/login"): "auth/bootstrap: establishes the session and CSRF token",
    ("POST", "/auth/password/reset-request"): (
        "public/auth bootstrap: pre-login reset request with rate limits"
    ),
    ("POST", "/auth/password/reset-confirm"): (
        "public/auth bootstrap: token-based reset before login"
    ),
    ("POST", "/auth/api-key/verify"): (
        "public/bootstrap: BYOK key verification before signup completion"
    ),
    ("POST", "/telegram/bind"): "bot: X-Bot-Token plus one-time link code",
    ("POST", "/telegram/unbind"): "bot: X-Bot-Token plus bound chat identity",
    ("POST", "/telegram/proxy/report"): (
        "bot: X-Bot-Token service-to-service report"
    ),
    ("POST", "/telegram/prompts/enhance"): (
        "bot: X-Bot-Token plus bound chat identity"
    ),
    ("POST", "/telegram/generations"): (
        "bot: X-Bot-Token plus bound chat identity"
    ),
}

_ROUTE_DUMP_SCRIPT = r"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from fastapi.routing import APIRoute

api_root = Path(os.environ["LUMEN_TEST_API_ROOT"])
if str(api_root) not in sys.path:
    sys.path.insert(0, str(api_root))

from app.deps import verify_csrf, verify_csrf_session
from app.main import app

csrf_calls = {verify_csrf, verify_csrf_session}
write_methods = {"DELETE", "PATCH", "POST", "PUT"}


def walk_dependant(dependant):
    yield dependant
    for child in getattr(dependant, "dependencies", []):
        yield from walk_dependant(child)


routes = []
for route in app.routes:
    if not isinstance(route, APIRoute):
        continue
    calls = [
        dependant.call
        for dependant in walk_dependant(route.dependant)
        if getattr(dependant, "call", None) is not None
    ]
    has_csrf = any(call in csrf_calls for call in calls)
    for method in sorted((route.methods or set()) & write_methods):
        routes.append(
            {
                "method": method,
                "path": route.path,
                "name": route.name,
                "has_csrf": has_csrf,
                "calls": [
                    getattr(call, "__name__", repr(call))
                    for call in calls
                ],
            }
        )

print(json.dumps(routes, sort_keys=True))
"""


def _route_dump() -> list[dict[str, Any]]:
    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "BYOK_API_KEY_MASTER_SECRET": "test-byok-master-secret-0123456789-test",
            "LUMEN_TEST_API_ROOT": str(API_ROOT),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", _ROUTE_DUMP_SCRIPT],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout)


def _switch_to_api_app() -> None:
    api_root = str(API_ROOT)
    if not sys.path or sys.path[0] != api_root:
        if api_root in sys.path:
            sys.path.remove(api_root)
        sys.path.insert(0, api_root)

    loaded = sys.modules.get("app")
    if loaded is None:
        return
    mod_file = getattr(loaded, "__file__", "") or ""
    try:
        is_api_app = Path(mod_file).resolve().is_relative_to(API_ROOT)
    except (OSError, RuntimeError, ValueError):
        is_api_app = False
    if not is_api_app:
        for key in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
            del sys.modules[key]


def test_write_routes_require_csrf_or_exact_exemption() -> None:
    stale_exemptions = set(CSRF_EXEMPT_WRITE_ROUTES)
    unexpected: list[str] = []

    for route in _route_dump():
        key = (route["method"], route["path"])
        if route["has_csrf"]:
            continue
        if key in CSRF_EXEMPT_WRITE_ROUTES:
            stale_exemptions.discard(key)
            continue
        unexpected.append(
            f"{route['method']} {route['path']} "
            f"({route['name']}; deps={route['calls']})"
        )

    assert unexpected == []
    assert stale_exemptions == set()


def test_github_main_channel_timestamp_is_timezone_aware_utc() -> None:
    _switch_to_api_app()

    from app.services.github_releases import GitHubReleasesClient

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        release = asyncio.run(GitHubReleasesClient().fetch_latest(channel="main"))

    assert release.published_at is not None
    assert release.published_at.endswith("Z")
    parsed = datetime.fromisoformat(release.published_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None
