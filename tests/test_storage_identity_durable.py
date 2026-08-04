from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess


ROOT = Path(__file__).resolve().parents[1]
STORAGE_IDENTITY = ROOT / "scripts" / "update" / "backup" / "storage_identity.sh"


def _write_findmnt(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
target_path=""
field=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -T) target_path="$2"; shift 2 ;;
        -no) field="$2"; shift 2 ;;
        *) shift ;;
    esac
done
if [ "$target_path" = "${TEST_DATA_ROOT:?}" ]; then
    target="$TEST_DATA_ROOT"
    source="//nas.example/lumen"
    fstype="cifs"
elif [ "$target_path" = "${TEST_DB_ROOT:?}" ]; then
    target="/"
    source="/dev/sdb1"
    fstype="ext4"
else
    exit 1
fi
case "$field" in
    TARGET) printf '%s\\n' "$target" ;;
    SOURCE) printf '%s\\n' "$source" ;;
    FSTYPE) printf '%s\\n' "$fstype" ;;
    *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_identity_check(
    tmp_path: Path,
    data_root: Path,
    db_root: Path,
    identity_file: Path,
) -> subprocess.CompletedProcess[str]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir(exist_ok=True)
    _write_findmnt(fakebin / "findmnt")
    env = {
        **os.environ,
        "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        "TEST_DATA_ROOT": str(data_root),
        "TEST_DB_ROOT": str(db_root),
    }
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
            log_warn() {{ :; }}
            lumen_run_as_root() {{ "$@"; }}
            . {shlex.quote(str(STORAGE_IDENTITY))}
            lumen_update_verify_split_db_identity \
                {shlex.quote(str(data_root))} \
                {shlex.quote(str(db_root))} \
                {shlex.quote(str(identity_file))}
            """,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _identity_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_root = tmp_path / "data"
    db_root = tmp_path / "database"
    state_root = tmp_path / "state"
    data_root.mkdir()
    db_root.mkdir()
    state_root.mkdir()
    return data_root, db_root, state_root / "db-identity.json"


def test_legacy_split_db_identity_upgrades_to_durable_dataset_marker(
    tmp_path: Path,
) -> None:
    data_root, db_root, identity_file = _identity_paths(tmp_path)
    identity_file.write_text(
        json.dumps(
            {
                "schema": 1,
                "db_root": str(db_root),
                "mount_target": "/",
                "mount_source": "/dev/sdb1",
                "mount_fstype": "ext4",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_identity_check(tmp_path, data_root, db_root, identity_file)

    assert result.returncode == 0, result.stderr + result.stdout
    upgraded = json.loads(identity_file.read_text(encoding="utf-8"))
    marker = (db_root / ".lumen-db-dataset-id").read_text(encoding="ascii").strip()
    assert upgraded["schema"] == 2
    assert upgraded["dataset_identity"] == marker
    assert len(marker) == 64


def test_split_db_replacement_with_same_source_and_fstype_is_rejected(
    tmp_path: Path,
) -> None:
    data_root, db_root, identity_file = _identity_paths(tmp_path)
    bound = _run_identity_check(tmp_path, data_root, db_root, identity_file)
    assert bound.returncode == 0, bound.stderr + bound.stdout

    (db_root / ".lumen-db-dataset-id").write_text(
        "f" * 64 + "\n",
        encoding="ascii",
    )
    verified = _run_identity_check(tmp_path, data_root, db_root, identity_file)

    assert verified.returncode != 0
    assert "database mount identity changed" in verified.stderr
