from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "perf" / "wave0" / "run.py"


def _run(*args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_realtime_characterization_covers_duplicate_and_fanout() -> None:
    result = _run("realtime", "--iterations", "20")

    assert result["api"]["fanout_channels"] == [
        "task:task-1",
        "user:user-1",
    ]
    assert result["api"]["live_live_frames_for_same_sse_id"] == 1
    assert result["api"]["replay_live_frames_for_same_sse_id"] == 0
    assert result["worker"]["fault_user_channel_reached"] is True


def test_queue_characterization_reports_command_growth() -> None:
    result = _run("queue", "--candidate-counts", "10,100", "--capacity", "4")

    assert [item["candidate_count"] for item in result["measurements"]] == [10, 100]
    assert all(item["selected_enqueue_count"] == 8 for item in result["measurements"])
    assert result["redis_command_growth_per_candidate"] >= 1.0


def test_feed_timing_is_explicitly_gated_without_environment(
    monkeypatch,
) -> None:
    monkeypatch.delenv("LUMEN_WAVE0_FEED_URL", raising=False)

    result = _run("feed", "--samples", "1", "--warmup", "0")

    assert result["status"] == "gated"
    assert "LUMEN_WAVE0_FEED_URL" in result["reason"]


def test_rss_entrypoints_cover_required_scenarios() -> None:
    result = _run("rss")

    assert set(result["scenarios"]) == {"1mp", "4k", "edit", "dual_race"}
    assert all(item["peak_rss_bytes"] > 0 for item in result["scenarios"].values())
