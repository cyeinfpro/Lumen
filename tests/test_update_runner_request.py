from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_runner() -> ModuleType:
    path = ROOT / "scripts" / "update_runner.py"
    spec = importlib.util.spec_from_file_location("lumen_update_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 1,
        "target_tag": "v1.2.3",
        "channel": "stable",
        "force_redeploy": False,
        "idempotency_key": "idem-123",
        "proxy_url": None,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


def test_update_runner_builds_fixed_environment_without_path_overrides(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request()), encoding="utf-8")

    request = runner.load_request(request_path)
    env = runner.build_environment(request)

    assert env["LUMEN_IMAGE_TAG"] == "v1.2.3"
    assert env["LUMEN_VERSION"] == "1.2.3"
    assert env["LUMEN_UPDATE_BUILD"] == "0"
    assert "LUMEN_UPDATE_ROOT" not in env
    assert "LUMEN_REPO_DIR" not in env
    assert "LUMEN_SOURCE_ROOT" not in env


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"target_tag": "../../tmp/payload"}, "target_tag"),
        ({"target_tag": "latest"}, "target_tag"),
        ({"channel": "shell"}, "channel"),
        ({"force_redeploy": "1"}, "force_redeploy"),
        ({"proxy_url": "http://proxy.example/path"}, "proxy_url"),
        ({"proxy_url": "http://proxy.example\nX=1"}, "proxy_url"),
        ({"issued_at": "2020-01-01T00:00:00+00:00"}, "stale"),
    ],
)
def test_update_runner_rejects_invalid_request_values(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    runner = _load_runner()
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_request(**overrides)), encoding="utf-8")

    with pytest.raises(runner.UpdateRequestError, match=message):
        runner.load_request(request_path)


def test_update_runner_rejects_unknown_fields_and_symlinks(tmp_path: Path) -> None:
    runner = _load_runner()
    real_path = tmp_path / "real.json"
    real_path.write_text(
        json.dumps(_request(LUMEN_UPDATE_ROOT="/tmp/attacker")),
        encoding="utf-8",
    )
    with pytest.raises(runner.UpdateRequestError, match="fields"):
        runner.load_request(real_path)

    link_path = tmp_path / "request.json"
    link_path.symlink_to(real_path)
    with pytest.raises(runner.UpdateRequestError, match="cannot read"):
        runner.load_request(link_path)
