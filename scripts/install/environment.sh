#!/usr/bin/env bash
# Install environment validation and secret management.
# Sourced by scripts/install.sh after raw bootstrap has completed.

# ---------------------------------------------------------------------------
# .env 写入辅助（保留旧行为：拒绝控制字符 / 单引号）
# ---------------------------------------------------------------------------
contains_control_chars() {
    local value="$1"
    printf '%s' "${value}" | LC_ALL=C grep -q '[[:cntrl:]]'
}

validate_dotenv_value() {
    local name="$1"
    local value="$2"
    if contains_control_chars "${value}"; then
        log_error "${name} 不能包含换行、制表符或其他控制字符。"
        return 1
    fi
    if [[ "${value}" == *"'"* ]]; then
        log_error "${name} 不能包含单引号，以免破坏 .env 引号边界。"
        return 1
    fi
    return 0
}

validate_redis_password() {
    local value="$1"
    validate_dotenv_value "REDIS_PASSWORD" "${value}" || return 1
    if [[ ! "${value}" =~ ^[A-Za-z0-9._~-]+$ ]]; then
        log_error "REDIS_PASSWORD 只能包含 URL 安全字符：A-Z a-z 0-9 . _ ~ -。"
        log_error "请避免 @、:、/、?、#、%、空格等会破坏 REDIS_URL 的字符。"
        return 1
    fi
    return 0
}

# 在 .env 文件里精确替换 KEY=value 行（避免全局 sed 误伤 §21.1）。
# 用法：env_file_set <file> <key> <value>
# 注意：value 不允许包含换行 / 单引号；用 dotenv_quote 校验。
# 在目标文件同 fs 下 mktemp，确保 mv 是 POSIX 原子 rename。默认 mktemp 在
# /tmp，与 /opt/lumen/shared/ 跨 fs 时退化为 copy+unlink，断电瞬间存在空文件窗口。
env_file_set() {
    local file="$1"
    local key="$2"
    local value="$3"
    lumen_regular_file_path_safe "${file}" || return 1
    validate_dotenv_value "${key}" "${value}" || return 1
    local tmp dir
    dir="$(dirname "${file}")"
    tmp="$(mktemp "${dir}/.lumen-env.XXXXXX" 2>/dev/null)" || tmp="$(mktemp)" || return 1
    # awk 行级精确替换：只动 ^KEY= 开头的行；其它原样保留。
    awk -v k="${key}" -v v="${value}" '
        BEGIN { replaced=0 }
        {
            if ($0 ~ "^" k "=") {
                printf "%s=%s\n", k, v
                replaced=1
            } else {
                print
            }
        }
        END {
            if (!replaced) {
                printf "%s=%s\n", k, v
            }
        }
    ' "${file}" > "${tmp}" && mv "${tmp}" "${file}"
}

# 读取 .env 中某 key 的当前值（沿用 lib.sh 实现）
env_file_get() {
    lumen_read_dotenv_value "$1" "$2"
}

# 检查 .env 是否存在指定 key 且非空，不输出 value。
env_key_present() {
    local file="$1"
    local key="$2"
    [ -f "${file}" ] || return 1
    grep -qE "^${key}=.+" "${file}"
}

lumen_env_truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

generate_hex_secret() {
    local bytes="${1:-32}"
    openssl rand -hex "${bytes}"
}

postgres_data_initialized() {
    local db_root="${LUMEN_DB_ROOT:-/opt/lumendata}"
    local postgres_dir="${db_root}/postgres"

    if [ -f "${postgres_dir}/PG_VERSION" ] || [ -f "${postgres_dir}/global/pg_control" ]; then
        return 0
    fi
    if command -v lumen_run_as_root >/dev/null 2>&1; then
        if lumen_run_as_root test -f "${postgres_dir}/PG_VERSION" 2>/dev/null \
                || lumen_run_as_root test -f "${postgres_dir}/global/pg_control" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

ensure_env_secret() {
    local file="$1"
    local key="$2"
    local bytes="${3:-32}"
    local value
    value="$(env_file_get "${key}" "${file}")"
    if [ -n "${value}" ]; then
        case "${key}" in
            AGENT_RUNTIME_SHARED_SECRET|AGENT_TOOL_CAPABILITY_SECRET)
                if [ "${#value}" -lt 32 ]; then
                    log_error "${key} 已配置但短于 32 字符；拒绝静默轮转。"
                    return 1
                fi
                ;;
        esac
        return 0
    fi
    if [ "${key}" = "BYOK_API_KEY_MASTER_SECRET" ] && [ "${LUMEN_ALLOW_BYOK_KEY_GEN:-0}" != "1" ]; then
        if postgres_data_initialized; then
            log_error "BYOK_API_KEY_MASTER_SECRET 缺失，且数据库可能已有 BYOK 密文。"
            log_error "  - 新部署：export LUMEN_ALLOW_BYOK_KEY_GEN=1 再重跑安装。"
            log_error "  - 升级：从备份恢复原始 BYOK_API_KEY_MASTER_SECRET，不要让脚本随机生成。"
            return 1
        fi
        log_warn "BYOK_API_KEY_MASTER_SECRET 缺失，但 Postgres 尚未初始化；按新部署/失败重跑自动生成。"
    fi
    value="$(generate_hex_secret "${bytes}")"
    if [ "${key}" = "REDIS_PASSWORD" ]; then
        validate_redis_password "${value}" || return 1
    else
        validate_dotenv_value "${key}" "${value}" || return 1
    fi
    env_file_set "${file}" "${key}" "${value}" || return 1
    return 2
}

