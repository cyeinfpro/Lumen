"""Regression guard for the Worker tail runtime-coupling cleanup."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from architecture_audit import collect_runtime_findings  # noqa: E402


TARGETS = (
    "apps/worker/app/account_limiter.py",
    "apps/worker/app/byok_runtime.py",
    "apps/worker/app/http_retry.py",
    "apps/worker/app/outbox/contracts.py",
    "apps/worker/app/sse_publish.py",
    "apps/worker/app/storage.py",
    "apps/worker/app/tasks/canvas_execution_reconcile.py",
)


def test_worker_tail_has_no_runtime_coupling_inventory() -> None:
    findings = collect_runtime_findings(tuple(ROOT / target for target in TARGETS))

    assert findings == {}
