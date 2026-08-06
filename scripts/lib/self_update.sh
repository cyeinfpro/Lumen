#!/usr/bin/env bash
# Commit-bound script self-update transaction for scripts/lib.sh.

lumen_validate_self_update_file() {
    local relative="$1"
    local path="$2"
    local first_line=""
    if [ ! -f "${path}" ] || [ -L "${path}" ]; then
        return 1
    fi
    IFS= read -r first_line < "${path}" || true
    case "${relative}" in
        *.sh)
            case "${first_line}" in
                '#!'*bash*) ;;
                *) return 1 ;;
            esac
            bash -n "${path}" >/dev/null 2>&1
            ;;
        *.py)
            case "${first_line}" in
                '#!'*)
                    case "${first_line}" in
                        '#!'*python3*) ;;
                        *) return 1 ;;
                    esac
                    ;;
            esac
            command -v python3 >/dev/null 2>&1 || return 1
            python3 - "${path}" <<'PY' >/dev/null 2>&1
from pathlib import Path
import sys

path = Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
compile(source, str(path), "exec")
PY
            ;;
        *)
            return 1
            ;;
    esac
}

lumen_self_update_file_mode() {
    case "${1:-}" in
        backup.sh|restore.sh|update.sh|install.sh|uninstall.sh|lumenctl.sh|migrate_to_releases.sh)
            printf '0755'
            ;;
        *)
            printf '0644'
            ;;
    esac
}

lumen_self_update_path_mode() {
    local path="$1"
    local raw=""
    if raw="$(stat -c '%a' "${path}" 2>/dev/null)" && [ -n "${raw}" ]; then
        :
    elif raw="$(stat -f '%Lp' "${path}" 2>/dev/null)" && [ -n "${raw}" ]; then
        :
    else
        return 1
    fi
    case "${raw}" in
        755|0755) printf '0755' ;;
        644|0644) printf '0644' ;;
        *) printf '%s' "${raw}" ;;
    esac
}

