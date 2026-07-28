from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "perf" / "wave3" / "run.mjs"


def _run(*args: str) -> dict:
    result = subprocess.run(
        ["node", str(RUNNER), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_contract_has_exact_distribution_and_page_20_target() -> None:
    result = _run("contract")

    assert result["count"] == 1000
    assert result["scenarioCounts"] == {
        "missing_thumb": 50,
        "pending": 10,
        "ready": 920,
        "thumb_404": 20,
    }
    assert result["search"]["page"] == 20
    assert result["search"]["targetId"] == "asset-975"


def test_legacy_browser_characterizes_unbounded_current_model() -> None:
    result = _run("legacy")

    if result["status"] == "gated":
        assert "Chrome" in result["reason"]
        return

    assert result["status"] == "measured"
    assert result["page"]["maxMountedTiles"] == 1000
    assert result["network"]["binaryRequests"] >= 10
    assert result["page"]["search"]["loadedPagesBeforeSearch"] == 20
    assert result["page"]["diagnostics"]["prewarmMaxQueueDepth"] > 32
    assert result["page"]["diagnostics"]["displayRequestsByReason"]["hover"] == 500


def test_target_browser_meets_fixed_dom_search_and_network_contracts() -> None:
    result = _run("target")

    if result["status"] == "gated":
        assert "Chrome" in result["reason"]
        return

    assert result["status"] == "measured"
    assert result["network"]["binaryRequests"] == 0
    assert result["network"]["searchRequests"] == 1
    assert result["page"]["search"]["loadedPagesBeforeSearch"] == 1
    assert result["page"]["search"]["resultIds"] == ["asset-975"]
    assert result["page"]["diagnostics"]["prewarmMaxQueueDepth"] <= 32
    assert result["page"]["diagnostics"]["displayRequestsByReason"]["hover"] == 0
    assert all(
        measurement["status"] == "met" for measurement in result["acceptance"].values()
    )
