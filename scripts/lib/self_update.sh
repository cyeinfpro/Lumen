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
                '#!'*python3*) ;;
                *) return 1 ;;
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

# Install every changed script plus the update markers as one rollback unit.
# The subshell owns signal traps, so caller traps are left untouched.
lumen_self_update_install_transaction() (
    scripts_dir="$1"
    download_dir="$2"
    backup_ts="$3"
    commit_sha="$4"
    all_files_list="$5"
    changed_files_list="$6"
    transaction_dir=""
    committed=0
    replacement_started=0
    rollback_failed=0
    f=""
    target=""
    target_dir=""
    backup=""
    stage=""
    mode=""
    index=0
    marker=""
    marker_stage=""
    marker_backup=""
    changed_files=()
    targets=()
    target_states=()
    target_backups=()
    target_stages=()
    marker_paths=()
    marker_states=()
    marker_backups=()
    marker_stages=()
    visible_backups=()

    while IFS= read -r f; do
        [ -n "${f}" ] && changed_files+=("${f}")
    done < "${changed_files_list}"

    umask 077
    transaction_dir="$(
        mktemp -d "${scripts_dir}/.lumen-self-update.txn.XXXXXXXXXX" 2>/dev/null
    )" || exit 1
    if ! chmod 0700 "${transaction_dir}"; then
        rm -rf "${transaction_dir}" 2>/dev/null || true
        exit 1
    fi
    export LUMEN_SELF_UPDATE_TRANSACTION_PID="${BASHPID:-$$}"

    # shellcheck disable=SC2329  # Invoked from rollback and EXIT traps.
    _lumen_self_update_restore_path() {
        local restore_path="$1"
        local restore_state="$2"
        local restore_backup="$3"
        local restore_index="$4"
        local restore_stage="${transaction_dir}/restore.${restore_index}"
        case "${restore_state}" in
            present)
                if ! cp -a "${restore_backup}" "${restore_stage}" \
                        || ! mv -f "${restore_stage}" "${restore_path}"; then
                    rm -f "${restore_stage}" 2>/dev/null || true
                    return 1
                fi
                ;;
            absent)
                rm -f "${restore_path}" || return 1
                ;;
            *)
                return 1
                ;;
        esac
    }

    # shellcheck disable=SC2329  # Invoked from the EXIT trap.
    _lumen_self_update_rollback() {
        local i
        rollback_failed=0
        for ((i = ${#marker_paths[@]} - 1; i >= 0; i--)); do
            if ! _lumen_self_update_restore_path \
                    "${marker_paths[$i]}" \
                    "${marker_states[$i]}" \
                    "${marker_backups[$i]}" \
                    "marker.${i}"; then
                rollback_failed=1
                log_warn "[self_update] marker 回滚失败：${marker_paths[$i]}"
            fi
        done
        for ((i = ${#targets[@]} - 1; i >= 0; i--)); do
            if ! _lumen_self_update_restore_path \
                    "${targets[$i]}" \
                    "${target_states[$i]}" \
                    "${target_backups[$i]}" \
                    "target.${i}"; then
                rollback_failed=1
                log_warn "[self_update] 脚本回滚失败：${targets[$i]}"
            fi
        done
        return "${rollback_failed}"
    }

    # shellcheck disable=SC2329  # Installed as the EXIT trap below.
    _lumen_self_update_finish() {
        local rc=$?
        local created_backup
        trap - EXIT INT TERM HUP
        if [ "${committed}" -ne 1 ]; then
            if [ "${replacement_started}" -eq 1 ]; then
                log_warn "[self_update] 安装事务失败，正在恢复全部 scripts 文件。"
                if ! _lumen_self_update_rollback; then
                    log_warn "[self_update] scripts 事务回滚不完整，拒绝继续运行。"
                    rc=70
                fi
            else
                for created_backup in \
                        ${visible_backups[@]+"${visible_backups[@]}"}; do
                    rm -f "${created_backup}" 2>/dev/null || true
                done
            fi
        fi
        rm -rf "${transaction_dir}" 2>/dev/null || true
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
        target_dir="$(dirname "${target}")"
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
            backup="${target}.bak.${backup_ts}"
            if [ -e "${backup}" ] || [ -L "${backup}" ]; then
                log_warn "[self_update] 备份路径已存在，拒绝覆盖：${backup}。"
                exit 1
            fi
            if ! cp -a "${target}" "${backup}" \
                    || ! cmp -s "${target}" "${backup}"; then
                log_warn "[self_update] 备份失败，未开始替换：${target}。"
                exit 1
            fi
            target_states+=("present")
            target_backups+=("${backup}")
            visible_backups+=("${backup}")
        else
            target_states+=("absent")
            target_backups+=("")
        fi

        stage="${transaction_dir}/staged/$(printf '%04d' "${index}")"
        mode="$(lumen_self_update_file_mode "${f}")"
        if ! cp -a "${download_dir}/${f}" "${stage}" \
                || ! chmod "${mode}" "${stage}" \
                || ! lumen_validate_self_update_file "${f}" "${stage}"; then
            log_warn "[self_update] staging/权限校验失败：${f}。"
            exit 1
        fi
        targets+=("${target}")
        target_stages+=("${stage}")
    done

    marker_paths=(
        "${scripts_dir}/.lumen-self-update.files"
        "${scripts_dir}/.lumen-self-update.source"
        "${scripts_dir}/.lumen-self-update.last"
    )
    index=0
    for marker in "${marker_paths[@]}"; do
        index=$((index + 1))
        if [ -L "${marker}" ] || { [ -e "${marker}" ] && [ ! -f "${marker}" ]; }; then
            log_warn "[self_update] marker 不是普通文件：${marker}。"
            exit 1
        fi
        marker_backup="${transaction_dir}/markers/original.${index}"
        if [ -f "${marker}" ]; then
            if ! cp -a "${marker}" "${marker_backup}"; then
                log_warn "[self_update] marker 备份失败：${marker}。"
                exit 1
            fi
            marker_states+=("present")
            marker_backups+=("${marker_backup}")
        else
            marker_states+=("absent")
            marker_backups+=("")
        fi
        marker_stage="${transaction_dir}/markers/staged.${index}"
        case "${index}" in
            1) sort -u "${all_files_list}" > "${marker_stage}" || exit 1 ;;
            2) printf '%s\n' "${commit_sha}" > "${marker_stage}" || exit 1 ;;
            3) date -u +%s > "${marker_stage}" || exit 1 ;;
        esac
        if ! chmod 0600 "${marker_stage}"; then
            log_warn "[self_update] marker 权限设置失败：${marker}。"
            exit 1
        fi
        marker_stages+=("${marker_stage}")
    done

    replacement_started=1
    for ((index = 0; index < ${#targets[@]}; index++)); do
        if ! mv -f "${target_stages[$index]}" "${targets[$index]}"; then
            log_warn "[self_update] 替换失败：${targets[$index]}。"
            exit 1
        fi
    done
    for ((index = 0; index < ${#marker_paths[@]}; index++)); do
        if ! mv -f "${marker_stages[$index]}" "${marker_paths[$index]}"; then
            log_warn "[self_update] marker 提交失败：${marker_paths[$index]}。"
            exit 1
        fi
    done

    committed=1
    exit 0
)

lumen_self_update_scripts() {
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
    )
    local python_helper_files=(release_manifest_guard.py update_runner.py restore_runner.py)
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
            release_manifest_guard.py
            update_runner.py
            restore_runner.py
            backup.sh
            restore.sh
            update.sh
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
                lib.sh|update.sh|lumenctl.sh)
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

    local release_tag="" commit_sha="${LUMEN_SELF_UPDATE_COMMIT:-}"
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

    local marker="${scripts_dir}/.lumen-self-update.last"
    local coverage_marker="${scripts_dir}/.lumen-self-update.files"
    local source_marker="${scripts_dir}/.lumen-self-update.source"
    local coverage_complete=1
    if [ ! -f "${coverage_marker}" ]; then
        coverage_complete=0
    else
        for requested in "${files[@]}"; do
            if ! grep -Fxq "${requested}" "${coverage_marker}" 2>/dev/null; then
                coverage_complete=0
                break
            fi
        done
    fi
    if [ ! -f "${source_marker}" ] \
            || [ "$(cat "${source_marker}" 2>/dev/null || true)" != "${commit_sha}" ]; then
        coverage_complete=0
    fi
    if [ "${LUMEN_SELF_UPDATE_FORCE:-0}" != "1" ] \
            && [ "${coverage_complete}" -eq 1 ] \
            && [ -f "${marker}" ]; then
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

    LUMEN_SELF_UPDATE_BACKUP_TS="$(date -u +%Y%m%d-%H%M%S)"
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
        if [ -f "${scripts_dir}/${f}" ] \
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
            "${changed_files_list}"; then
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

# Bootstrap-only branch boundary: GitHub API branch -> immutable commit.

lumen_self_update_scripts_from_github_branch() {
    # shellcheck disable=SC2034  # Public results consumed by sourcing callers.
    LUMEN_SELF_UPDATE_RESULT=skipped LUMEN_SELF_UPDATE_CHANGED=""
    # shellcheck disable=SC2034  # Public results consumed by sourcing callers.
    LUMEN_SELF_UPDATE_BACKUP_TS="" LUMEN_SELF_UPDATE_SOURCE=""
    # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
    LUMEN_SELF_UPDATE_SOURCE_COMMIT=""
    local scripts_dir="${1:-}" branch="${2:-${LUMEN_SELF_UPDATE_BRANCH:-main}}"
    local ttl_sec="${3:-${LUMEN_SELF_UPDATE_TTL:-600}}" commit_sha="${LUMEN_SELF_UPDATE_COMMIT:-}"
    shift "$(( $# < 3 ? $# : 3 ))"
    [ "${LUMEN_SELF_UPDATE:-1}" != "0" ] \
        || { LUMEN_SELF_UPDATE_RESULT=disabled; return 0; }
    [ -n "${scripts_dir}" ] && [ -d "${scripts_dir}" ] || return 0
    if [ -n "${commit_sha}" ] && [[ ! "${commit_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        log_warn "[self_update] LUMEN_SELF_UPDATE_COMMIT 不是有效的 40 位 commit。"
        # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
    commit_sha="${commit_sha:-$(lumen_resolve_github_branch_commit "${branch}")}" || {
        # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    }
    log_info "[self_update] branch=${branch} 已固定到 commit=${commit_sha}。"
    lumen_self_update_scripts "${scripts_dir}" "${commit_sha}" "${ttl_sec}" "$@"
}
