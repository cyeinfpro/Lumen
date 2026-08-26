from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_mandatory_agent_memory_is_readable_and_tracked() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "read `MEMORY.md`" in agents
    memory = ROOT / "MEMORY.md"
    assert memory.is_file()
    assert memory.read_text(encoding="utf-8").startswith(
        "# MEMORY.md - Long-Term Memory\n"
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "MEMORY.md"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode == 0, "MEMORY.md must be tracked for clean checkouts"
