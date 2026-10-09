"""History ref rewrites must never publish old images or release aliases."""

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/docker-release.yml"


def test_every_docker_release_job_rejects_forced_push() -> None:
    jobs = WORKFLOW.read_text().split("\njobs:\n", 1)[1]
    blocks = re.split(r"(?m)(?=^  [A-Za-z0-9_-]+:\s*$)", jobs)
    found = 0
    for block in blocks:
        if not re.match(r"^  [A-Za-z0-9_-]+:", block):
            continue
        condition = re.search(r"(?m)^    if: (.+)$", block)
        assert condition, block.splitlines()[0]
        assert "github.event_name != 'push' || !github.event.forced" in condition[1]
        found += 1
    assert found > 0


def test_forced_push_guard_keeps_normal_release_events() -> None:
    for event in ("push", "workflow_dispatch", "pull_request"):
        for forced in (False, True):
            allowed = event != "push" or not forced
            assert allowed is not (event == "push" and forced)
