#!/usr/bin/env bash
# Validate the explicitly registered unmanaged-direct storage mode.

lumen_update_unmanaged_direct_storage_valid() {
    local data_root="$1"
    local state_dir="$2"
    local marker="${state_dir%/}/unmanaged-direct"

    if ! python3 - "${marker}" "${data_root}" <<'PY'
import os
import stat
import sys

marker = sys.argv[1]
data_root = sys.argv[2]

try:
    state_info = os.lstat(os.path.dirname(marker))
    marker_info = os.lstat(marker)
    target_info = os.lstat(data_root)
except OSError:
    raise SystemExit(1)

if (
    not stat.S_ISDIR(state_info.st_mode)
    or stat.S_ISLNK(state_info.st_mode)
    or not stat.S_ISREG(marker_info.st_mode)
    or stat.S_ISLNK(marker_info.st_mode)
    or marker_info.st_uid not in {0, os.geteuid()}
    or marker_info.st_mode & 0o022
    or not stat.S_ISDIR(target_info.st_mode)
    or stat.S_ISLNK(target_info.st_mode)
):
    raise SystemExit(1)

try:
    with open(marker, "rb") as handle:
        payload = handle.read(129)
except OSError:
    raise SystemExit(1)

if payload != b"schema=1\nmode=unmanaged-direct\n":
    raise SystemExit(1)
PY
    then
        return 1
    fi
    ! mountpoint -q "${data_root}" 2>/dev/null
}
