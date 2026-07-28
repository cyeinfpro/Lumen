#!/usr/bin/env bash
# Atomic release switch phase.

# Phase: switch
update_phase_switch() {
emit_start switch

if ! lumen_release_atomic_switch "${ROOT}" "${NEW_ID}"; then
    log_error "[switch] symlink 切换失败。"
    emit_fail switch 1
    exit 1
fi
if [ -f "${ROOT}/current/VERSION" ]; then
    ln -sfn current/VERSION "${ROOT}/VERSION" 2>/dev/null || cp "${ROOT}/current/VERSION" "${ROOT}/VERSION"
fi
if ! lumen_update_fsync_directory "${ROOT}"; then
    log_error "[switch] symlink 目录 fsync 失败。"
    emit_fail switch 1
    exit 1
fi
UPDATE_RELEASE_SWITCHED=1
emit_info switch from "${CURRENT_ID:-<none>}"
emit_info switch to   "${NEW_ID}"
if ! refresh_update_runner_units; then
    log_error "[switch] 刷新 update runner units 失败。"
    emit_fail switch 1
    exit 1
fi
emit_done switch 0
}
