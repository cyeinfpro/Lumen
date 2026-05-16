from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
WORKFLOWS = ROOT / ".github" / "workflows"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    return subprocess.run(
        ["bash", "-lc", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_safe_rm_rejects_system_and_home_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = f"""
    set +e
    export HOME={shlex.quote(str(home))}
    . {shlex.quote(str(LIB))}
    for path in / /opt /opt/ /usr /var "$HOME"; do
        if lumen_path_safe_for_rm "$path"; then
            printf 'unsafe path allowed: %s\\n' "$path" >&2
            exit 1
        fi
    done
    for path in /opt/lumendata /var/lib/lumen-data /srv/lumen-data; do
        if ! lumen_path_safe_for_rm "$path"; then
            printf 'lumen data path rejected: %s\\n' "$path" >&2
            exit 2
        fi
    done
    """

    result = _run_bash(script)

    assert result.returncode == 0, result.stderr + result.stdout


def test_github_workflow_actions_are_pinned_to_commit_sha() -> None:
    floating: list[str] = []
    floating_ref = re.compile(r"uses:\s*[^@\s]+@v\d+(?:\.\d+)?(?:\.\d+)?\b")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if floating_ref.search(line):
                floating.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

    assert floating == []
