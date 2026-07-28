from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "perf" / "wave2" / "run.py"


def _run(*args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_scheduler_characterizes_100_mixed_candidates() -> None:
    result = _run("scheduler", "--candidate-counts", "10,100", "--capacity", "4")
    measurement = next(
        item for item in result["measurements"] if item["candidate_count"] == 100
    )

    assert measurement["mixed_lane_count"] == 6
    assert measurement["selected_enqueue_count"] == 8
    assert measurement["redis_commands"] >= measurement["selected_enqueue_count"]
    assert measurement["candidate_scan_round_trips"] > 0
    assert result["acceptance"]["status"] in {"met", "not_met"}


def test_enqueue_unknown_oracle_limits_active_revision() -> None:
    result = _run("enqueue-unknown")

    assert result["current"]["accepted_active_dispatch_revisions"] >= 1
    assert result["target_oracle"]["accepted_active_dispatch_revisions"] == 1
    assert result["target_oracle"]["duplicate_active_dispatch_revisions"] == 0
    assert result["target_oracle"]["acceptance"]["status"] == "met"


def test_mixed_resource_demand_stays_within_fixed_budgets() -> None:
    result = _run("resources")

    assert result["workload_counts"] == {
        "1mp": 8,
        "4k": 2,
        "dual_race": 1,
        "multi_reference_edit": 2,
    }
    assert result["demand_by_kind"]["dual_race"]["external_lane_units"] == 2
    assert (
        result["peak_active_weighted_units"]
        <= result["budgets"]["global_weighted_units"]
    )
    assert result["cleanup_faults"]["permit_leaks"] == 0
    assert result["acceptance"]["status"] == "met"


def test_four_k_payload_reports_base64_amplification_and_staged_target() -> None:
    result = _run("payload", "--payload-mib", "12")
    shapes = result["synthetic_shapes"]

    assert shapes["legacy_url_bytes_base64"]["base64_expansion_ratio"] >= 4 / 3
    assert shapes["staged_payload_target"]["temporary_path_removed"] is True
    assert (
        shapes["logical_peak_reduction_percent"]
        >= result["acceptance"]["fixed_reduction_target_percent"]
    )
    assert result["real_workload_comparison"]["status"] in {"gated", "measured"}


def test_compare_accepts_two_suite_artifacts(tmp_path: Path) -> None:
    suite = _run(
        "suite",
        "--candidate-counts",
        "10,100",
        "--capacity",
        "4",
        "--payload-mib",
        "12",
    )
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(json.dumps(suite), encoding="utf-8")
    after.write_text(json.dumps(suite), encoding="utf-8")

    comparison = _run(
        "compare",
        "--before",
        str(before),
        "--after",
        str(after),
    )

    assert comparison["status"] == "compared"
    assert comparison["scheduler_100"]["before_redis_commands"] is not None
    assert comparison["after_acceptance"]["resource_demand"]["status"] == "met"