lumen_self_update_write_integrity_manifest() {
    local source_dir="$1"
    local commit_sha="$2"
    local files_list="$3"
    local output="$4"
    python3 - "${source_dir}" "${commit_sha}" "${files_list}" "${output}" <<'PY'
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys

source_dir = Path(sys.argv[1])
commit = sys.argv[2]
files_list = Path(sys.argv[3])
output = Path(sys.argv[4])
executable = {
    "backup.sh",
    "restore.sh",
    "update.sh",
    "install.sh",
    "uninstall.sh",
    "lumenctl.sh",
    "migrate_to_releases.sh",
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


records = []
seen = set()
for raw in files_list.read_text(encoding="utf-8").splitlines():
    relative = raw.strip()
    parsed = PurePosixPath(relative)
    if (
        not relative
        or parsed.is_absolute()
        or ".." in parsed.parts
        or relative in seen
    ):
        raise SystemExit("invalid self-update integrity path")
    seen.add(relative)
    source = source_dir / relative
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit("self-update integrity source is not a regular file")
    mode = "0755" if relative in executable else "0644"
    records.append(
        {
            "commit": commit,
            "path": relative,
            "type": "file",
            "mode": mode,
            "hash": digest(source),
        }
    )

with output.open("w", encoding="utf-8", newline="\n") as handle:
    for record in records:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
PY
}

lumen_self_update_integrity_valid() {
    local scripts_dir="$1"
    local commit_sha="$2"
    local manifest="$3"
    shift 3
    [ -f "${manifest}" ] && [ ! -L "${manifest}" ] || return 1
    python3 - "${scripts_dir}" "${commit_sha}" "${manifest}" "$@" <<'PY' \
        >/dev/null 2>&1
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import sys

scripts_dir = Path(sys.argv[1])
expected_commit = sys.argv[2]
manifest = Path(sys.argv[3])
requested = set(sys.argv[4:])
expected_keys = {"commit", "path", "type", "mode", "hash"}
records = {}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


for line in manifest.read_text(encoding="utf-8").splitlines():
    record = json.loads(line)
    if not isinstance(record, dict) or set(record) != expected_keys:
        raise SystemExit(1)
    relative = record.get("path")
    if not isinstance(relative, str):
        raise SystemExit(1)
    parsed = PurePosixPath(relative)
    if (
        not relative
        or parsed.is_absolute()
        or ".." in parsed.parts
        or relative in records
    ):
        raise SystemExit(1)
    if (
        record.get("commit") != expected_commit
        or record.get("type") != "file"
        or record.get("mode") not in {"0644", "0755"}
    ):
        raise SystemExit(1)
    target = scripts_dir / relative
    info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit(1)
    if f"{stat.S_IMODE(info.st_mode):04o}" != record["mode"]:
        raise SystemExit(1)
    if digest(target) != record.get("hash"):
        raise SystemExit(1)
    records[relative] = record

if not requested.issubset(records):
    raise SystemExit(1)
PY
}

lumen_self_update_release_tag_is_older() {
    python3 - "$1" "$2" <<'PY' >/dev/null 2>&1
import re
import sys


def parse(value):
    match = re.fullmatch(
        r"v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?",
        value,
    )
    if not match:
        raise SystemExit(2)
    core = tuple(int(item) for item in match.group(1, 2, 3))
    prerelease = match.group(4)
    if prerelease is None:
        return core, None
    parts = []
    for item in prerelease.split("."):
        parts.append((0, int(item)) if item.isdigit() else (1, item))
    return core, tuple(parts)


candidate = parse(sys.argv[1])
floor = parse(sys.argv[2])
if candidate[0] != floor[0]:
    raise SystemExit(0 if candidate[0] < floor[0] else 1)
if candidate[1] is None:
    raise SystemExit(1)
if floor[1] is None:
    raise SystemExit(0)
raise SystemExit(0 if candidate[1] < floor[1] else 1)
PY
}

lumen_self_update_transaction_lock_path() {
    local scripts_dir="${1%/}"
    local physical_dir=""
    if physical_dir="$(cd "${scripts_dir}" 2>/dev/null && pwd -P)"; then
        scripts_dir="${physical_dir}"
    fi
    printf '%s.lumen-self-update.lock\n' "${scripts_dir}"
}

lumen_self_update_outer_lock_valid() {
    local lock_path="$1"
    local fd="${LUMEN_SCRIPT_UNIT_LOCK_FD:-}"
    [ "${LUMEN_SCRIPT_UNIT_LOCK_PATH:-}" = "${lock_path}" ] || return 1
    case "${fd}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    python3 - "${fd}" "${lock_path}" <<'PY'
import fcntl
import os
from pathlib import Path
import stat
import sys

fd = int(sys.argv[1])
path = Path(sys.argv[2])
try:
    opened = os.fstat(fd)
    current = os.stat(path, follow_symlinks=False)
except OSError:
    raise SystemExit(1)
if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
    raise SystemExit(1)
if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
    raise SystemExit(1)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    raise SystemExit(1)
PY
}

lumen_self_update_acquire_transaction_lock() {
    local lock_path="$1"
    local timeout="$2"
    local rc=0
    LUMEN_SELF_UPDATE_LOCK_BORROWED=0
    if lumen_self_update_outer_lock_valid "${lock_path}"; then
        LUMEN_SELF_UPDATE_LOCK_BORROWED=1
        return 0
    fi
    if ! exec 18<>"${lock_path}"; then
        return 1
    fi
    if python3 - 18 "${lock_path}" "${timeout}" <<'PY'
import fcntl
import os
from pathlib import Path
import stat
import sys
import time

fd = int(sys.argv[1])
path = Path(sys.argv[2])
try:
    timeout = max(0.0, float(sys.argv[3]))
except ValueError:
    timeout = 60.0
opened = os.fstat(fd)
current = os.stat(path, follow_symlinks=False)
if (
    not stat.S_ISREG(opened.st_mode)
    or not stat.S_ISREG(current.st_mode)
    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    or opened.st_uid != os.geteuid()
):
    raise SystemExit(1)
os.fchmod(fd, 0o600)
deadline = time.monotonic() + timeout
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline:
            raise SystemExit(75)
        time.sleep(0.05)
PY
    then
        return 0
    else
        rc=$?
        exec 18>&-
        return "${rc}"
    fi
}

lumen_self_update_release_transaction_lock() {
    if [ "${LUMEN_SELF_UPDATE_LOCK_BORROWED:-0}" = "1" ]; then
        return 0
    fi
    exec 18>&- 2>/dev/null || true
}

lumen_self_update_restore_saved_trap() {
    local saved_trap="$1"
    local signal_name="$2"
    if command -v lumen_restore_saved_trap >/dev/null 2>&1; then
        lumen_restore_saved_trap "${saved_trap}" "${signal_name}"
    elif [ -n "${saved_trap}" ]; then
        eval "${saved_trap}"
    else
        trap - "${signal_name}"
    fi
}

lumen_self_update_return_status() {
    return "$1"
}

lumen_self_update_run_saved_exit_trap() {
    local saved_exit="$1"
    local rc="$2"
    local saved_command=""
    if command -v lumen_run_saved_exit_trap >/dev/null 2>&1; then
        lumen_run_saved_exit_trap "${saved_exit}" "${rc}"
        return 0
    fi
    [ -n "${saved_exit}" ] || return 0
    case "${saved_exit}" in
        "trap -- "*) ;;
        *) return 0 ;;
    esac
    eval "set -- ${saved_exit#trap -- }"
    [ "$#" -ge 2 ] || return 0
    saved_command="$1"
    (
        set +e
        lumen_self_update_return_status "${rc}"
        eval "${saved_command}"
    ) || true
}

lumen_self_update_transaction_helper() {
    local helper=""
    helper="$(
        cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null \
            && pwd -P
    )/install/bootstrap_transaction.py"
    [ -f "${helper}" ] && [ ! -L "${helper}" ] || return 1
    printf '%s\n' "${helper}"
}

lumen_self_update_recover_transactions() {
    local scripts_dir="$1"
    local transaction="" helper=""
    for transaction in "${scripts_dir}.txn."*; do
        [ -d "${transaction}" ] || continue
        if [ -f "${transaction}/bootstrap_transaction.py" ] \
                && [ ! -L "${transaction}/bootstrap_transaction.py" ]; then
            helper="${transaction}/bootstrap_transaction.py"
        else
            helper="$(lumen_self_update_transaction_helper)" || return 1
        fi
        log_warn "[self_update] 恢复未完成 scripts transaction：${transaction}。"
        if [ -f "${transaction}/intent.json" ]; then
            python3 "${helper}" recover-held "${transaction}" || return 1
        else
            rm -rf "${transaction}" || return 1
        fi
    done
}

