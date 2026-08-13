#!/usr/bin/env bash
# Fail-closed storage and database backing mount identity verification.

_LUMEN_STORAGE_IDENTITY_DIR="$(
    cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P
)"
_LUMEN_BACKUP_RESTORE_SERVICES="$(
    cd "${_LUMEN_STORAGE_IDENTITY_DIR}/../.." && pwd -P
)/lib/backup_restore_services.sh"
if ! command -v lumen_require_no_active_systemd_fallback_writers \
        >/dev/null 2>&1; then
    # shellcheck source=/dev/null
    . "${_LUMEN_BACKUP_RESTORE_SERVICES}"
fi
unset _LUMEN_STORAGE_IDENTITY_DIR _LUMEN_BACKUP_RESTORE_SERVICES
# shellcheck source=storage_direct.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/storage_direct.sh"

lumen_update_storage_controller_path() {
    local candidate=""
    for candidate in \
        "${LUMEN_UPDATE_STORAGE_CONTROLLER:-}" \
        "${NEW_RELEASE:+${NEW_RELEASE}/deploy/scripts/lumen_storage_mount.sh}" \
        "${ROOT:+${ROOT}/current/deploy/scripts/lumen_storage_mount.sh}" \
        "${LUMEN_LOCAL_SBIN_DIR:-/usr/local/sbin}/lumen-storage-mount"; do
        [ -n "${candidate}" ] || continue
        if [ -f "${candidate}" ] && [ ! -L "${candidate}" ] \
                && [ -r "${candidate}" ]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

lumen_update_realpath() {
    python3 - "$1" <<'PY'
import os, sys

print(os.path.realpath(sys.argv[1]))
PY
}

lumen_update_capture_exact_data_mount() {
    local data_root="$1"
    local actual_target="" actual_source="" actual_fstype="" actual_id=""
    local actual_resolved="" expected_resolved=""

    if [ ! -d "${data_root}" ] || ! mountpoint -q "${data_root}" 2>/dev/null; then
        log_error "数据根不是独立 mountpoint：${data_root}"
        return 1
    fi
    actual_target="$(findmnt -T "${data_root}" -no TARGET 2>/dev/null)" \
        || return 1
    actual_source="$(findmnt -T "${data_root}" -no SOURCE 2>/dev/null)" \
        || return 1
    actual_fstype="$(findmnt -T "${data_root}" -no FSTYPE 2>/dev/null)" \
        || return 1
    actual_id="$(findmnt -T "${data_root}" -no ID 2>/dev/null)" \
        || return 1
    if [ -z "${actual_target}" ] || [ -z "${actual_source}" ] \
            || [ -z "${actual_fstype}" ] || [ -z "${actual_id}" ]; then
        log_error "无法读取数据根 mount identity：${data_root}"
        return 1
    fi
    actual_resolved="$(lumen_update_realpath "${actual_target}")" || return 1
    expected_resolved="$(lumen_update_realpath "${data_root}")" || return 1
    if [ "${actual_resolved}" != "${expected_resolved}" ]; then
        log_error "数据根实际由父文件系统承载：expected=${expected_resolved} actual_mount=${actual_resolved}"
        return 1
    fi
    return 0
}

lumen_update_run_storage_controller() {
    local controller="$1"
    local action="$2"
    local data_root="$3"
    local db_root="$4"
    local state_dir="$5"
    lumen_run_as_root env \
        LUMEN_STORAGE_TARGET="${data_root}" \
        LUMEN_DB_ROOT="${db_root}" \
        LUMEN_STORAGE_STATE_DIR="${state_dir}" \
        LUMEN_DEPLOY_ENV_FILE="${SHARED_ENV:-}" \
        LUMEN_DOCKER_COMPOSE_DIR="${ROOT:-/opt/lumen}/current" \
        bash "${controller}" "${action}"
}

lumen_update_verify_split_db_identity() {
    local data_root="$1"
    local db_root="$2"
    local identity_file="$3"
    lumen_run_as_root python3 - \
        "${data_root}" "${db_root}" "${identity_file}" <<'PY'
from __future__ import annotations

import errno
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

data_root = os.path.realpath(sys.argv[1])
db_root = os.path.realpath(sys.argv[2])
identity_file = Path(sys.argv[3])
dataset_identity_file = Path(db_root) / ".lumen-db-dataset-id"
dataset_identity_pattern = re.compile(r"[0-9a-f]{64}")

def findmnt_value(path: str, field: str) -> str:
    result = subprocess.run(
        ["findmnt", "-T", path, "-no", field],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value or "\n" in value:
        raise SystemExit(f"cannot read {field} mount identity for {path}")
    return value

def mount_identity(path: str) -> dict[str, str]:
    target = os.path.realpath(findmnt_value(path, "TARGET"))
    try:
        contained = os.path.commonpath([path, target]) == target
    except ValueError:
        contained = False
    if not contained:
        raise SystemExit(f"mount target {target} does not contain {path}")
    return {
        "db_root": path,
        "mount_target": target,
        "mount_source": findmnt_value(path, "SOURCE"),
        "mount_fstype": findmnt_value(path, "FSTYPE"),
    }

def read_dataset_identity() -> str:
    info = dataset_identity_file.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit("database dataset identity is not a regular file")
    try:
        value = dataset_identity_file.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"cannot read database dataset identity: {exc}")
    if not dataset_identity_pattern.fullmatch(value):
        raise SystemExit("database dataset identity is invalid")
    return value

def ensure_dataset_identity() -> str:
    try:
        return read_dataset_identity()
    except FileNotFoundError:
        pass
    value = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(dataset_identity_file, flags, 0o600)
    except FileExistsError:
        return read_dataset_identity()
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
    except BaseException:
        dataset_identity_file.unlink(missing_ok=True)
        raise
    directory_fd = os.open(
        dataset_identity_file.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in {
                errno.EINVAL,
                getattr(errno, "ENOTSUP", -1),
                getattr(errno, "EOPNOTSUPP", -1),
            }:
                raise
    finally:
        os.close(directory_fd)
    return value

def write_identity(payload: dict[str, str | int]) -> None:
    parent = identity_file.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError:
        raise SystemExit("database mount identity state directory is missing")
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise SystemExit("database mount identity state directory is unsafe")
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temporary_raw = tempfile.mkstemp(
        prefix=f".{identity_file.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(temporary, identity_file)
        directory_fd = os.open(
            parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                if exc.errno not in {
                    errno.EINVAL,
                    getattr(errno, "ENOTSUP", -1),
                    getattr(errno, "EOPNOTSUPP", -1),
                }:
                    raise
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)

if not os.path.isdir(data_root) or not os.path.isdir(db_root):
    raise SystemExit("data or database root is not a directory")
data_identity = mount_identity(data_root)
db_identity = mount_identity(db_root)
same_mount = all(
    db_identity[key] == data_identity[key]
    for key in ("mount_target", "mount_source", "mount_fstype")
)
try:
    existing_info = identity_file.lstat()
except FileNotFoundError:
    existing_info = None
if existing_info is not None:
    if stat.S_ISLNK(existing_info.st_mode) or not stat.S_ISREG(existing_info.st_mode):
        raise SystemExit("database mount identity state is not a regular file")
    try:
        expected = json.loads(identity_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read database mount identity state: {exc}")
    schema = expected.get("schema")
    if schema == 1:
        observed = {"schema": 1, **db_identity}
        if expected != observed:
            raise SystemExit(
                "database mount identity changed: "
                f"expected={expected!r} observed={observed!r}"
            )
        dataset_identity = ensure_dataset_identity()
        write_identity(
            {
                "schema": 2,
                **db_identity,
                "dataset_identity": dataset_identity,
            }
        )
        print("upgraded")
        raise SystemExit(0)
    if schema != 2:
        raise SystemExit("database mount identity schema is invalid")
    observed = {
        "schema": 2,
        **db_identity,
        "dataset_identity": read_dataset_identity(),
    }
    if expected != observed:
        raise SystemExit(
            "database mount identity changed: "
            f"expected={expected!r} observed={observed!r}"
        )
    print("verified")
    raise SystemExit(0)
if same_mount:
    print("covered_by_data_root")
    raise SystemExit(0)
write_identity(
    {
        "schema": 2,
        **db_identity,
        "dataset_identity": ensure_dataset_identity(),
    }
)
print("bound")
PY
}

lumen_update_require_storage_identity() {
    local context="${1:-update}"
    local data_root="${LUMEN_DATA_ROOT:-/opt/lumendata}"
    local db_root="${LUMEN_DB_ROOT:-${data_root}}"
    local state_dir="${LUMEN_STORAGE_STATE_DIR:-/var/lib/lumen-storage}"
    local last_good="${state_dir}/last-good.conf"
    local controller="" db_identity_file="" db_identity_status=""
    local storage_mode="mounted"

    if ! lumen_require_no_active_systemd_fallback_writers; then
        log_error "[${context}] systemd 兜底 writer 仍在运行，拒绝维护数据或容器。"
        return 1
    fi
    if [ "${SKIP_STORAGE_CHECK:-0}" = "1" ]; then
        if [ "${UPDATE_STORAGE_SKIP_WARNED:-0}" -ne 1 ]; then
            log_warn "SKIP_STORAGE_CHECK=1：显式跳过 mount identity fail-closed 门禁。"
            UPDATE_STORAGE_SKIP_WARNED=1
        fi
        return 0
    fi
    case "${data_root}:${db_root}" in
        /*:/*) ;;
        *)
            log_error "[${context}] LUMEN_DATA_ROOT/LUMEN_DB_ROOT 必须是绝对路径。"
            return 1
            ;;
    esac
    controller="$(lumen_update_storage_controller_path)" || {
        log_error "[${context}] 找不到可验证 storage last-good identity 的 controller。"
        return 1
    }
    if ! lumen_update_capture_exact_data_mount "${data_root}" >/dev/null 2>&1; then
        storage_mode="unmanaged-direct"
        if ! lumen_update_unmanaged_direct_storage_valid \
                "${data_root}" "${state_dir}"; then
            log_error "[${context}] LUMEN_DATA_ROOT 既不是精确 mountpoint，也不是已验证的 unmanaged-direct 数据根。"
            return 1
        fi
        log_info "[${context}] 使用已登记的 unmanaged-direct 数据根；跳过精确 mountpoint 要求。"
    fi
    if [ -L "${last_good}" ] \
            || { [ -e "${last_good}" ] && [ ! -f "${last_good}" ]; }; then
        log_error "[${context}] storage last-good identity 文件类型不安全：${last_good}"
        return 1
    fi
    if [ "${storage_mode}" = "unmanaged-direct" ]; then
        if [ -e "${last_good}" ] || [ -L "${last_good}" ]; then
            log_error "[${context}] unmanaged-direct 数据根不能与 managed storage last-good 同时存在。"
            return 1
        fi
    else
        if [ ! -e "${last_good}" ] && [ ! -L "${last_good}" ]; then
            log_warn "[${context}] 旧部署缺少 storage last-good；仅对已精确挂载目标执行一次安全绑定。"
            if ! lumen_update_run_storage_controller "${controller}" up \
                    "${data_root}" "${db_root}" "${state_dir}"; then
                log_error "[${context}] storage last-good identity 初始化失败。"
                return 1
            fi
        fi
        if [ -f "${last_good}" ] \
                && ! grep -Eq '^DATASET_IDENTITY=[0-9a-f]{64}$' "${last_good}"; then
            log_warn "[${context}] legacy storage last-good 缺少 durable dataset identity，执行一次受锁升级。"
            if ! lumen_update_run_storage_controller "${controller}" bind-identity \
                    "${data_root}" "${db_root}" "${state_dir}"; then
                log_error "[${context}] legacy storage dataset identity 升级失败。"
                return 1
            fi
        fi
    fi
    if ! lumen_update_run_storage_controller "${controller}" verify \
            "${data_root}" "${db_root}" "${state_dir}"; then
        log_error "[${context}] storage controller verify/last-good identity 校验失败。"
        return 1
    fi
    if [ "${storage_mode}" = "unmanaged-direct" ]; then
        if ! lumen_update_unmanaged_direct_storage_valid \
                "${data_root}" "${state_dir}"; then
            log_error "[${context}] controller verify 后 unmanaged-direct 数据根状态已漂移。"
            return 1
        fi
    elif ! lumen_update_capture_exact_data_mount "${data_root}" >/dev/null 2>&1; then
        log_error "[${context}] controller verify 后数据根 mount identity 已漂移。"
        return 1
    fi

    db_identity_file="$(
        printf '%s' "${LUMEN_UPDATE_DB_ROOT_IDENTITY_FILE:-${SHARED_DIR:-${ROOT:-/opt/lumen}/shared}/.db-root.last-good.json}"
    )"
    if ! db_identity_status="$(
            lumen_update_verify_split_db_identity \
                "${data_root}" "${db_root}" "${db_identity_file}" 2>&1
        )"; then
        log_error "[${context}] LUMEN_DB_ROOT 承载文件系统 identity 校验失败：${db_identity_status}"
        return 1
    fi
    return 0
}
