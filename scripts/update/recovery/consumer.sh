#!/usr/bin/env bash
# Durable wake-up marker for automatic update journal resume.

lumen_update_recovery_marker_path() {
    if [ -n "${LUMEN_UPDATE_RECOVERY_MARKER:-}" ]; then
        printf '%s\n' "${LUMEN_UPDATE_RECOVERY_MARKER}"
    else
        printf '%s\n' "${SHARED_DIR:?}/.update-resume"
    fi
}

lumen_update_recovery_marker_write() {
    python3 - "$(lumen_update_recovery_marker_path)" "${OPERATION_ID:?}" <<'PY'
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile

path = Path(sys.argv[1])
operation_id = sys.argv[2]
path.parent.mkdir(parents=True, exist_ok=True)
try:
    existing = path.lstat()
except FileNotFoundError:
    existing = None
if existing is not None and not stat.S_ISREG(existing.st_mode):
    raise SystemExit("update recovery marker is not a regular file")
fd, temporary_raw = tempfile.mkstemp(
    prefix=f".{path.name}.",
    suffix=".tmp",
    dir=path.parent,
)
temporary = Path(temporary_raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{operation_id}\n")
        os.fchmod(handle.fileno(), 0o600)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                raise
    finally:
        os.close(directory_fd)
finally:
    temporary.unlink(missing_ok=True)
PY
}

lumen_update_recovery_marker_clear() {
    python3 - "$(lumen_update_recovery_marker_path)" <<'PY'
import errno
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
try:
    metadata = path.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("update recovery marker is not a regular file")
path.unlink()
directory_fd = os.open(
    path.parent,
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
)
try:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
            raise
finally:
    os.close(directory_fd)
PY
}

lumen_update_journal_status() {
    [ "${LUMEN_UPDATE_JOURNAL_READY}" = "1" ] || return 0
    lumen_update_journal_refresh_state
    if ! lumen_update_journal_exec status "$1"; then
        return 1
    fi
    lumen_update_recovery_marker_clear
}
