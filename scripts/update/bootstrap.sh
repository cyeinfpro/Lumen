#!/usr/bin/env bash
# Updater environment and shared state initialization.

SCRIPT_ROOT="$(lumen_resolve_repo_root "${SCRIPT_DIR}")"
ROOT_SOURCE="script"
_LUMEN_UPDATE_ROOT_CANDIDATE="${_LUMEN_UPDATE_INPUT_DEPLOY_ROOT:-}"
if [ -z "${_LUMEN_UPDATE_ROOT_CANDIDATE}" ] \
        && [ -z "${_LUMEN_UPDATE_INPUT_UPDATE_ROOT:-}" ] \
        && [ "${SCRIPT_ROOT}" != "${LUMEN_DEPLOY_ROOT}" ] \
        && [ ! -f "${SCRIPT_ROOT}/shared/.env" ] \
        && [ ! -L "${SCRIPT_ROOT}/current" ] \
        && [ -f "${LUMEN_DEPLOY_ROOT}/shared/.env" ]; then
    _LUMEN_UPDATE_ROOT_CANDIDATE="${LUMEN_DEPLOY_ROOT}"
    ROOT_SOURCE="deploy_root"
fi
if ! ROOT="$(
        lumen_resolve_deploy_root \
            "${SCRIPT_DIR}" \
            "${_LUMEN_UPDATE_ROOT_CANDIDATE}" \
            "${_LUMEN_UPDATE_INPUT_UPDATE_ROOT:-}"
)"; then
    log_error "拒绝不安全或有歧义的更新部署根目录。"
    exit 78
fi
if [ "${ROOT}" != "${SCRIPT_ROOT}" ]; then
    ROOT_SOURCE="deploy_root"
fi
LUMEN_DEPLOY_ROOT="${ROOT}"
LUMEN_UPDATE_ROOT="${ROOT}"
export LUMEN_DEPLOY_ROOT LUMEN_UPDATE_ROOT
unset _LUMEN_UPDATE_ROOT_CANDIDATE
if [ "${ROOT_SOURCE}" = "deploy_root" ] \
        && [ -z "${LUMEN_REPO_DIR:-}" ] \
        && [ -f "${SCRIPT_ROOT}/docker-compose.yml" ]; then
    LUMEN_REPO_DIR="${SCRIPT_ROOT}"
    export LUMEN_REPO_DIR
fi
SHARED_DIR="${ROOT}/shared"
SHARED_ENV="${SHARED_DIR}/.env"
if ! lumen_release_shared_env_path_safe "${ROOT}"; then
    log_error "拒绝从不安全的 shared/.env 启动更新。"
    exit 78
fi
if ! lumen_require_systemd_flock; then
    log_error "Linux systemd 更新路径需要 flock；请安装 util-linux 后重试。"
    exit 78
fi
shared_data_root=""
shared_db_root=""
shared_backup_root=""
shared_postgres_uid=""
shared_postgres_gid=""
shared_redis_uid=""
shared_redis_gid=""
shared_app_uid=""
shared_app_gid=""
shared_app_storage_gid=""
if [ -f "${SHARED_ENV}" ]; then
    shared_data_root="$(lumen_env_value LUMEN_DATA_ROOT "${SHARED_ENV}" 2>/dev/null || true)"
    shared_db_root="$(lumen_env_value LUMEN_DB_ROOT "${SHARED_ENV}" 2>/dev/null || true)"
    shared_backup_root="$(lumen_env_value LUMEN_BACKUP_ROOT "${SHARED_ENV}" 2>/dev/null || true)"
    shared_backup_root="${shared_backup_root:-$(lumen_env_value BACKUP_ROOT "${SHARED_ENV}" 2>/dev/null || true)}"
    shared_postgres_uid="$(lumen_env_value LUMEN_POSTGRES_UID "${SHARED_ENV}" 2>/dev/null || true)"
    shared_postgres_gid="$(lumen_env_value LUMEN_POSTGRES_GID "${SHARED_ENV}" 2>/dev/null || true)"
    shared_redis_uid="$(lumen_env_value LUMEN_REDIS_UID "${SHARED_ENV}" 2>/dev/null || true)"
    shared_redis_gid="$(lumen_env_value LUMEN_REDIS_GID "${SHARED_ENV}" 2>/dev/null || true)"
    shared_app_uid="$(lumen_env_value LUMEN_APP_UID "${SHARED_ENV}" 2>/dev/null || true)"
    shared_app_gid="$(lumen_env_value LUMEN_APP_GID "${SHARED_ENV}" 2>/dev/null || true)"
    shared_app_storage_gid="$(lumen_env_value LUMEN_APP_STORAGE_GID "${SHARED_ENV}" 2>/dev/null || true)"
