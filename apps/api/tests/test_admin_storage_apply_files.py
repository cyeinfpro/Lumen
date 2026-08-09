from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.routes.admin_storage_apply_files import (
    read_host_fence_floor,
    stage_storage_apply,
)


def test_host_fence_floor_uses_claim_results_and_pending_requests(
    tmp_path: Path,
) -> None:
    claim_path = tmp_path / "apply.claim.json"
    results_dir = tmp_path / "results"
    requests_dir = tmp_path / "requests"
    latest_path = tmp_path / "last-apply.json"
    results_dir.mkdir()
    requests_dir.mkdir()
    claim_path.write_text(
        json.dumps(
            {
                "operation_id": "a" * 32,
                "fence": 7,
            }
        ),
        encoding="utf-8",
    )
    (results_dir / f"{'b' * 32}.11.json").write_text("partial", encoding="utf-8")
    (requests_dir / f"{'c' * 32}.9.json").write_text("pending", encoding="utf-8")
    latest_path.write_text(
        json.dumps(
            {
                "operation_id": "d" * 32,
                "fence": 10,
                "status": "ok",
            }
        ),
        encoding="utf-8",
    )

    assert (
        read_host_fence_floor(
            claim_path=claim_path,
            results_dir=results_dir,
            requests_dir=requests_dir,
            latest_result_path=latest_path,
        )
        == 11
    )


def test_host_fence_floor_fails_closed_on_invalid_claim(tmp_path: Path) -> None:
    claim_path = tmp_path / "apply.claim.json"
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    claim_path.write_text('{"fence": 99}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="identity is invalid"):
        read_host_fence_floor(
            claim_path=claim_path,
            results_dir=results_dir,
        )


def test_storage_request_is_immutable_for_same_identity(tmp_path: Path) -> None:
    requests_dir = tmp_path / "requests"
    operation_id = "e" * 32
    first = "MODE=local\nLOCAL_ROOT=/srv/lumen-a\n"
    first_digest = hashlib.sha256(first.encode("utf-8")).hexdigest()
    stage_storage_apply(
        state_dir=tmp_path,
        requests_dir=requests_dir,
        operation_id=operation_id,
        fence=12,
        desired_config_sha256=first_digest,
        conf_text=first,
    )

    second = "MODE=local\nLOCAL_ROOT=/srv/lumen-b\n"
    with pytest.raises(RuntimeError, match="immutable storage request conflicts"):
        stage_storage_apply(
            state_dir=tmp_path,
            requests_dir=requests_dir,
            operation_id=operation_id,
            fence=12,
            desired_config_sha256=hashlib.sha256(second.encode("utf-8")).hexdigest(),
            conf_text=second,
        )