# Build a complete sibling scripts tree and publish it with one directory
# exchange. Catchable failures leave the active tree untouched; SIGKILL or a
# host crash can only leave the complete old tree or the complete new tree.
lumen_self_update_install_transaction() (
    scripts_dir="$1"
    download_dir="$2"
    backup_ts="$3"
    commit_sha="$4"
    all_files_list="$5"
    changed_files_list="$6"
    source_tag="${7:-}"
    transaction_dir=""
    staged_tree=""
    committed=0
    replacement_started=0
    f=""
    target=""
    staged_target=""
    target_dir=""
    backup=""
    stage=""
    mode=""
    index=0
    marker=""
    marker_stage=""
    marker_backup=""
    changed_files=()
    marker_paths=()
    marker_stages=()

    while IFS= read -r f; do
        [ -n "${f}" ] && changed_files+=("${f}")
    done < "${changed_files_list}"

    umask 077
    transaction_dir="$(
        mktemp -d "${scripts_dir}.txn.XXXXXXXXXX" 2>/dev/null
    )" || exit 1
    if ! chmod 0700 "${transaction_dir}"; then
        rm -rf "${transaction_dir}" 2>/dev/null || true
        exit 1
    fi
    staged_tree="${transaction_dir}/active"
    if ! cp -a "${scripts_dir}" "${staged_tree}"; then
        rm -rf "${transaction_dir}" 2>/dev/null || true
        exit 1
    fi
    export LUMEN_SELF_UPDATE_TRANSACTION_PID="${BASHPID:-$$}"

    # shellcheck disable=SC2329  # Installed as the EXIT trap below.
    _lumen_self_update_finish() {
        local rc=$?
        local recovery_rc=0 helper=""
        trap - EXIT
        trap '' INT TERM HUP
        if [ ! -d "${transaction_dir}" ]; then
            committed=1
        fi
        if [ "${committed}" -ne 1 ]; then
            if [ "${replacement_started}" -eq 1 ]; then
                log_warn "[self_update] 安装事务失败，正在恢复全部 scripts 文件。"
            fi
            if [ -f "${transaction_dir}/intent.json" ]; then
                helper="$(lumen_self_update_transaction_helper)" \
                    || recovery_rc=$?
                if [ "${recovery_rc}" -eq 0 ]; then
                    LUMEN_BOOTSTRAP_TRACE_FILE="${LUMEN_SELF_UPDATE_TRACE_FILE:-}" \
                    LUMEN_BOOTSTRAP_FAILPOINT="" \
                        python3 "${helper}" recover-held \
                            "${transaction_dir}" || recovery_rc=$?
                fi
            else
                rm -rf "${transaction_dir}" 2>/dev/null || recovery_rc=$?
            fi
        fi
        if [ "${recovery_rc}" -ne 0 ]; then
            log_warn "[self_update] scripts transaction 自动恢复失败：${transaction_dir}。"
            exit 70
        fi
        exit "${rc}"
    }

    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap '_lumen_self_update_finish' EXIT

    mkdir -p "${transaction_dir}/staged" "${transaction_dir}/markers" \
        || exit 1

    for f in ${changed_files[@]+"${changed_files[@]}"}; do
        target="${scripts_dir}/${f}"
        staged_target="${staged_tree}/${f}"
        target_dir="$(dirname "${staged_target}")"
        if ! mkdir -p "${target_dir}"; then
            log_warn "[self_update] 无法创建目标目录：${target_dir}。"
            exit 1
        fi
        if [ -L "${target}" ] || { [ -e "${target}" ] && [ ! -f "${target}" ]; }; then
            log_warn "[self_update] 目标不是普通文件：${target}。"
            exit 1
        fi

        index=$((index + 1))
        backup=""
        if [ -f "${target}" ]; then
            backup="${staged_target}.bak.${backup_ts}"
            if [ -e "${backup}" ] || [ -L "${backup}" ]; then
                log_warn "[self_update] 备份路径已存在，拒绝覆盖：${backup}。"
                exit 1
            fi
            if ! cp -a "${target}" "${backup}" \
                    || ! cmp -s "${target}" "${backup}"; then
                log_warn "[self_update] 备份失败，未开始替换：${target}。"
                exit 1
            fi
        fi

        stage="${transaction_dir}/staged/$(printf '%04d' "${index}")"
        mode="$(lumen_self_update_file_mode "${f}")"
        if ! cp -a "${download_dir}/${f}" "${stage}" \
                || ! chmod "${mode}" "${stage}" \
                || ! lumen_validate_self_update_file "${f}" "${stage}"; then
            log_warn "[self_update] staging/权限校验失败：${f}。"
            exit 1
        fi
        replacement_started=1
        if ! mkdir -p "$(dirname "${staged_target}")" \
                || ! mv -f "${stage}" "${staged_target}"; then
            log_warn "[self_update] staged tree 替换失败：${staged_target}。"
            exit 1
        fi
    done

    marker_paths=(
        "${staged_tree}/.lumen-self-update.files"
        "${staged_tree}/.lumen-self-update.source"
        "${staged_tree}/.lumen-self-update.last"
        "${staged_tree}/.lumen-self-update.integrity"
        "${staged_tree}/.lumen-self-update.release-tag"
    )
    index=0
    for marker in "${marker_paths[@]}"; do
        index=$((index + 1))
        marker_stage="${transaction_dir}/markers/staged.${index}"
        mkdir -p "$(dirname "${marker_stage}")" || exit 1
        case "${index}" in
            1) sort -u "${all_files_list}" > "${marker_stage}" || exit 1 ;;
            2) printf '%s\n' "${commit_sha}" > "${marker_stage}" || exit 1 ;;
            3) date -u +%s > "${marker_stage}" || exit 1 ;;
            4)
                lumen_self_update_write_integrity_manifest \
                    "${download_dir}" \
                    "${commit_sha}" \
                    "${all_files_list}" \
                    "${marker_stage}" || exit 1
                ;;
            5) printf '%s\n' "${source_tag}" > "${marker_stage}" || exit 1 ;;
        esac
        if ! chmod 0600 "${marker_stage}"; then
            log_warn "[self_update] marker 权限设置失败：${marker}。"
            exit 1
        fi
        marker_stages+=("${marker_stage}")
    done

    for ((index = 0; index < ${#marker_paths[@]}; index++)); do
        if ! mv -f "${marker_stages[$index]}" "${marker_paths[$index]}"; then
            log_warn "[self_update] marker 提交失败：${marker_paths[$index]}。"
            exit 1
        fi
    done

    local transaction_helper=""
    if [ -f "${download_dir}/install/bootstrap_transaction.py" ] \
            && [ ! -L "${download_dir}/install/bootstrap_transaction.py" ]; then
        transaction_helper="${download_dir}/install/bootstrap_transaction.py"
    else
        transaction_helper="$(lumen_self_update_transaction_helper)" || exit 1
    fi
    if ! LUMEN_BOOTSTRAP_TRACE_FILE="${LUMEN_SELF_UPDATE_TRACE_FILE:-}" \
            LUMEN_BOOTSTRAP_FAILPOINT="${LUMEN_SELF_UPDATE_FAILPOINT:-}" \
            python3 "${transaction_helper}" commit-staged \
                "${scripts_dir}" "${staged_tree}" "${transaction_dir}"; then
        log_warn "[self_update] scripts 目录原子切换失败。"
        exit 1
    fi
    committed=1
    exit 0
)

_lumen_self_update_scripts_locked() {
    LUMEN_SELF_UPDATE_RESULT=skipped
    LUMEN_SELF_UPDATE_CHANGED=""
    LUMEN_SELF_UPDATE_BACKUP_TS=""
    LUMEN_SELF_UPDATE_SOURCE=""
    LUMEN_SELF_UPDATE_SOURCE_TAG=""
    LUMEN_SELF_UPDATE_SOURCE_COMMIT=""

    local scripts_dir="${1:-}"
    local source_ref="${2:-${LUMEN_SELF_UPDATE_REF:-}}"
    local ttl_sec="${3:-${LUMEN_SELF_UPDATE_TTL:-600}}"
    if [ "$#" -gt 3 ]; then
        shift 3
    else
        shift "$#"
    fi
    local files=("$@")
    local module_files=(
        lib/system.sh
        lib/environment.sh
        lib/step_protocol.sh
        lib/runtime.sh
        lib/locking.sh
        lib/container_release.sh
        lib/release_layout.sh
        lib/self_update.sh
        lib/backup_restore_services.sh
        lib/backup_journal.sh
        lib/restore_journal.sh
    )
    local python_helper_files=(
        install/bootstrap_transaction.py
        release_manifest_guard.py
        update_runner.py
        restore_runner.py
        update/entry_lock.py
        redis_backup_archive.py
        backup_permissions.py
        restore_journal.py
    )
    if [ "${#files[@]}" -eq 0 ]; then
        files=(
            lib.sh
            lib/system.sh
            lib/environment.sh
            lib/step_protocol.sh
            lib/runtime.sh
            lib/locking.sh
            lib/container_release.sh
            lib/release_layout.sh
            lib/self_update.sh
            lib/backup_restore_services.sh
            lib/backup_journal.sh
            lib/restore_journal.sh
            install/bootstrap_transaction.py
            release_manifest_guard.py
            update_runner.py
            restore_runner.py
            redis_backup_archive.py
            backup_permissions.py
            restore_journal.py
            backup.sh
            restore.sh
            update.sh
            update/recovery/consumer.sh
            lumenctl.sh
        )
    else
        # Facade/modules/runners are one version unit, including legacy callers.
        local requested include_modules=0 include_python_helpers=0 module helper present
        for requested in "${files[@]}"; do
            if [ "${requested}" = "lib.sh" ]; then
                include_modules=1
            fi
            case "${requested}" in
                lib.sh|lib/self_update.sh|update.sh|lumenctl.sh)
                    include_python_helpers=1
                    ;;
            esac
        done
        if [ "${include_modules}" -eq 1 ]; then
            for module in "${module_files[@]}"; do
                present=0
                for requested in "${files[@]}"; do
                    if [ "${requested}" = "${module}" ]; then
                        present=1
                        break
                    fi
                done
                if [ "${present}" -eq 0 ]; then
                    files+=("${module}")
                fi
            done
        fi
        if [ "${include_python_helpers}" -eq 1 ]; then
            for helper in "${python_helper_files[@]}"; do
                present=0
                for requested in "${files[@]}"; do
                    if [ "${requested}" = "${helper}" ]; then
                        present=1
                        break
                    fi
                done
                if [ "${present}" -eq 0 ]; then
                    files+=("${helper}")
                fi
            done
        fi
        for requested in "${files[@]}"; do
            if [ "${requested}" = "backup.sh" ]; then
                present=0
                for helper in "${files[@]}"; do
                    if [ "${helper}" = "lib/backup_journal.sh" ]; then
                        present=1
                        break
                    fi
                done
                if [ "${present}" -eq 0 ]; then
                    files+=("lib/backup_journal.sh")
                fi
                break
            fi
        done
        for requested in "${files[@]}"; do
            if [ "${requested}" = "update.sh" ]; then
                present=0
                for helper in "${files[@]}"; do
                    if [ "${helper}" = "update/recovery/consumer.sh" ]; then
                        present=1
                        break
                    fi
                done
                if [ "${present}" -eq 0 ]; then
                    files+=("update/recovery/consumer.sh")
                fi
                break
            fi
        done
    fi

    # Install dependencies before facade/update entrypoints.
    local ordered_files=()
    for module in "${module_files[@]}"; do
        for requested in "${files[@]}"; do
            if [ "${requested}" = "${module}" ]; then
                ordered_files+=("${requested}")
                break
            fi
        done
    done
    for helper in "${python_helper_files[@]}"; do
        for requested in "${files[@]}"; do
            if [ "${requested}" = "${helper}" ]; then
                ordered_files+=("${requested}")
                break
            fi
        done
    done
    for requested in "${files[@]}"; do
        present=0
        for module in "${module_files[@]}"; do
            if [ "${requested}" = "${module}" ]; then
                present=1
                break
            fi
        done
        if [ "${present}" -eq 0 ]; then
            for helper in "${python_helper_files[@]}"; do
                if [ "${requested}" = "${helper}" ]; then
                    present=1
                    break
                fi
            done
        fi
        if [ "${present}" -eq 0 ]; then
            ordered_files+=("${requested}")
        fi
    done
    files=("${ordered_files[@]}")

    if [ "${LUMEN_SELF_UPDATE:-1}" = "0" ]; then
        LUMEN_SELF_UPDATE_RESULT=disabled
        return 0
    fi
    if [ -z "${scripts_dir}" ] || [ ! -d "${scripts_dir}" ]; then
        LUMEN_SELF_UPDATE_RESULT=skipped
        return 0
    fi

    local expected_commit="${LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT:-}"
    local release_tag=""
    local commit_sha="${expected_commit:-${LUMEN_SELF_UPDATE_COMMIT:-}}"
    if [ -n "${expected_commit}" ] \
            && [[ ! "${expected_commit}" =~ ^[0-9a-f]{40}$ ]]; then
        log_warn "[self_update] LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT 不是有效的 40 位 commit。"
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
    if [[ "${source_ref}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
        release_tag="${source_ref}"
        local manifest_file="${LUMEN_SELF_UPDATE_MANIFEST_FILE:-}"
        local manifest_tmp=""
        if [ -z "${manifest_file}" ]; then
            manifest_tmp="$(mktemp 2>/dev/null)" || {
                LUMEN_SELF_UPDATE_RESULT=failed
                return 0
            }
            if ! command -v lumen_fetch_release_manifest >/dev/null 2>&1 \
                    || ! lumen_fetch_release_manifest "${release_tag}" "${manifest_tmp}"; then
                rm -f "${manifest_tmp}" 2>/dev/null || true
                log_warn "[self_update] 无法获取 ${release_tag} 的 release manifest，拒绝覆盖脚本。"
                LUMEN_SELF_UPDATE_RESULT=failed
                return 0
            fi
            manifest_file="${manifest_tmp}"
        fi
        local manifest_commit=""
        manifest_commit="$(python3 - "${manifest_file}" "${release_tag}" <<'PY' 2>/dev/null || true
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
commit = payload.get("commit_sha")
if payload.get("version") != sys.argv[2] or not isinstance(commit, str):
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit(1)
print(commit)
PY
)"
        [ -n "${manifest_tmp}" ] && rm -f "${manifest_tmp}" 2>/dev/null || true
        if [ -z "${manifest_commit}" ] \
                || { [ -n "${commit_sha}" ] && [ "${commit_sha}" != "${manifest_commit}" ]; }; then
            log_warn "[self_update] ${release_tag} 的 release commit 无效或与预期不一致，拒绝覆盖脚本。"
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        commit_sha="${manifest_commit}"
    elif [[ "${source_ref}" =~ ^[0-9a-f]{40}$ ]]; then
        if [ -n "${commit_sha}" ] && [ "${commit_sha}" != "${source_ref}" ]; then
            log_warn "[self_update] commit 与 LUMEN_SELF_UPDATE_COMMIT 不一致，拒绝覆盖脚本。"
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        commit_sha="${source_ref}"
    elif [ -z "${source_ref}" ] && [[ "${commit_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        :
    else
        log_warn "[self_update] source=${source_ref:-<empty>} 不是具体 release tag/commit；拒绝从可变 branch 覆盖脚本。"
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
    if [[ ! "${commit_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        log_warn "[self_update] 未解析到有效的 40 位 release commit，拒绝覆盖脚本。"
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
    # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
    LUMEN_SELF_UPDATE_SOURCE_TAG="${release_tag}"
    LUMEN_SELF_UPDATE_SOURCE_COMMIT="${commit_sha}"
    LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT="${commit_sha}"
    export LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT

    local marker="${scripts_dir}/.lumen-self-update.last"
    local coverage_marker="${scripts_dir}/.lumen-self-update.files"
    local source_marker="${scripts_dir}/.lumen-self-update.source"
    local integrity_marker="${scripts_dir}/.lumen-self-update.integrity"
    local release_tag_marker="${scripts_dir}/.lumen-self-update.release-tag"
    local release_tag_floor=""
    local release_tag_marker_value="${release_tag}"
    local candidate_floor=""
    for candidate_floor in \
            "${scripts_dir}/../.image-tag" \
            "${release_tag_marker}"; do
        if [ -L "${candidate_floor}" ] \
                || { [ -e "${candidate_floor}" ] && [ ! -f "${candidate_floor}" ]; }; then
            log_warn "[self_update] release tag floor 不是普通文件：${candidate_floor}。"
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        [ -f "${candidate_floor}" ] || continue
        release_tag_floor="$(
            head -n1 "${candidate_floor}" 2>/dev/null | tr -d '[:space:]'
        )"
        if [ -z "${release_tag_marker_value}" ] \
                && [[ "${release_tag_floor}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
            release_tag_marker_value="${release_tag_floor}"
        fi
        if [ -n "${release_tag}" ] \
                && [[ "${release_tag_floor}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] \
                && lumen_self_update_release_tag_is_older \
                    "${release_tag}" "${release_tag_floor}"; then
            log_warn "[self_update] 拒绝 scripts release tag 降级：requested=${release_tag} installed=${release_tag_floor}。"
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
    done
    local coverage_complete=1
    if [ ! -f "${coverage_marker}" ] || [ -L "${coverage_marker}" ]; then
        coverage_complete=0
    else
        for requested in "${files[@]}"; do
            if ! grep -Fxq "${requested}" "${coverage_marker}" 2>/dev/null; then
                coverage_complete=0
                break
            fi
        done
    fi
    if [ ! -f "${source_marker}" ] || [ -L "${source_marker}" ] \
            || [ "$(cat "${source_marker}" 2>/dev/null || true)" != "${commit_sha}" ]; then
        coverage_complete=0
    fi
    if [ "${coverage_complete}" -eq 1 ] \
            && ! lumen_self_update_integrity_valid \
                "${scripts_dir}" \
                "${commit_sha}" \
                "${integrity_marker}" \
                "${files[@]}"; then
        coverage_complete=0
    fi
    if [ "${LUMEN_SELF_UPDATE_FORCE:-0}" != "1" ] \
            && [ "${coverage_complete}" -eq 1 ] \
            && [ -f "${marker}" ] \
            && [ ! -L "${marker}" ]; then
        local last_ts now_ts age
        last_ts="$(cat "${marker}" 2>/dev/null || echo 0)"
        case "${last_ts}" in
            ''|*[!0-9]*) last_ts=0 ;;
        esac
        now_ts="$(date -u +%s)"
        age=$((now_ts - last_ts))
        if [ "${ttl_sec}" -gt 0 ] && [ "${age}" -lt "${ttl_sec}" ] && [ "${age}" -ge 0 ]; then
            LUMEN_SELF_UPDATE_RESULT=skipped
            return 0
        fi
    fi

    local repo_url="${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git}"
    local owner_repo="" raw_base=""
    owner_repo="$(lumen_github_repo_slug "${repo_url}")" || true
    if [ -z "${owner_repo}" ]; then
        if command -v log_warn >/dev/null 2>&1; then
            log_warn "[self_update] LUMEN_REPO_URL 不是 https://github.com/<owner>/<repo>(.git)：${repo_url}，跳过。"
        fi
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
    raw_base="https://raw.githubusercontent.com/${owner_repo}/${commit_sha}/scripts"
    # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
    LUMEN_SELF_UPDATE_SOURCE="${raw_base}"

    local proxy_url=""
    proxy_url="$(lumen_effective_proxy_url "${SHARED_ENV:-}" 2>/dev/null || true)"
    local curl_cmd=(curl -fsSL --connect-timeout 10 --max-time 60)
    if [ -n "${proxy_url}" ]; then
        curl_cmd+=(--proxy "${proxy_url}")
    fi

    local tmp_dir
    tmp_dir="$(mktemp -d 2>/dev/null)" || { LUMEN_SELF_UPDATE_RESULT=failed; return 0; }

    local f
    for f in "${files[@]}"; do
        case "${f}" in
            ''|.|..|/*|../*|*/../*|*/..|*[!A-Za-z0-9_./-]*)
                if command -v log_warn >/dev/null 2>&1; then
                    log_warn "[self_update] 非法脚本相对路径：${f:-<empty>}，跳过 self-update。"
                fi
                rm -rf "${tmp_dir}" 2>/dev/null || true
                LUMEN_SELF_UPDATE_RESULT=failed
                return 0
                ;;
        esac
        if ! mkdir -p "$(dirname "${tmp_dir}/${f}")"; then
            if command -v log_warn >/dev/null 2>&1; then
                log_warn "[self_update] 无法创建临时模块目录：$(dirname "${tmp_dir}/${f}")。"
            fi
            rm -rf "${tmp_dir}" 2>/dev/null || true
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        if ! "${curl_cmd[@]}" "${raw_base}/${f}" -o "${tmp_dir}/${f}" 2>/dev/null; then
            if command -v log_warn >/dev/null 2>&1; then
                log_warn "[self_update] 下载 ${f} 失败（GitHub 不可达？），跳过 self-update（继续用本地脚本）。"
            fi
            rm -rf "${tmp_dir}" 2>/dev/null || true
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        if ! lumen_validate_self_update_file "${f}" "${tmp_dir}/${f}"; then
            if command -v log_warn >/dev/null 2>&1; then
                case "${f}" in
                    *.sh)
                        log_warn "[self_update] ${f} 不是有效 bash 脚本，跳过 self-update。"
                        ;;
                    *.py)
                        log_warn "[self_update] ${f} 不是有效 Python 3 helper，跳过 self-update。"
                        ;;
                    *)
                        log_warn "[self_update] ${f} 文件类型不在 self-update 允许列表，跳过。"
                        ;;
                esac
            fi
            rm -rf "${tmp_dir}" 2>/dev/null || true
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
    done

    LUMEN_SELF_UPDATE_BACKUP_TS="$(
        printf '%s.%s.%s' \
            "$(date -u +%Y%m%d-%H%M%S)" \
            "${BASHPID:-$$}" \
            "${RANDOM:-0}"
    )"
    local changed=""
    local library_changed=0
    local update_requested=0
    local changed_files=()
    local all_files_list="${tmp_dir}/.all-files"
    local changed_files_list="${tmp_dir}/.changed-files"
    for f in "${files[@]}"; do
        if [ "${f}" = "update.sh" ]; then
            update_requested=1
            break
        fi
    done
    for f in "${files[@]}"; do
        local expected_mode=""
        local installed_mode=""
        expected_mode="$(lumen_self_update_file_mode "${f}")"
        installed_mode="$(
            lumen_self_update_path_mode "${scripts_dir}/${f}" 2>/dev/null || true
        )"
        if [ -f "${scripts_dir}/${f}" ] \
                && [ ! -L "${scripts_dir}/${f}" ] \
                && [ "${installed_mode}" = "${expected_mode}" ] \
                && cmp -s "${tmp_dir}/${f}" "${scripts_dir}/${f}"; then
            continue
        fi
        changed_files+=("${f}")
        changed="${changed}${f} "
        case "${f}" in
            lib.sh|lib/*.sh) library_changed=1 ;;
        esac
    done

    if ! printf '%s\n' "${files[@]}" > "${all_files_list}" \
            || ! printf '%s\n' \
                ${changed_files[@]+"${changed_files[@]}"} > "${changed_files_list}"; then
        rm -rf "${tmp_dir}" 2>/dev/null || true
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi

    local transaction_rc=0
    if lumen_self_update_install_transaction \
            "${scripts_dir}" \
            "${tmp_dir}" \
            "${LUMEN_SELF_UPDATE_BACKUP_TS}" \
            "${commit_sha}" \
            "${all_files_list}" \
            "${changed_files_list}" \
            "${release_tag_marker_value}"; then
        :
    else
        transaction_rc=$?
        rm -rf "${tmp_dir}" 2>/dev/null || true
        LUMEN_SELF_UPDATE_RESULT=failed
        case "${transaction_rc}" in
            70|129|130|143)
                return "${transaction_rc}"
                ;;
        esac
        return 0
    fi

    # Preserve facade/update changed tokens used by caller re-exec contracts.
    if [ "${library_changed}" -eq 1 ]; then
        case " ${changed} " in
            *" lib.sh "*) ;;
            *) changed="${changed}lib.sh " ;;
        esac
        if [ "${update_requested}" -eq 1 ]; then
            case " ${changed} " in
                *" update.sh "*) ;;
                *) changed="${changed}update.sh " ;;
            esac
        fi
    fi

    # 每个文件保留最近 N 份备份；find 无匹配时仍成功，兼容 set -e/pipefail。
    local max_keep="${LUMEN_SELF_UPDATE_BAK_KEEP:-5}"
    if [ "${max_keep}" -gt 0 ] 2>/dev/null; then
        local prune_f prune_dir prune_name total del_n
        for prune_f in "${files[@]}"; do
            prune_dir="${scripts_dir}/$(dirname "${prune_f}")"
            prune_name="$(basename "${prune_f}")"
            total="$(find "${prune_dir}" -maxdepth 1 -name "${prune_name}.bak.*" -type f 2>/dev/null | wc -l | tr -d '[:space:]')"
            if [ -n "${total}" ] && [ "${total}" -gt "${max_keep}" ] 2>/dev/null; then
                del_n=$((total - max_keep))
                find "${prune_dir}" -maxdepth 1 -name "${prune_name}.bak.*" -type f 2>/dev/null \
                    | sort \
                    | head -n "${del_n}" \
                    | while IFS= read -r bak_path; do
                        [ -n "${bak_path}" ] && rm -f "${bak_path}" 2>/dev/null || true
                    done
            fi
        done
    fi

    rm -rf "${tmp_dir}" 2>/dev/null || true

    # shellcheck disable=SC2034  # Public results consumed by sourcing callers.
    LUMEN_SELF_UPDATE_CHANGED="${changed}"
    # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
    LUMEN_SELF_UPDATE_RESULT=ok
    if command -v log_info >/dev/null 2>&1; then
        if [ -z "${changed}" ]; then
            log_info "[self_update] 远端 ${raw_base} 与本地一致，无需替换。"
        else
            log_info "[self_update] 已从 ${raw_base} 同步：${changed}（旧版备份 *.bak.${LUMEN_SELF_UPDATE_BACKUP_TS}）。"
        fi
    fi
    return 0
}

lumen_self_update_scripts() {
    LUMEN_SELF_UPDATE_RESULT=skipped
    LUMEN_SELF_UPDATE_CHANGED=""
    LUMEN_SELF_UPDATE_BACKUP_TS=""
    LUMEN_SELF_UPDATE_SOURCE=""
    LUMEN_SELF_UPDATE_SOURCE_TAG=""
    LUMEN_SELF_UPDATE_SOURCE_COMMIT=""

    local scripts_dir="${1:-}"
    local lock_path=""
    local rc=0
    local timeout="${LUMEN_SELF_UPDATE_LOCK_TIMEOUT:-60}"
    local legacy_lock_dir="${scripts_dir}/.lumen-self-update.lock.d"
    lock_path="$(lumen_self_update_transaction_lock_path "${scripts_dir}")"
    case "${timeout}" in
        ''|*[!0-9]*) timeout=60 ;;
    esac
    if [ -e "${legacy_lock_dir}" ] || [ -L "${legacy_lock_dir}" ]; then
        log_warn "[self_update] 检测到旧版 scripts transaction lock：${legacy_lock_dir}。"
        LUMEN_SELF_UPDATE_RESULT=failed
        return 75
    fi
    if lumen_self_update_acquire_transaction_lock "${lock_path}" "${timeout}"; then
        :
    else
        rc=$?
        log_warn "[self_update] 等待 scripts transaction lock 超时：${lock_path}。"
        LUMEN_SELF_UPDATE_RESULT=failed
        return "${rc}"
    fi

    if ! lumen_self_update_recover_transactions "${scripts_dir}"; then
        log_warn "[self_update] 未完成 scripts transaction 恢复失败。"
        LUMEN_SELF_UPDATE_RESULT=failed
        lumen_self_update_release_transaction_lock || true
        return 70
    fi
    if _lumen_self_update_scripts_locked "$@"; then
        rc=0
    else
        rc=$?
    fi

    # Older internal branches report semantic failure through the public
    # result variable while returning zero. The public API must fail closed.
    if [ "${LUMEN_SELF_UPDATE_RESULT:-}" = "failed" ] \
            && [ "${rc}" -eq 0 ]; then
        rc=78
    fi
    if ! lumen_self_update_release_transaction_lock; then
        log_warn "[self_update] 无法释放 scripts transaction lock。"
        LUMEN_SELF_UPDATE_RESULT=failed
        if [ "${rc}" -eq 0 ]; then
            rc=70
        fi
    fi
    return "${rc}"
}

# Bootstrap-only branch boundary: GitHub API branch -> immutable commit.

lumen_self_update_scripts_from_github_branch() {
    # shellcheck disable=SC2034  # Public results consumed by sourcing callers.
    LUMEN_SELF_UPDATE_RESULT=skipped LUMEN_SELF_UPDATE_CHANGED=""
    # shellcheck disable=SC2034  # Public results consumed by sourcing callers.
    LUMEN_SELF_UPDATE_BACKUP_TS="" LUMEN_SELF_UPDATE_SOURCE=""
    # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
    LUMEN_SELF_UPDATE_SOURCE_COMMIT=""
    local scripts_dir="${1:-}" branch="${2:-${LUMEN_SELF_UPDATE_BRANCH:-main}}"
    local ttl_sec="${3:-${LUMEN_SELF_UPDATE_TTL:-600}}"
    local commit_sha="${LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT:-${LUMEN_SELF_UPDATE_COMMIT:-}}"
    shift "$(( $# < 3 ? $# : 3 ))"
    [ "${LUMEN_SELF_UPDATE:-1}" != "0" ] \
        || { LUMEN_SELF_UPDATE_RESULT=disabled; return 0; }
    [ -n "${scripts_dir}" ] && [ -d "${scripts_dir}" ] || return 0
    if [ -n "${commit_sha}" ] && [[ ! "${commit_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        log_warn "[self_update] LUMEN_SELF_UPDATE_COMMIT 不是有效的 40 位 commit。"
        # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
        LUMEN_SELF_UPDATE_RESULT=failed
        return 78
    fi
    commit_sha="${commit_sha:-$(lumen_resolve_github_branch_commit "${branch}")}" || {
        # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
        LUMEN_SELF_UPDATE_RESULT=failed
        return 78
    }
    LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT="${commit_sha}"
    export LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT
    log_info "[self_update] branch=${branch} 已固定到 commit=${commit_sha}。"
    lumen_self_update_scripts "${scripts_dir}" "${commit_sha}" "${ttl_sec}" "$@"
}

lumenctl_update_entry_uses_runner() {
    local update_script="$1"
    [ -f "${update_script}" ] \
        && grep -Fq 'update/runner.sh' "${update_script}" 2>/dev/null
}

lumenctl_update_script_unit_complete() {
    local scripts_dir="$1"
    local relative
    for relative in \
            lib.sh \
            backup.sh \
            lib/backup_journal.sh \
            restore.sh \
            "${_LUMEN_UPDATE_DEPENDENCY_FILES[@]}" \
            update.sh; do
        if [ ! -f "${scripts_dir}/${relative}" ] \
                || [ -L "${scripts_dir}/${relative}" ]; then
            return 1
        fi
    done
    return 0
}

lumenctl_prepare_update_script_unit() {
    local update_script="$1"
    local scripts_dir=""
    local sync_rc=0
    if ! lumenctl_update_entry_uses_runner "${update_script}"; then
        return 0
    fi
    scripts_dir="$(cd "$(dirname "${update_script}")" && pwd -P)" || return 1
    if lumenctl_update_script_unit_complete "${scripts_dir}"; then
        return 0
    fi
    if [ "${LUMEN_LUMENCTL_SELF_UPDATE:-1}" = "0" ] \
            || [ "${LUMEN_SELF_UPDATE:-1}" = "0" ]; then
        log_error "[lumenctl] update.sh 依赖单元不完整，且脚本 self-update 已关闭。"
        return 1
    fi
    log_warn "[lumenctl] update.sh 依赖单元不完整，强制同步完整 updater 脚本单元。"
    LUMEN_SELF_UPDATE_FORCE=1 \
        lumenctl_sync_script_unit 0 "${scripts_dir}" || sync_rc=$?
    if [ "${sync_rc}" -ne 0 ] \
            || [ "${LUMEN_SELF_UPDATE_RESULT:-}" != "ok" ] \
            || ! lumenctl_update_script_unit_complete "${scripts_dir}"; then
        log_error "[lumenctl] updater 脚本单元修复失败，拒绝执行不完整的 update.sh。"
        return 1
    fi
    log_info "[lumenctl] updater 脚本单元已修复，继续执行 update.sh。"
    return 0
}
