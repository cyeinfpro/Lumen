"""Disposable, no-provider Canvas API -> dispatch -> worker -> GET acceptance.

Run with the existing repository Python environment. Only a new temp directory,
SQLite database, local artifacts and a private Unix-socket Redis are used.
Authentication is injected for the synthetic owner; all Canvas HTTP routes,
pricing, wallet holds, task/outbox admission and plan dispatch remain real.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def child_env(directory: Path, app: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(directory),
        "PYTHONPATH": str(ROOT / "apps" / app)
        + os.pathsep
        + str(ROOT / "packages/core"),
        "DATABASE_URL": "sqlite+aiosqlite:///" + str(directory / "state.sqlite"),
        "REDIS_URL": "unix://" + str(Path("/tmp") / (directory.name + ".sock")),
        "DATA_DIR": str(directory / "artifacts"),
        "STORAGE_ROOT": str(directory / "artifacts"),
        "APP_ENV": "test",
        "PROVIDERS": "",
        "OTEL_SDK_DISABLED": "true",
        "LUMEN_STUB_DIR": str(directory),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--video", action="store_true", help="run the local video lifecycle instead"
    )
    arguments = parser.parse_args()
    api_script = (
        "lumen_canvas_stub_video_api.py"
        if arguments.video
        else "lumen_canvas_stub_api.py"
    )
    executable = shutil.which("redis-server")
    if executable is None:
        raise RuntimeError(
            "Existing redis-server is required; no installation is attempted"
        )
    parent = ROOT / "tmp/lumen-core-ui-joint"
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="stub-chain-", dir=parent))
    # Unix socket only: no externally reachable port, credentials or persistent
    # service changes. Keep SQLite/artifacts/logs as local acceptance evidence.
    sock = Path("/tmp") / (directory.name + ".sock")
    redis = subprocess.Popen(
        [
            executable,
            "--port",
            "0",
            "--unixsocket",
            str(sock),
            "--save",
            "",
            "--appendonly",
            "no",
            "--maxmemory",
            "64mb",
        ],
        cwd=directory,
        stdout=(directory / "redis.log").open("wb"),
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 10
        while not sock.exists():
            if redis.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("disposable Redis failed to start")
            time.sleep(0.05)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / api_script),
                str(directory),
            ],
            cwd=directory,
            env=child_env(directory, "api"),
            timeout=240,
        )
        raise SystemExit(result.returncode)
    finally:
        redis.terminate()
        redis.wait(timeout=10)


if __name__ == "__main__":
    main()
