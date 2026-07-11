#!/usr/bin/env python3
"""Validate a restore trigger and invoke the fixed host restore script."""

from __future__ import annotations

import os
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Union


_DEFAULT_TRIGGER = Path("/opt/lumendata/backup/.restore.trigger")
_TIMESTAMP_RE = re.compile(r"^[0-9]{8}-[0-9]{6}$")
_MAX_TRIGGER_AGE = timedelta(minutes=5)
_TRUSTED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_UNSAFE_ENV_KEYS = {
    "BASH_ENV",
    "BASHOPTS",
    "CDPATH",
    "ENV",
    "GLOBIGNORE",
    "LUMEN_RESTORE_SCRIPT",
    "LUMEN_RESTORE_TRIGGER",
    "PYTHONHOME",
    "PYTHONPATH",
    "SHELLOPTS",
}


class RestoreTriggerError(ValueError):
    pass


def load_timestamp(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RestoreTriggerError("cannot open restore trigger") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > 64:
            raise RestoreTriggerError("restore trigger is not a small regular file")
        raw = os.read(fd, 65)
        if len(raw) != info.st_size:
            raise RestoreTriggerError("restore trigger changed while being read")
    finally:
        os.close(fd)
    try:
        timestamp = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RestoreTriggerError("restore timestamp is not ASCII") from exc
    if not _TIMESTAMP_RE.fullmatch(timestamp):
        raise RestoreTriggerError("restore timestamp is invalid")
    modified = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
    age = datetime.now(timezone.utc) - modified
    if age < -timedelta(minutes=1) or age > _MAX_TRIGGER_AGE:
        raise RestoreTriggerError("restore trigger is stale")
    return timestamp


def trusted_restore_script(
    runner_file: Optional[Union[str, Path]] = None,
) -> Path:
    runner = Path(runner_file or __file__).resolve(strict=True)
    return runner.with_name("restore.sh")


def sanitized_restore_environment(source: Mapping[str, str]) -> Dict[str, str]:
    env = {key: value for key, value in source.items() if key not in _UNSAFE_ENV_KEYS}
    env["PATH"] = _TRUSTED_PATH
    return env


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1:
        print("restore runner rejected trigger: unexpected arguments", file=sys.stderr)
        return 2
    trigger = Path(args[0]) if args else _DEFAULT_TRIGGER
    script = trusted_restore_script()
    try:
        timestamp = load_timestamp(trigger)
        script_info = script.lstat()
        if not stat.S_ISREG(script_info.st_mode):
            raise RestoreTriggerError("restore script is not a regular file")
    except (OSError, RestoreTriggerError) as exc:
        print(f"restore runner rejected trigger: {exc}", file=sys.stderr)
        return 2
    print(f"restore runner accepted timestamp={timestamp}", flush=True)
    os.execve(
        "/bin/bash",
        ["/bin/bash", str(script), timestamp],
        sanitized_restore_environment(os.environ),
    )
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
