from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_storage_mount_up_returns_mount_failure(tmp_path: Path) -> None:
    mockbin = tmp_path / "bin"
    state_dir = tmp_path / "state"
    local_root = tmp_path / "local"
    target = tmp_path / "target"
    mockbin.mkdir()
    state_dir.mkdir()

    _write_executable(mockbin / "mountpoint", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(mockbin / "findmnt", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(
        mockbin / "mount",
        "#!/usr/bin/env bash\nprintf 'mock mount failed\\n' >&2\nexit 32\n",
    )

    (state_dir / "storage.conf").write_text(
        f"MODE=local\nLOCAL_ROOT={local_root}\n",
        encoding="utf-8",
    )
    script = Path("deploy/scripts/lumen_storage_mount.sh").resolve()
    env = {
        **os.environ,
        "PATH": f"{mockbin}{os.pathsep}{os.environ['PATH']}",
        "LUMEN_STORAGE_STATE_DIR": str(state_dir),
        "LUMEN_STORAGE_TARGET": str(target),
        "LUMEN_STORAGE_DEFAULT_LOCAL_ROOT": str(local_root),
    }

    result = subprocess.run(
        ["bash", str(script), "up"],
        cwd=script.parent.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 32
    assert "mock mount failed" in result.stderr
    assert (state_dir / "status.json").is_file()