ensure_required_env_secrets() {
    local file="$1"
    local generated=()
    local db_url redis_url redis_from_url

    db_url="$(env_file_get DATABASE_URL "${file}")"
    if [ -z "$(env_file_get DB_PASSWORD "${file}")" ] && [ -n "${db_url}" ]; then
        lumen_ensure_compose_db_env_vars "${file}" || return 1
    fi

    redis_url="$(env_file_get REDIS_URL "${file}")"
    if [ -z "$(env_file_get REDIS_PASSWORD "${file}")" ] && [ -n "${redis_url}" ]; then
        redis_from_url="$(lumen_redis_password_from_url "${redis_url}" 2>/dev/null || true)"
        if [ -n "${redis_from_url}" ]; then
            validate_redis_password "${redis_from_url}" || return 1
            env_file_set "${file}" REDIS_PASSWORD "${redis_from_url}" || return 1
        fi
    fi

    ensure_env_secret "${file}" DB_PASSWORD 32 || case "$?" in
        2) generated+=("DB_PASSWORD") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" REDIS_PASSWORD 32 || case "$?" in
        2) generated+=("REDIS_PASSWORD") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" SESSION_SECRET 64 || case "$?" in
        2) generated+=("SESSION_SECRET") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" IMAGE_PROXY_SECRET 32 || case "$?" in
        2) generated+=("IMAGE_PROXY_SECRET") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" BYOK_API_KEY_MASTER_SECRET 48 || case "$?" in
        2) generated+=("BYOK_API_KEY_MASTER_SECRET") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" TELEGRAM_BOT_SHARED_SECRET 32 || case "$?" in
        2) generated+=("TELEGRAM_BOT_SHARED_SECRET") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" AGENT_RUNTIME_SHARED_SECRET 32 || case "$?" in
        2) generated+=("AGENT_RUNTIME_SHARED_SECRET") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" AGENT_TOOL_CAPABILITY_SECRET 32 || case "$?" in
        2) generated+=("AGENT_TOOL_CAPABILITY_SECRET") ;;
        *) return 1 ;;
    esac
    if [ "$(env_file_get AGENT_RUNTIME_SHARED_SECRET "${file}")" = \
            "$(env_file_get AGENT_TOOL_CAPABILITY_SECRET "${file}")" ]; then
        log_error "Agent Runtime 与工具 capability 必须使用不同密钥。"
        return 1
    fi

    local db_user db_name db_password redis_password
    db_user="$(env_file_get DB_USER "${file}")"
    db_name="$(env_file_get DB_NAME "${file}")"
    db_password="$(env_file_get DB_PASSWORD "${file}")"
    redis_password="$(env_file_get REDIS_PASSWORD "${file}")"
    db_user="${db_user:-lumen_app}"
    db_name="${db_name:-lumen_app}"
    if [ -z "$(env_file_get DATABASE_URL "${file}")" ]; then
        env_file_set "${file}" DATABASE_URL \
            "postgresql+asyncpg://${db_user}:${db_password}@postgres:5432/${db_name}" || return 1
    fi
    if [ -z "$(env_file_get REDIS_URL "${file}")" ]; then
        env_file_set "${file}" REDIS_URL \
            "redis://:${redis_password}@redis:6379/0" || return 1
    fi

    if [ "${#generated[@]}" -gt 0 ]; then
        log_info "已补齐随机密钥：${generated[*]}。"
    fi
}
