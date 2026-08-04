#!/usr/bin/env bash
# Release metadata persistence and ownership hardening before service activation.

lumen_update_write_release_metadata() {
    local head="${UPDATE_MIGRATION_HEAD:-}"
    local manifest_head="" metadata="${NEW_RELEASE}/.lumen_release.json"
    if [[ ! "${head}" =~ ^[0-9A-Za-z_]+$ ]]; then
        log_error "[restart_services] 缺少已验证 UPDATE_MIGRATION_HEAD，无法声明 release schema capability。"
        return 1
    fi
    if [ -f "${NEW_RELEASE}/release-manifest.json" ] \
            && [ ! -L "${NEW_RELEASE}/release-manifest.json" ]; then
        manifest_head="$(
            python3 - "${NEW_RELEASE}/release-manifest.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
heads = payload.get("alembic_heads") if isinstance(payload, dict) else None
if (
    not isinstance(heads, list)
    or len(heads) != 1
    or not isinstance(heads[0], str)
    or not re.fullmatch(r"[0-9A-Za-z_]+", heads[0])
):
    raise SystemExit(1)
print(heads[0])
PY
        )" || return 1
        if [ "${manifest_head}" != "${head}" ]; then
            log_error "[restart_services] release manifest head=${manifest_head} 与已验证 DB head=${head} 不一致。"
            return 1
        fi
    fi
    python3 - "${metadata}" "${NEW_ID}" \
        "${RELEASE_SOURCE_COMMIT:-}" "${TARGET_RELEASE_TAG:-${TARGET_TAG:-}}" \
        "${head}" <<'PY'
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

path = Path(sys.argv[1])
payload = {
    "id": sys.argv[2],
    "sha": sys.argv[3],
    "branch": sys.argv[4],
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "alembic_head_expected": sys.argv[5],
    "alembic_head_applied": sys.argv[5],
}
fd, temporary_raw = tempfile.mkstemp(
    prefix=".lumen-release.",
    suffix=".tmp",
    dir=path.parent,
)
temporary = Path(temporary_raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fchmod(handle.fileno(), 0o644)
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

lumen_update_harden_release_ownership() {
    local shared_dir=""
    shared_dir="$(dirname "${SHARED_ENV}")"
    if ! lumen_ensure_backup_service_user \
            "${LUMEN_BACKUP_ROOT:-${LUMEN_DATA_ROOT%/}/backup}"; then
        log_error "[restart_services] 备份目录或私有 recovery journal 迁移失败。"
        return 1
    fi
    if ! lumen_release_harden_ownership \
            "${ROOT}" "${NEW_RELEASE}" "${shared_dir}" \
            "${LUMEN_APP_UID:-10001}" "${LUMEN_APP_GID:-10001}" \
            "${LUMEN_BACKUP_SERVICE_GROUP:-lumen-backup}"; then
        log_error "[restart_services] 无法把 release/shared 收紧为 root-owned。"
        return 1
    fi
    log_info "[restart_services] release/shared ownership 已收口；runtime 子目录保持应用用户可写。"
}
