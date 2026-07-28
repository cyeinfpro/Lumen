#!/usr/bin/env bash
# Lock phase initialization and release-bound self-update phase.

# Phase: lock
update_phase_lock() {
emit_start lock
emit_info lock operation_id "${OPERATION_ID}"

# CURRENT_RELEASE 提前到 self_update_scripts 前解析（check phase 内仍会重赋值，幂等）；
# 不放进 check phase 是为了 self_update_scripts 在 noop 判断之前就能拿到 release scripts 目录。
CURRENT_RELEASE=""
CURRENT_ID=""
if [ -L "${ROOT}/current" ]; then
    CURRENT_RELEASE="$(lumen_release_current_path "${ROOT}" || true)"
    [ -n "${CURRENT_RELEASE}" ] && CURRENT_ID="$(basename "${CURRENT_RELEASE}")"
fi
emit_done  lock 0
}

# Phase: self_update_scripts
update_phase_self_update_scripts() {
emit_start self_update_scripts

if [ "${LUMEN_UPDATE_SELF_UPDATE_SCRIPTS:-1}" = "0" ]; then
    log_info "[self_update_scripts] 关闭（LUMEN_UPDATE_SELF_UPDATE_SCRIPTS=0）。"
    emit_done self_update_scripts 0
elif [ -z "${CURRENT_RELEASE}" ] || [ ! -d "${CURRENT_RELEASE}/scripts" ]; then
    log_info "[self_update_scripts] 不是 release 布局（CURRENT_RELEASE 为空），跳过。"
    emit_done self_update_scripts 0
else
    SELF_UPDATE_REF="${LUMEN_UPDATE_SCRIPTS_REF:-}"
    if [ -z "${SELF_UPDATE_REF}" ] && [ -f "${CURRENT_RELEASE}/.image-tag" ]; then
        SELF_UPDATE_REF="$(head -n1 "${CURRENT_RELEASE}/.image-tag" 2>/dev/null | tr -d '[:space:]')"
    fi
    if ! printf '%s\n' "${SELF_UPDATE_REF}" \
            | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'; then
        log_info "[self_update_scripts] current release 没有具体 release tag，拒绝从 branch 自更新。"
        emit_info self_update_scripts source_ref "${SELF_UPDATE_REF:-<none>}"
        emit_done self_update_scripts 0
    else
    lumen_self_update_scripts \
        "${CURRENT_RELEASE}/scripts" \
        "${SELF_UPDATE_REF}" \
        60 \
        lib.sh \
        release_manifest_guard.py update_runner.py restore_runner.py \
        backup.sh restore.sh update.sh \
        update/runner.sh update/phases.sh update/bootstrap.sh \
        update/common.sh update/phase_contract.sh update/journal.sh \
        update/journal_store.py \
        update/release/manifest.sh update/release/runner_units.sh \
        update/release/source_helpers.sh update/release/self_update.sh \
        update/release/check.sh update/release/fetch.sh \
        update/release/digest.sh update/release/image_proof_store.py \
        update/release/activate.sh \
        update/backup/restore_points.sh \
        update/backup/migration_helpers.sh update/backup/preflight.sh \
        update/backup/phases.sh update/services/compose.sh \
        update/services/switch.sh update/services/restart.sh \
        update/services/health.sh update/recovery/cleanup.sh \
        update/recovery/state.sh update/recovery/blue_green.sh
    case "${LUMEN_SELF_UPDATE_RESULT:-}" in
        ok)
            if [ -n "${LUMEN_SELF_UPDATE_CHANGED:-}" ]; then
                emit_info self_update_scripts source "${LUMEN_SELF_UPDATE_SOURCE}"
                emit_info self_update_scripts commit "${LUMEN_SELF_UPDATE_SOURCE_COMMIT}"
                emit_info self_update_scripts changed "${LUMEN_SELF_UPDATE_CHANGED}"
                emit_info self_update_scripts backup_suffix ".bak.${LUMEN_SELF_UPDATE_BACKUP_TS}"
                # update.sh 自己变化 → re-exec 新版
                case " ${LUMEN_SELF_UPDATE_CHANGED} " in
                    *" update.sh "*|*" update/"*|*" lib.sh "*)
                        local self_update_hops="${LUMEN_UPDATE_SELF_UPDATED:-0}"
                        case "${self_update_hops}" in
                            ''|*[!0-9]*) self_update_hops=0 ;;
                        esac
                        if [ "${self_update_hops}" -ge 2 ]; then
                            log_warn "[self_update_scripts] update.sh 连续变化超过两跳，拒绝继续 re-exec。"
                        else
                            log_info "[self_update_scripts] update.sh 已变更，re-exec 新版（保留 OPERATION_ID）。"
                            emit_done self_update_scripts 0
                            export LUMEN_UPDATE_SELF_UPDATED=$((self_update_hops + 1))
                            export LUMEN_UPDATE_RESUME=1
                            export OPERATION_ID
                            exec bash "${CURRENT_RELEASE}/scripts/update.sh" "$@"
                        fi
                        ;;
                esac
            fi
            emit_done self_update_scripts 0
            ;;
        failed)
            emit_warn self_update_scripts "fetch_or_validate_failed_continue_with_local"
            emit_done self_update_scripts 0
            ;;
        disabled|skipped|*)
            emit_done self_update_scripts 0
            ;;
    esac
    fi
fi
}