fi
LUMEN_DATA_ROOT="${_LUMEN_UPDATE_INPUT_DATA_ROOT:-${shared_data_root:-/opt/lumendata}}"
LUMEN_DB_ROOT="${_LUMEN_UPDATE_INPUT_DB_ROOT:-${shared_db_root:-${LUMEN_DATA_ROOT}}}"
LUMEN_BACKUP_ROOT="${_LUMEN_UPDATE_INPUT_BACKUP_ROOT:-${shared_backup_root:-${LUMEN_DATA_ROOT}/backup}}"
LUMEN_POSTGRES_UID="${_LUMEN_UPDATE_INPUT_POSTGRES_UID:-${shared_postgres_uid:-999}}"
LUMEN_POSTGRES_GID="${_LUMEN_UPDATE_INPUT_POSTGRES_GID:-${shared_postgres_gid:-999}}"
LUMEN_REDIS_UID="${_LUMEN_UPDATE_INPUT_REDIS_UID:-${shared_redis_uid:-999}}"
LUMEN_REDIS_GID="${_LUMEN_UPDATE_INPUT_REDIS_GID:-${shared_redis_gid:-999}}"
LUMEN_APP_UID="${_LUMEN_UPDATE_INPUT_APP_UID:-${shared_app_uid:-10001}}"
LUMEN_APP_GID="${_LUMEN_UPDATE_INPUT_APP_GID:-${shared_app_gid:-10001}}"
LUMEN_APP_STORAGE_GID="${_LUMEN_UPDATE_INPUT_APP_STORAGE_GID:-${shared_app_storage_gid:-${LUMEN_APP_GID}}}"
export LUMEN_DATA_ROOT LUMEN_DB_ROOT LUMEN_BACKUP_ROOT LUMEN_POSTGRES_UID LUMEN_POSTGRES_GID LUMEN_REDIS_UID LUMEN_REDIS_GID LUMEN_APP_UID LUMEN_APP_GID LUMEN_APP_STORAGE_GID
UPDATE_LOG_DIR="${LUMEN_BACKUP_ROOT}"
OPERATION_ID="update-$(date -u +%Y%m%d-%H%M%S)-$$"

NEW_ID=""
NEW_RELEASE=""
PREVIOUS_TAG=""
TARGET_TAG=""
RELEASE_SOURCE_COMMIT=""
RELEASE_SOURCE_COMMIT_PROOF=""
RELEASE_EXPECTED_COMMIT=""
RELEASE_SOURCE_API_IMAGE=""
RELEASE_SOURCE_MANIFEST_CACHE=""
ROLLBACK_DONE=0
UPDATE_STATE_COMMITTED=0
UPDATE_STATE_COMMIT_UNKNOWN=0
UPDATE_STATE_SNAPSHOT_READY=0
UPDATE_SNAPSHOT_LINKS_KNOWN=0
UPDATE_SNAPSHOT_ENV_SHA256=""
UPDATE_RELEASE_SWITCHED=0
UPDATE_OLD_SERVICES_STOPPED=0
UPDATE_ENV_SNAPSHOT=""
UPDATE_ORIGINAL_CURRENT_PRESENT=0
UPDATE_ORIGINAL_CURRENT_TARGET=""
UPDATE_ORIGINAL_PREVIOUS_PRESENT=0
UPDATE_ORIGINAL_PREVIOUS_TARGET=""
UPDATE_HOST_ARTIFACT_SNAPSHOT=""
UPDATE_RESTORE_POINT_TIMESTAMP=""
UPDATE_RESTORE_POINT_PG=""
UPDATE_RESTORE_POINT_REDIS=""
UPDATE_RESTORE_POINT_PG_SIZE=""
UPDATE_RESTORE_POINT_REDIS_SIZE=""
UPDATE_RESTORE_POINT_PG_SHA256=""
UPDATE_RESTORE_POINT_REDIS_SHA256=""
UPDATE_MIGRATION_STARTED=0
UPDATE_MIGRATION_VERIFIED=0
UPDATE_MIGRATION_HEAD=""
UPDATE_RESTORE_BOUNDARY_LOGGED=0
LUMEN_UPDATE_MODE="$(printf '%s' "${LUMEN_UPDATE_MODE:-fast}" | tr '[:upper:]' '[:lower:]')"
case "${LUMEN_UPDATE_MODE}" in
    fast)
        ;;
    standard|safe|full)
        LUMEN_UPDATE_MODE="standard"
        ;;
    *)
        log_warn "未知 LUMEN_UPDATE_MODE=${LUMEN_UPDATE_MODE}，回退到 fast。"
        LUMEN_UPDATE_MODE="fast"
        ;;
esac
if [ "${LUMEN_UPDATE_MODE}" = "fast" ] && [ -z "${LUMEN_UPDATE_SELF_UPDATE_SCRIPTS+x}" ]; then
    # stable 首跳必须先补齐 release manifest guard 与 host runners，否则旧
    # release 会用旧 helper 校验新发布物。rolling main 才默认跳过额外 raw 拉取。
    case "${LUMEN_UPDATE_CHANNEL:-stable}" in
        stable|latest|minor|major|v[0-9]*)
            LUMEN_UPDATE_SELF_UPDATE_SCRIPTS=1
            ;;
        *)
            LUMEN_UPDATE_SELF_UPDATE_SCRIPTS=0
            ;;
    esac
fi
export LUMEN_UPDATE_MODE

lumen_install_signal_handlers

log_info "项目根目录：${ROOT}"
if [ "${ROOT_SOURCE}" = "deploy_root" ]; then
    log_info "检测到已安装部署目录：${ROOT}；发布物来源：${LUMEN_REPO_DIR:-${SCRIPT_ROOT}}"
fi
log_info "operation_id：${OPERATION_ID}"
log_info "update_mode：${LUMEN_UPDATE_MODE}"
