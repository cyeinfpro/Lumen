#!/usr/bin/env bash
# Lumen 公用 bash 库：颜色日志、交互输入、命令检查、路径解析。
# 由 install.sh / update.sh / uninstall.sh source 引入。
# 不要直接执行本文件。

if [ -z "${BASH_VERSION:-}" ]; then
    echo "scripts/lib.sh requires bash. Please run scripts with bash, not sh." >&2
    exit 1
fi

# 颜色（仅在 stdout 是 tty 时输出转义码，避免日志文件被污染）
if [ -t 1 ]; then
    LUMEN_C_RESET="$(printf '\033[0m')"
    LUMEN_C_BOLD="$(printf '\033[1m')"
    LUMEN_C_RED="$(printf '\033[31m')"
    LUMEN_C_YELLOW="$(printf '\033[33m')"
    LUMEN_C_GREEN="$(printf '\033[32m')"
    LUMEN_C_BLUE="$(printf '\033[34m')"
    LUMEN_C_CYAN="$(printf '\033[36m')"
else
    LUMEN_C_RESET=""
    LUMEN_C_BOLD=""
    LUMEN_C_RED=""
    LUMEN_C_YELLOW=""
    LUMEN_C_GREEN=""
    LUMEN_C_BLUE=""
    LUMEN_C_CYAN=""
fi

log_info() {
    printf '%s[INFO]%s %s\n' "${LUMEN_C_GREEN}" "${LUMEN_C_RESET}" "$*"
}

log_warn() {
    printf '%s[WARN]%s %s\n' "${LUMEN_C_YELLOW}" "${LUMEN_C_RESET}" "$*" >&2
}

log_error() {
    printf '%s[ERROR]%s %s\n' "${LUMEN_C_RED}" "${LUMEN_C_RESET}" "$*" >&2
}

log_step() {
    printf '\n%s%s==>%s %s%s%s\n' \
        "${LUMEN_C_BOLD}" "${LUMEN_C_BLUE}" "${LUMEN_C_RESET}" \
        "${LUMEN_C_BOLD}" "$*" "${LUMEN_C_RESET}"
}

lumen_env_truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

lumen_require_python_min_version() {
    local python_bin="${1:-python3}"
    local min_major="${2:-3}"
    local min_minor="${3:-8}"
    if ! command -v "${python_bin}" >/dev/null 2>&1; then
        log_error "未找到 Python：${python_bin}（需要 >= ${min_major}.${min_minor}）。"
        return 1
    fi
    if ! "${python_bin}" - "${min_major}" "${min_minor}" <<'PY' >/dev/null 2>&1
import sys

major = int(sys.argv[1])
minor = int(sys.argv[2])
raise SystemExit(0 if sys.version_info >= (major, minor) else 1)
PY
    then
        log_error "${python_bin} 版本过低：$("${python_bin}" --version 2>&1)，需要 >= ${min_major}.${min_minor}。"
        return 1
    fi
    return 0
}

# 默认运维路径与 Compose project name（§11.4 死规则：project name 必须固定）。
# 调用方可通过环境变量覆盖；fallback 全部走 /opt/lumendata 与 /opt/lumen 约定。
# LUMEN_DB_ROOT 只承载 postgres / redis，便于把数据库放在本机盘，
# 同时让 storage / backup 继续使用 LUMEN_DATA_ROOT（例如 CIFS/NAS）。
: "${LUMEN_DATA_ROOT:=/opt/lumendata}"
: "${LUMEN_DB_ROOT:=$LUMEN_DATA_ROOT}"
: "${LUMEN_BACKUP_ROOT:=$LUMEN_DATA_ROOT/backup}"
: "${LUMEN_POSTGRES_UID:=999}"
: "${LUMEN_POSTGRES_GID:=999}"
: "${LUMEN_REDIS_UID:=999}"
: "${LUMEN_REDIS_GID:=999}"
: "${LUMEN_APP_UID:=10001}"
: "${LUMEN_APP_GID:=10001}"
: "${LUMEN_APP_STORAGE_GID:=$LUMEN_APP_GID}"
: "${LUMEN_DEPLOY_ROOT:=/opt/lumen}"
: "${LUMEN_COMPOSE_PROJECT:=lumen}"
export LUMEN_DATA_ROOT LUMEN_DB_ROOT LUMEN_BACKUP_ROOT LUMEN_POSTGRES_UID LUMEN_POSTGRES_GID LUMEN_REDIS_UID LUMEN_REDIS_GID LUMEN_APP_UID LUMEN_APP_GID LUMEN_APP_STORAGE_GID LUMEN_DEPLOY_ROOT LUMEN_COMPOSE_PROJECT

lumen_read_dotenv_value() {
    local key="$1"
    local file="$2"
    local raw=""
    raw="$(sed -n "s/^${key}=//p" "${file}" 2>/dev/null | head -n1 || true)"
    raw="${raw%$'\r'}"
    if [[ "${raw}" == \'*\' && "${raw}" == *\' ]]; then
        raw="${raw:1:${#raw}-2}"
    elif [[ "${raw}" == \"*\" && "${raw}" == *\" ]]; then
        raw="${raw:1:${#raw}-2}"
    fi
    printf '%s' "${raw}"
}

lumen_ensure_compose_db_env_vars() {
    local file="$1"
    if [ ! -f "${file}" ]; then
        log_error "${file} 不存在，无法为 docker compose 读取 DB_USER/DB_PASSWORD/DB_NAME。"
        return 1
    fi
    if grep -qE '^DB_USER=.+' "${file}" \
        && grep -qE '^DB_PASSWORD=.+' "${file}" \
        && grep -qE '^DB_NAME=.+' "${file}"; then
        return 0
    fi
    if ! grep -qE '^DATABASE_URL=.+' "${file}"; then
        log_error "${file} 缺少 DB_USER/DB_PASSWORD/DB_NAME，且无法从 DATABASE_URL 推导。"
        log_error "请补充 DB_USER、DB_PASSWORD、DB_NAME 后重跑。"
        return 1
    fi
    if ! python3 - "${file}" <<'PY'
from pathlib import Path
from urllib.parse import unquote, urlsplit
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
values = {}
for line in lines:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, raw = line.split("=", 1)
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1]
    values[key.strip()] = raw

url = values.get("DATABASE_URL", "")
parts = urlsplit(url)
db_user = unquote(parts.username or "")
db_password = unquote(parts.password or "")
db_name = unquote(parts.path.lstrip("/"))
missing = [name for name in ("DB_USER", "DB_PASSWORD", "DB_NAME") if not values.get(name)]
if missing and (not db_user or not db_password or not db_name):
    raise SystemExit(
        "DATABASE_URL must include username, password, and database name "
        "to backfill missing DB_USER/DB_PASSWORD/DB_NAME"
    )
for key, value in (("DB_USER", db_user), ("DB_PASSWORD", db_password), ("DB_NAME", db_name)):
    if any(ord(ch) < 32 for ch in value) or "'" in value:
        raise SystemExit("{} derived from DATABASE_URL contains unsupported characters".format(key))

append = []
if not values.get("DB_USER"):
    append.append("DB_USER={}".format(db_user))
if not values.get("DB_PASSWORD"):
    append.append("DB_PASSWORD='{}'".format(db_password))
if not values.get("DB_NAME"):
    append.append("DB_NAME={}".format(db_name))
if append:
    with path.open("a", encoding="utf-8") as f:
        f.write("\n# Backfilled for docker-compose variable interpolation.\n")
        for line in append:
            f.write(line + "\n")
PY
    then
        return 1
    fi
    log_warn "${file} 缺少 DB_USER/DB_PASSWORD/DB_NAME，已从 DATABASE_URL 补全供 docker compose 使用。"
}

lumen_migrate_container_urls() {
    local file="$1"
    local mode="${2:---dry-run}"
    if ! command -v python3 >/dev/null 2>&1; then
        log_error "lumen_migrate_container_urls 需要 python3 来安全解析 URL。"
        return 1
    fi
    if [ ! -f "${file}" ]; then
        log_error "${file} 不存在，无法迁移容器内 URL。"
        return 1
    fi
    if [ "${mode}" != "--dry-run" ] && [ "${mode}" != "--apply" ]; then
        log_error "lumen_migrate_container_urls: mode 必须是 --dry-run 或 --apply。"
        return 1
    fi
    python3 - "${file}" "${mode}" <<'PY'
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import difflib
import sys
import time

path = Path(sys.argv[1])
mode = sys.argv[2]
apply = mode == "--apply"
allowed = {"DATABASE_URL", "REDIS_URL", "LUMEN_BACKEND_URL", "LUMEN_API_BASE"}
local_keep_keys = {
    "PUBLIC_BASE_URL",
    "CORS_ALLOW_ORIGINS",
    "NEXT_PUBLIC_API_BASE",
    "POSTGRES_BIND_HOST",
    "REDIS_BIND_HOST",
    "API_BIND_HOST",
    "WEB_BIND_HOST",
    "WORKER_METRICS_BIND",
    "LUMEN_UPDATE_PROXY_URL",
    "LUMEN_HTTP_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}

original = path.read_text(encoding="utf-8").splitlines()
changed = []
diff_before_after: list[tuple[str, str, str]] = []

def split_assignment(line: str) -> tuple[str, str, str, str] | None:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    leading = ""
    quote = ""
    trailing = ""
    raw = value.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        quote = raw[0]
        raw = raw[1:-1]
    return key, raw, quote, leading + key

def replace_netloc(value: str, host: str, port: int) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    if parts.hostname not in {"localhost", "127.0.0.1"}:
        return value
    auth = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
    netloc = f"{auth}{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

def quote_value(value: str, quote: str) -> str:
    return f"{quote}{value}{quote}" if quote else value

def mask_url(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return "<redacted>"
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    if parts.username or parts.password:
        user = parts.username or ""
        auth = f"{user}:***@" if user else "***@"
    else:
        auth = ""
    return urlunsplit((parts.scheme, f"{auth}{host}{port}", parts.path, parts.query, parts.fragment))

def mask_value(key: str, value: str) -> str:
    if key in {"DATABASE_URL", "REDIS_URL"}:
        return mask_url(value)
    if any(token in key for token in ("PASSWORD", "SECRET", "TOKEN", "API_KEY")):
        return "<redacted>"
    return value

def mask_assignment_line(line: str) -> str:
    parsed = split_assignment(line)
    if parsed is None:
        return line
    key, value, quote, prefix = parsed
    return f"{prefix}={quote_value(mask_value(key, value), quote)}"

for line in original:
    parsed = split_assignment(line)
    if parsed is None:
        changed.append(line)
        continue
    key, value, quote, prefix = parsed
    new_value = value
    if key == "DATABASE_URL":
        new_value = replace_netloc(value, "postgres", 5432)
    elif key == "REDIS_URL":
        new_value = replace_netloc(value, "redis", 6379)
    elif key in {"LUMEN_BACKEND_URL", "LUMEN_API_BASE"} and value in {
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    }:
        new_value = "http://api:8000"
    if new_value != value:
        if key not in allowed:
            raise SystemExit(f"refusing to modify non-allowlisted key: {key}")
        diff_before_after.append((key, value, new_value))
        changed.append(f"{prefix}={quote_value(new_value, quote)}")
    else:
        changed.append(line)

residual_errors: list[str] = []
for line in changed:
    parsed = split_assignment(line)
    if parsed is None:
        continue
    key, value, _quote, _prefix = parsed
    if "localhost" not in value and "127.0.0.1" not in value:
        continue
    if key in allowed:
        raise SystemExit(f"{key} still points at localhost after migration")
    if key not in local_keep_keys:
        residual_errors.append(
            f"{key} still contains localhost/127.0.0.1; review manually or add it to the explicit keep list"
        )
if residual_errors:
    raise SystemExit("\n".join(residual_errors))

if not diff_before_after:
    print("no container URL changes needed")
    raise SystemExit(0)

for key, before, after in diff_before_after:
    print(f"{key}: {mask_value(key, before)} -> {mask_value(key, after)}")
diff = difflib.unified_diff(
    [mask_assignment_line(line) + "\n" for line in original],
    [mask_assignment_line(line) + "\n" for line in changed],
    fromfile=str(path),
    tofile=f"{path} (container-url-migrated)",
)
print("".join(diff), end="")

if apply:
    backup = path.with_name(path.name + f".bak.{time.strftime('%Y%m%d%H%M%S', time.gmtime())}")
    backup.write_text("\n".join(original) + "\n", encoding="utf-8")
    path.write_text("\n".join(changed) + "\n", encoding="utf-8")
    print(f"applied; backup={backup}")
else:
    print("dry-run only; rerun with --apply to write changes")
PY
}

lumen_release_ensure_shared_env() {
    local root="$1"
    local shared_env="${root}/shared/.env"
    local root_env="${root}/.env"
    local current_env="${root}/current/.env"

    mkdir -p "${root}/shared" 2>/dev/null || true

    if [ -f "${shared_env}" ]; then
        return 0
    fi

    if [ -f "${root_env}" ] && [ ! -L "${root_env}" ]; then
        log_warn "shared/.env 缺失，检测到 ROOT/.env；自动移入 shared/.env 并保留软链。"
        if ! mv "${root_env}" "${shared_env}"; then
            log_error "无法把 ${root_env} 移入 ${shared_env}。"
            return 1
        fi
        ln -sfn "shared/.env" "${root_env}" 2>/dev/null || true
        return 0
    fi

    if [ -f "${current_env}" ]; then
        log_warn "shared/.env 缺失，检测到 current/.env；自动复制到 shared/.env。"
        if ! cp "${current_env}" "${shared_env}"; then
            log_error "无法把 ${current_env} 复制到 ${shared_env}。"
            return 1
        fi
        if [ ! -e "${root_env}" ] || [ -L "${root_env}" ]; then
            ln -sfn "shared/.env" "${root_env}" 2>/dev/null || true
        fi
        return 0
    fi

    log_error "shared/.env 缺失，且未找到可恢复的 ROOT/.env 或 current/.env。"
    log_error "请把生产 .env 放到 ${shared_env} 后重跑 update。"
    return 1
}

# ---------------------------------------------------------------------------
# Step protocol（结构化阶段协议）
# 由 admin_update.py 通过 .update.log 解析；格式必须严格保持。
# 三种行：
#   ::lumen-step:: phase=<name> status=start ts=<ISO8601>
#   ::lumen-step:: phase=<name> status=done  rc=<int> dur_ms=<int> ts=<ISO>
#   ::lumen-info:: phase=<name> key=<k> value=<v>
# ---------------------------------------------------------------------------

# 当前正在进行中的 phase（由 lumen_step_begin 设置，lumen_step_end 清除）。
# 可以被 trap/error handler 读取以输出 status=done rc=非零。
LUMEN_CURRENT_PHASE=""
# 该 phase 的起始时间（毫秒），用于计算 dur_ms。
LUMEN_CURRENT_PHASE_START_MS=""

# 所有合法的 phase 枚举（与 update.sh 严格对齐）。
# rollback 是异常分支，不计入正常流程，但允许在 begin/end 中使用。
LUMEN_VALID_PHASES="lock check preflight backup_preflight fetch_release set_image_tag pull_images warm_pull start_infra migrate_db switch restart_services start_green shift_traffic shift_traffic_50 shift_traffic_100 drain_blue stop_blue start_blue shift_traffic_blue stop_green health_check cleanup rollback prepare fetch link_shared containers deps_python deps_node build_web health_post"

lumen_iso_now() {
    # GNU date / BSD date 都支持 -u +%FT%TZ
    date -u +%FT%TZ 2>/dev/null || date
}

# 当前时间，毫秒级（用于 dur_ms 计算）。
# 优先 GNU date %N；BSD date 没有 %N，则用 perl/python 兜底；最后退回到秒×1000。
lumen_now_ms() {
    local out
    out="$(date -u +%s%3N 2>/dev/null || true)"
    case "${out}" in
        ''|*[!0-9]*) ;;
        *N*) ;;
        *)
            # GNU date: 已经是毫秒数
            printf '%s' "${out}"
            return 0
            ;;
    esac
    if command -v perl >/dev/null 2>&1; then
        perl -MTime::HiRes=time -e 'printf "%d", time()*1000' 2>/dev/null && return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import time;print(int(time.time()*1000))' 2>/dev/null && return 0
    fi
    # 兜底：秒级精度。dur_ms 会偏差 0~999ms，但仍可读。
    printf '%s000' "$(date -u +%s 2>/dev/null || echo 0)"
}

lumen_step_phase_is_valid() {
    local phase="$1"
    case " ${LUMEN_VALID_PHASES} " in
        *" ${phase} "*) return 0 ;;
        *) return 1 ;;
    esac
}

# lumen_step_begin <phase>
# 输出 ::lumen-step:: phase=<name> status=start ts=<iso>
# 同时记录 LUMEN_CURRENT_PHASE / 起始时间，便于 lumen_step_end 计算 dur_ms。
lumen_step_begin() {
    local phase="$1"
    if [ -z "${phase}" ]; then
        log_warn "lumen_step_begin: 空 phase 参数。"
        return 0
    fi
    if ! lumen_step_phase_is_valid "${phase}"; then
        log_warn "lumen_step_begin: 未登记的 phase=${phase}（允许列表：${LUMEN_VALID_PHASES}）。"
    fi
    LUMEN_CURRENT_PHASE="${phase}"
    LUMEN_CURRENT_PHASE_START_MS="$(lumen_now_ms)"
    printf '::lumen-step:: phase=%s status=start ts=%s\n' \
        "${phase}" "$(lumen_iso_now)"
}

# lumen_step_end <phase> <rc>
# 输出 ::lumen-step:: phase=<name> status=done rc=<int> dur_ms=<int> ts=<iso>
# 在成功路径里手动调用；失败时由 trap 调用（rc 由 trap 计算）。
lumen_step_end() {
    local phase="$1"
    local rc="${2:-0}"
    local dur_ms=0
    if [ -z "${phase}" ]; then
        return 0
    fi
    if [ -n "${LUMEN_CURRENT_PHASE_START_MS:-}" ]; then
        local now_ms
        now_ms="$(lumen_now_ms)"
        # 纯算术：bash 内置即可；避开外部 expr 的字符串风险。
        dur_ms=$(( now_ms - LUMEN_CURRENT_PHASE_START_MS ))
        if [ "${dur_ms}" -lt 0 ]; then
            dur_ms=0
        fi
    fi
    printf '::lumen-step:: phase=%s status=done rc=%s dur_ms=%s ts=%s\n' \
        "${phase}" "${rc}" "${dur_ms}" "$(lumen_iso_now)"
    # 只在结束的是“当前”phase 时清空，避免乱序调用导致状态被错误清空。
    if [ "${LUMEN_CURRENT_PHASE:-}" = "${phase}" ]; then
        LUMEN_CURRENT_PHASE=""
        LUMEN_CURRENT_PHASE_START_MS=""
    fi
}

# lumen_step_info <phase> <key> <value...>
# 输出 ::lumen-info:: phase=<name> key=<k> value=<v>
# value 中的换行 / CR 会被替换为空格，避免破坏单行协议。
lumen_step_info() {
    local phase="$1"
    local key="$2"
    shift 2 || true
    local raw="$*"
    local value
    # 把 CR/LF 折叠成空格，防止输出多行污染协议。
    value="$(printf '%s' "${raw}" | tr '\r\n' '  ')"
    printf '::lumen-info:: phase=%s key=%s value=%s\n' \
        "${phase}" "${key}" "${value}"
}

# 在 ERR/EXIT trap 里调用：如果当前还有进行中的 phase，输出失败的 done 行。
# 防止协议解析方因为缺少 done 行而把整个 phase 误判为悬挂。
lumen_step_finalize_failure() {
    local rc="${1:-1}"
    if [ -n "${LUMEN_CURRENT_PHASE:-}" ]; then
        lumen_step_end "${LUMEN_CURRENT_PHASE}" "${rc}"
    fi
}

LUMEN_DOCKER_USE_SUDO="${LUMEN_DOCKER_USE_SUDO:-0}"

# Maintenance and operation lock helpers are loaded from lib/locking.sh
# at the end of this facade.

lumen_handle_signal() {
    local signal="$1"
    local line="${2:-unknown}"
    local code=1
    case "${signal}" in
        INT) code=130 ;;
        TERM) code=143 ;;
    esac
    trap - INT TERM
    log_error "收到 ${signal}，脚本已中断（第 ${line} 行）。"
    exit "${code}"
}

lumen_install_signal_handlers() {
    trap 'lumen_handle_signal INT "${LINENO}"' INT
    trap 'lumen_handle_signal TERM "${LINENO}"' TERM
}

# confirm "msg"  ->  返回 0 表示 yes，1 表示 no/默认。
# 默认 N，用户必须明确输入 y/Y/yes/YES 才返回 0。
confirm() {
    local prompt="$1"
    local reply=""
    printf '%s%s%s [y/N]: ' "${LUMEN_C_CYAN}" "${prompt}" "${LUMEN_C_RESET}"
    if ! IFS= read -r reply; then
        # EOF（curl|bash 远程模式 / 重定向 stdin / Ctrl-D）下视为 No，但显式
        # 提示，避免用户感觉"我啥也没按怎么就退出了"。
        printf '\n[INFO] (EOF / 非交互输入，视为 No)\n'
        return 1
    fi
    case "${reply}" in
        y|Y|yes|YES|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

# ensure_cmd <name> <install_hint>
# 检查命令是否存在；不存在则打印安装提示并 exit 1。
ensure_cmd() {
    local name="$1"
    local hint="${2:-}"
    if command -v "${name}" >/dev/null 2>&1; then
        return 0
    fi
    log_error "缺少命令 \"${name}\"。请先安装后重试。"
    if [ -n "${hint}" ]; then
        printf '       建议安装方式：%s\n' "${hint}" >&2
    fi
    exit 1
}

sudo_has_tty() {
    [ -r /dev/tty ] && [ -w /dev/tty ]
}

lumen_sudo() {
    if sudo_has_tty; then
        sudo "$@"
    else
        sudo -n "$@"
    fi
}

lumen_docker() {
    if [ "${LUMEN_DOCKER_USE_SUDO:-0}" = "1" ]; then
        lumen_sudo docker "$@"
    else
        docker "$@"
    fi
}

lumen_docker_command_label() {
    if [ "${LUMEN_DOCKER_USE_SUDO:-0}" = "1" ]; then
        printf 'sudo docker'
    else
        printf 'docker'
    fi
}

lumen_detect_docker_access() {
    LUMEN_DOCKER_USE_SUDO=0
    command -v docker >/dev/null 2>&1 || return 1

    if docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        return 0
    fi

    if [ "$(detect_os)" = "linux" ] \
        && [ "${EUID:-$(id -u)}" -ne 0 ] \
        && command -v sudo >/dev/null 2>&1; then
        if lumen_sudo docker compose version >/dev/null 2>&1 \
            && lumen_sudo docker info >/dev/null 2>&1; then
            LUMEN_DOCKER_USE_SUDO=1
            return 0
        fi
    fi

    return 1
}

lumen_require_docker_access() {
    ensure_cmd docker "请安装 Docker 后重试"
    if lumen_detect_docker_access; then
        if [ "${LUMEN_DOCKER_USE_SUDO:-0}" = "1" ]; then
            log_warn "当前用户无法直接访问 Docker，本次将自动使用 sudo docker。"
        fi
        return 0
    fi

    if ! docker compose version >/dev/null 2>&1; then
        log_error "未检测到 docker compose v2。请升级 Docker。"
    else
        log_error "Docker daemon 未运行，或当前用户无权访问 Docker。"
    fi
    if [ "$(detect_os)" = "linux" ]; then
        log_error "请先启动 Docker：sudo systemctl start docker；若是权限问题，可将用户加入 docker 组后重新登录。"
    else
        log_error "请确认 Docker Desktop 已启动并完成初始化。"
    fi
    exit 1
}

# detect_os -> 输出 macos/linux/unknown
detect_os() {
    local uname_s
    uname_s="$(uname -s 2>/dev/null || echo unknown)"
    case "${uname_s}" in
        Darwin) printf 'macos\n' ;;
        Linux) printf 'linux\n' ;;
        *) printf 'unknown\n' ;;
    esac
}

lumen_resolve_repo_root() {
    local script_dir="$1"
    local script_phys probe probe_parent
    script_phys="$(cd "${script_dir}" && pwd -P)"
    probe="$(cd "${script_phys}/.." && pwd -P)"
    probe_parent="$(cd "${probe}/.." && pwd -P)"
    if [ "$(basename "${probe_parent}")" = "releases" ]; then
        (cd "${probe_parent}/.." && pwd -P)
        return 0
    fi
    printf '%s' "${probe}"
}

# port_in_use <port> -> 返回 0 表示被占用，1 表示空闲（或无可用检测工具）
# 优先 lsof，其次 ss，再次 netstat。
port_in_use() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        if lsof -iTCP:"${port}" -sTCP:LISTEN -nP >/dev/null 2>&1; then
            return 0
        fi
        return 1
    fi
    if command -v ss >/dev/null 2>&1; then
        if ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -qE "[:.]${port}\$"; then
            return 0
        fi
        return 1
    fi
    if command -v netstat >/dev/null 2>&1; then
        if netstat -an 2>/dev/null | awk '/LISTEN/ {print $4}' | grep -qE "[:.]${port}\$"; then
            return 0
        fi
        return 1
    fi
    return 1
}

lumen_process_listening_on_port() {
    port_in_use "$1"
}

lumen_http_status() {
    local url="$1"
    if ! command -v curl >/dev/null 2>&1; then
        return 1
    fi
    # 健康检查走本地 loopback。admin 面板触发 update 时会注入 HTTP_PROXY=socks5h://...
    # 给 git/uv/npm 用，但本地 healthz 千万不能走代理——curl 会把 "connect 127.0.0.1" 投递
    # 到代理服务器，落到那台机器自己的 loopback，永远拿不到 lumen-api 的响应。
    curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' \
        --connect-timeout "${LUMEN_HEALTH_CONNECT_TIMEOUT:-2}" \
        --max-time "${LUMEN_HEALTH_MAX_TIME:-8}" \
        "${url}" 2>/dev/null
}

lumen_wait_for_http_ok() {
    local url="$1"
    local attempts="${2:-60}"
    local status=""
    local _attempt
    for _attempt in $(seq 1 "${attempts}"); do
        status="$(lumen_http_status "${url}" || true)"
        case "${status}" in
            2??|3??) return 0 ;;
        esac
        sleep 1
    done
    return 1
}

lumen_wait_for_port() {
    local port="$1"
    local attempts="${2:-60}"
    local _attempt
    for _attempt in $(seq 1 "${attempts}"); do
        if lumen_process_listening_on_port "${port}"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

lumen_env_value() {
    local key="$1"
    local file="${2:-.env}"
    local raw=""
    raw="$(sed -n "s/^${key}=//p" "${file}" 2>/dev/null | head -n1 || true)"
    raw="${raw%$'\r'}"
    if [[ "${raw}" == \'*\' && "${raw}" == *\' ]]; then
        raw="${raw:1:${#raw}-2}"
    elif [[ "${raw}" == \"*\" && "${raw}" == *\" ]]; then
        raw="${raw:1:${#raw}-2}"
    fi
    printf '%s' "${raw}"
}

lumen_set_env_value_in_file() {
    local file="$1"
    local key="$2"
    local value="$3"
    if [ -z "${file}" ] || [ -z "${key}" ]; then
        log_error "lumen_set_env_value_in_file：参数不完整。"
        return 1
    fi
    if [ ! -f "${file}" ]; then
        log_error "lumen_set_env_value_in_file：${file} 不存在。"
        return 1
    fi
    if [[ ! "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        log_error "lumen_set_env_value_in_file：非法 key=${key}。"
        return 1
    fi
    if printf '%s' "${value}" | LC_ALL=C grep -q '[[:cntrl:]]'; then
        log_error "lumen_set_env_value_in_file：${key} 不能包含控制字符。"
        return 1
    fi
    local tmp
    tmp="$(mktemp "${file}.tmp.XXXXXX")" || return 1
    awk -v k="${key}" -v v="${value}" '
        BEGIN { done = 0 }
        $0 ~ "^" k "=" {
            if (done == 0) {
                print k "=" v
                done = 1
            }
            next
        }
        { print }
        END {
            if (done == 0) print k "=" v
        }
    ' "${file}" > "${tmp}" && mv "${tmp}" "${file}"
}

lumen_find_shared_env() {
    local script_root="${1:-}"
    local candidate
    for candidate in \
        "${LUMEN_ENV_FILE:-}" \
        "${script_root:+${script_root}/.env}" \
        "${script_root:+${script_root}/shared/.env}" \
        "/opt/lumen/shared/.env"; do
        [ -n "${candidate}" ] || continue
        if [ -f "${candidate}" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

lumen_dotenv_export_if_unset() {
    local key="$1"
    local file="$2"
    local value=""
    if [ -n "${!key:-}" ]; then
        return 0
    fi
    if [ ! -f "${file}" ]; then
        return 0
    fi
    value="$(lumen_env_value "${key}" "${file}")"
    if [ -n "${value}" ]; then
        export "${key}=${value}"
    fi
}

lumen_redis_password_from_url() {
    local url="${1:-}"
    case "${url}" in
        redis://*|rediss://*) ;;
        *) return 1 ;;
    esac
    local rest="${url#*://}"
    case "${rest}" in
        *@*) ;;
        *) return 1 ;;
    esac
    local userpass="${rest%@*}"
    case "${userpass}" in
        *:*) printf '%s' "${userpass#*:}" ;;
        *)   printf '%s' "${userpass}" ;;
    esac
}

# 优先以 REDIS_URL 嵌入密码为准（与 docker-compose 中 api/worker 共用同一 URL，
# 即容器实际 requirepass）；fallback 到 .env 单独那一行 REDIS_PASSWORD。
# 调用前确保 REDIS_URL / REDIS_PASSWORD 已 export 到当前 shell。
lumen_redis_resolve_password() {
    local from_url=""
    if [ -n "${REDIS_URL:-}" ]; then
        from_url="$(lumen_redis_password_from_url "${REDIS_URL}" 2>/dev/null || true)"
    fi
    if [ -n "${from_url}" ]; then
        printf '%s' "${from_url}"
        return 0
    fi
    printf '%s' "${REDIS_PASSWORD:-}"
}

# Redis 协议错误（NOAUTH / WRONGPASS / ERR ...）会以正常输出形式返回 stdout
# 且 redis-cli 进程仍 exit 0；wrapper 必须主动识别避免后续把错误当数据处理。
lumen_redis_is_error_reply() {
    case "${1:-}" in
        "(error) "*|"NOAUTH "*|"WRONGPASS "*|"AUTH failed"*|"ERR "*|"ERROR "*|"NOPERM "*|"NOSCRIPT "*)
            return 0
            ;;
    esac
    return 1
}

# 从 LUMEN_REPO_URL 对应的 GitHub raw 拉指定脚本到 scripts_dir，让管理脚本能"自我升级"。
# 失败软降级（network/校验错只 WARN，不阻塞 caller）；TTL marker 防止短时间重复拉。
#
# 用法：
#   lumen_self_update_scripts <scripts_dir> [branch] [ttl_sec] [files...]
#
# 默认参数：
#   branch    = ${LUMEN_SELF_UPDATE_BRANCH:-main}
#   ttl_sec   = ${LUMEN_SELF_UPDATE_TTL:-600}（10 分钟内复调直接 noop；0 / FORCE=1 强拉）
#   files...  = lib.sh lib/runtime.sh lib/locking.sh lib/container_release.sh
#               release_manifest_guard.py update_runner.py restore_runner.py
#               backup.sh restore.sh update.sh lumenctl.sh
#
# 输出（全局变量；调用方据此决定后续动作）：
#   LUMEN_SELF_UPDATE_RESULT     ok | skipped | failed | disabled
#   LUMEN_SELF_UPDATE_CHANGED    "f1 f2 ..."（空格分隔；可空表示远端=本地）
#   LUMEN_SELF_UPDATE_BACKUP_TS  YYYYMMDD-HHMMSS（备份后缀；仅 ok 时有效）
#   LUMEN_SELF_UPDATE_SOURCE     raw URL base
#
# 环境变量：
#   LUMEN_REPO_URL               默认 https://github.com/cyeinfpro/Lumen.git
#   LUMEN_SELF_UPDATE_FORCE=1    突破 TTL 强制拉
#   LUMEN_SELF_UPDATE=0          全局关闭（caller 自己另外暴露开关也可以）
#
# 返回值：始终 0（softfail；caller 看 LUMEN_SELF_UPDATE_RESULT 判定）。
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

lumen_self_update_scripts() {
    LUMEN_SELF_UPDATE_RESULT=skipped
    LUMEN_SELF_UPDATE_CHANGED=""
    LUMEN_SELF_UPDATE_BACKUP_TS=""
    LUMEN_SELF_UPDATE_SOURCE=""

    local scripts_dir="${1:-}"
    local branch="${2:-${LUMEN_SELF_UPDATE_BRANCH:-main}}"
    local ttl_sec="${3:-${LUMEN_SELF_UPDATE_TTL:-600}}"
    if [ "$#" -gt 3 ]; then
        shift 3
    else
        shift "$#"
    fi
    local files=("$@")
    local module_files=(
        lib/runtime.sh
        lib/locking.sh
        lib/container_release.sh
    )
    local python_helper_files=(
        release_manifest_guard.py
        update_runner.py
        restore_runner.py
    )
    if [ "${#files[@]}" -eq 0 ]; then
        files=(
            lib.sh
            lib/runtime.sh
            lib/locking.sh
            lib/container_release.sh
            release_manifest_guard.py
            update_runner.py
            restore_runner.py
            backup.sh
            restore.sh
            update.sh
            lumenctl.sh
        )
    else
        # lib.sh、lib/*.sh 与 Python runners/guard 是一个版本单元。旧 stable
        # updater 的显式清单只包含 shell 文件；自动补齐 helper，避免新 update.sh
        # 在首跳 re-exec 后仍调用旧 release_manifest_guard.py。
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

    # Install dependencies before the facade/update entrypoints so an
    # interrupted replacement cannot leave new shell code pointing at old or
    # missing modules/helpers.
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

    local marker="${scripts_dir}/.lumen-self-update.last"
    local coverage_marker="${scripts_dir}/.lumen-self-update.files"
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
    local raw_base=""
    case "${repo_url}" in
        https://github.com/*)
            local owner_repo="${repo_url#https://github.com/}"
            owner_repo="${owner_repo%.git}"
            raw_base="https://raw.githubusercontent.com/${owner_repo}/${branch}/scripts"
            ;;
    esac
    if [ -z "${raw_base}" ]; then
        if command -v log_warn >/dev/null 2>&1; then
            log_warn "[self_update] LUMEN_REPO_URL 不是 https://github.com/<owner>/<repo>(.git)：${repo_url}，跳过。"
        fi
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
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
    local target target_dir
    for f in "${files[@]}"; do
        if [ "${f}" = "update.sh" ]; then
            update_requested=1
            break
        fi
    done
    for f in "${files[@]}"; do
        target="${scripts_dir}/${f}"
        target_dir="$(dirname "${target}")"
        if ! mkdir -p "${target_dir}"; then
            if command -v log_warn >/dev/null 2>&1; then
                log_warn "[self_update] 无法创建目标模块目录：${target_dir}，跳过 self-update。"
            fi
            rm -rf "${tmp_dir}" 2>/dev/null || true
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        if [ -f "${target}" ] && cmp -s "${tmp_dir}/${f}" "${target}"; then
            continue
        fi
        if [ -f "${target}" ]; then
            cp -a "${target}" "${target}.bak.${LUMEN_SELF_UPDATE_BACKUP_TS}" 2>/dev/null || true
        fi
        cp -a "${tmp_dir}/${f}" "${target}.new"
        mv "${target}.new" "${target}"
        changed="${changed}${f} "
        case "${f}" in
            lib.sh|lib/*.sh) library_changed=1 ;;
        esac
    done

    # Existing callers use changed-file tokens as their re-exec contract:
    # lumenctl watches lib.sh, while update.sh watches update.sh. A module-only
    # change still replaces already-sourced function definitions, so emit the
    # corresponding dependency tokens without requiring caller changes.
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

    # 关键脚本执行权限（只对仓库里本来就 +x 的）
    local exec_f
    for exec_f in backup.sh restore.sh update.sh install.sh uninstall.sh lumenctl.sh migrate_to_releases.sh; do
        if [ -f "${scripts_dir}/${exec_f}" ]; then
            chmod +x "${scripts_dir}/${exec_f}" 2>/dev/null || true
        fi
    done

    # 清理过老的 *.bak.<ts>：每个文件最多保留 LUMEN_SELF_UPDATE_BAK_KEEP（默认 5）份。
    # 时间戳格式 YYYYMMDD-HHMMSS 字典序==时间序，sort+head 取最旧的删除。
    # 用 find 而不是 ls + glob：无匹配时 ls exit 非零会被 set -e + pipefail 误触 abort；
    # find 无匹配仍 exit 0（dir 存在），更稳。
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

    local coverage_tmp="${coverage_marker}.tmp.$$"
    if printf '%s\n' "${files[@]}" | sort -u > "${coverage_tmp}" 2>/dev/null; then
        mv -f "${coverage_tmp}" "${coverage_marker}" 2>/dev/null || true
    else
        rm -f "${coverage_tmp}" 2>/dev/null || true
    fi
    date -u +%s > "${marker}" 2>/dev/null || true
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

lumen_effective_proxy_url() {
    local env_file="${1:-}"
    local key value
    for key in LUMEN_UPDATE_PROXY_URL LUMEN_HTTP_PROXY HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy; do
        value="${!key:-}"
        if [ -z "${value}" ] && [ -n "${env_file}" ] && [ -f "${env_file}" ]; then
            value="$(lumen_env_value "${key}" "${env_file}")"
        fi
        if [ -n "${value}" ]; then
            printf '%s' "${value}"
            return 0
        fi
    done
    return 1
}

lumen_configure_proxy_env() {
    local env_file="${1:-}"
    local proxy_url no_proxy_value
    proxy_url="$(lumen_effective_proxy_url "${env_file}" 2>/dev/null || true)"
    if [ -z "${proxy_url}" ]; then
        return 1
    fi
    export LUMEN_UPDATE_PROXY_URL="${proxy_url}"
    export LUMEN_HTTP_PROXY="${proxy_url}"
    export HTTP_PROXY="${proxy_url}"
    export HTTPS_PROXY="${proxy_url}"
    export ALL_PROXY="${proxy_url}"
    export http_proxy="${proxy_url}"
    export https_proxy="${proxy_url}"
    export all_proxy="${proxy_url}"

    no_proxy_value="${NO_PROXY:-${no_proxy:-}}"
    if [ -z "${no_proxy_value}" ] && [ -n "${env_file}" ] && [ -f "${env_file}" ]; then
        no_proxy_value="$(lumen_env_value NO_PROXY "${env_file}")"
        [ -n "${no_proxy_value}" ] || no_proxy_value="$(lumen_env_value no_proxy "${env_file}")"
    fi
    no_proxy_value="${no_proxy_value:-127.0.0.1,localhost,::1}"
    export NO_PROXY="${no_proxy_value}"
    export no_proxy="${no_proxy_value}"

    printf '%s' "${proxy_url}"
}

# Host runtime, systemd, storage ownership, and local process helpers are loaded
# from lib/runtime.sh at the end of this facade.

# lumen_root —— 解析 BASH_SOURCE[1] 所在目录的上级（项目根目录）
# 注意：本函数依赖调用者通过 source 引入本文件，从其所在的脚本路径反推。
# 用法： ROOT="$(lumen_root)"
lumen_root() {
    local src="${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}"
    local script_dir
    script_dir="$(cd "$(dirname "${src}")" && pwd)"
    # script_dir 形如 /path/to/lumen/scripts，取上级
    (cd "${script_dir}/.." && pwd)
}

# read_or_default "msg" "default" -> 输出用户输入或默认值
# 用法：  val="$(read_or_default '提示' '默认值')"
read_or_default() {
    local prompt="$1"
    local default="${2:-}"
    local reply=""
    if [ -n "${default}" ]; then
        printf '%s%s%s [%s]: ' "${LUMEN_C_CYAN}" "${prompt}" "${LUMEN_C_RESET}" "${default}" >&2
    else
        printf '%s%s%s: ' "${LUMEN_C_CYAN}" "${prompt}" "${LUMEN_C_RESET}" >&2
    fi
    if [ -r /dev/tty ] && IFS= read -r reply 2>/dev/null </dev/tty; then
        :
    elif ! IFS= read -r reply; then
        reply=""
    fi
    if [ -z "${reply}" ]; then
        printf '%s' "${default}"
    else
        printf '%s' "${reply}"
    fi
}

# read_secret "msg" -> 用 read -s 静默读密码，输出到 stdout（用法： pwd="$(read_secret 'Password')"）
read_secret() {
    local prompt="$1"
    local reply=""
    printf '%s%s%s: ' "${LUMEN_C_CYAN}" "${prompt}" "${LUMEN_C_RESET}" >&2
    # -s 静默读取；某些终端不支持 -s，则降级
    if [ -r /dev/tty ] && IFS= read -rs reply </dev/tty 2>/dev/null; then
        printf '\n' >&2
    elif [ -r /dev/tty ]; then
        if ! IFS= read -r reply </dev/tty; then
            reply=""
        fi
    else
        if ! IFS= read -r reply; then
            reply=""
        fi
    fi
    printf '%s' "${reply}"
}

# ---------------------------------------------------------------------------
# Release / shared 目录工具
# 用于 Capistrano 风格的 release 切换：
#   ${ROOT}/current      -> releases/<active>
#   ${ROOT}/previous     -> releases/<previous>
#   ${ROOT}/releases/<id>/   全量代码 + .venv + node_modules + .next
#   ${ROOT}/shared/web-env/.env.local
#   ${ROOT}/shared/worker-var/
#   ${ROOT}/shared/web-next-cache/
# ---------------------------------------------------------------------------

# release id：UTC 时间 + sha7。按字典序排序即时间序，便于 cleanup 保留最近 N 个。
lumen_release_id() {
    local sha="${1:-unknown}"
    # 截断到 7 位：与 git rev-parse --short 保持一致；不足 7 位时直接补字面量。
    local short
    short="$(printf '%s' "${sha}" | cut -c1-7)"
    [ -n "${short}" ] || short="unknown"
    # 加 PID 后缀避免同一秒内重跑时 release_id collide（rsync 会污染同一目录）。
    printf '%sZ-%s-%s' "$(date -u +%Y%m%dT%H%M%S)" "${short}" "$$"
}

# 读取 ${ROOT}/current 当前指向的 release 目录的绝对路径。
# 不是 symlink 时返回空串。
lumen_release_current_path() {
    local root="$1"
    local cur="${root}/current"
    [ -L "${cur}" ] || return 0
    if command -v readlink >/dev/null 2>&1; then
        # readlink -f 在 BSD 也可用（macOS 12+ 的 coreutils）；不行就回退到自己拼。
        local target
        target="$(readlink -f "${cur}" 2>/dev/null || true)"
        if [ -n "${target}" ]; then
            printf '%s' "${target}"
            return 0
        fi
        target="$(readlink "${cur}" 2>/dev/null || true)"
        case "${target}" in
            /*) printf '%s' "${target}" ;;
            '') ;;
            *) printf '%s/%s' "${root}" "${target}" ;;
        esac
    fi
}

# 读取 ${ROOT}/current 指向 release 的 id（即目录名），不是 symlink 返回空串。
lumen_release_current_id() {
    local root="$1"
    local target
    target="$(lumen_release_current_path "${root}" || true)"
    [ -n "${target}" ] || return 0
    basename "${target}"
}

# 检测 GNU mv 是否支持 -T 选项（用于真正原子的 symlink 替换）。
# 0 = 支持；1 = 不支持（macOS / BSD 默认）。
lumen_mv_has_T() {
    # `mv --version` GNU 才支持；BSD mv 会报 illegal option。
    mv --version >/dev/null 2>&1 || return 1
    # GNU mv 全部支持 -T（since coreutils 6.x）。
    return 0
}

# lumen_atomic_replace_symlink <link_target> <link_path>
# 跨平台原子替换 symlink。优先级：
#   1. GNU `mv -T`（rename(2) syscall，POSIX 保证原子）
#   2. python3 os.replace（也是 rename(2) 一次完成，BSD/macOS 上严格原子）
#   3. `ln -sfn`（unlink+symlink 两步，存在 µs 级窗口；最后兜底）
# link_target 是软链内容（通常相对路径如 "releases/<id>"）；link_path 是绝对路径。
lumen_atomic_replace_symlink() {
    local link_target="$1"
    local link_path="$2"
    local link_dir
    link_dir="$(dirname "${link_path}")"
    local link_name
    link_name="$(basename "${link_path}")"
    local tmp="${link_dir}/.${link_name}.tmp.$$"

    rm -f "${tmp}" 2>/dev/null || true
    if ! ln -s "${link_target}" "${tmp}"; then
        return 1
    fi

    if lumen_mv_has_T; then
        if mv -T "${tmp}" "${link_path}"; then
            return 0
        fi
        rm -f "${tmp}" 2>/dev/null || true
        return 1
    fi

    if command -v python3 >/dev/null 2>&1; then
        # os.replace 直接 rename(2)，跨平台原子；目标是 symlink 本身（不解引用）。
        if python3 -c "import os, sys; os.replace(sys.argv[1], sys.argv[2])" "${tmp}" "${link_path}" 2>/dev/null; then
            return 0
        fi
    fi

    # 最后兜底：ln -sfn。窗口极短，仅作 fallback。
    rm -f "${tmp}" 2>/dev/null || true
    ln -sfn "${link_target}" "${link_path}" 2>/dev/null || return 1
    return 0
}

# lumen_release_atomic_switch <root> <new_id>
# 原子地把 ${root}/current 切到 releases/<new_id>，并把旧 release 写入 ${root}/previous。
# 注意：current/previous 都是相对软链（指 "releases/<id>"），便于整体迁移到不同前缀。
lumen_release_atomic_switch() {
    local root="$1"
    local new_id="$2"
    local old_id=""
    old_id="$(lumen_release_current_id "${root}" || true)"

    if [ -z "${new_id}" ]; then
        log_error "lumen_release_atomic_switch：new_id 为空。"
        return 1
    fi
    if [ ! -d "${root}/releases/${new_id}" ]; then
        log_error "lumen_release_atomic_switch：不存在 releases/${new_id}。"
        return 1
    fi

    if ! lumen_atomic_replace_symlink "releases/${new_id}" "${root}/current"; then
        log_error "切换 ${root}/current → releases/${new_id} 失败。"
        return 1
    fi

    # 更新 previous 软链（指向旧 release）。失败不致命。
    if [ -n "${old_id}" ] && [ "${old_id}" != "${new_id}" ] \
        && [ -d "${root}/releases/${old_id}" ]; then
        lumen_atomic_replace_symlink "releases/${old_id}" "${root}/previous" 2>/dev/null || true
    fi
    return 0
}

# lumen_release_link_shared <release_dir> <shared_dir>
# 把 shared 目录下的几条已知路径软链到 release 内对应位置。
# 调用前 release 内的同名文件/目录如果存在会被备份到 .pre-link 后再删除（避免 ln 报错）。
lumen_release_link_shared() {
    local release_dir="$1"
    local shared_dir="$2"
    if [ ! -d "${release_dir}" ]; then
        log_error "lumen_release_link_shared：release 目录不存在：${release_dir}"
        return 1
    fi
    if [ ! -d "${shared_dir}" ]; then
        log_error "lumen_release_link_shared：shared 目录不存在：${shared_dir}"
        return 1
    fi

    # 四条软链。第二个字段为 shared 下的物理路径，第三个字段为 release 内目标路径。
    # 用换行分隔，避开复杂关联数组（兼容 bash 3.2 / macOS）。
    # .env 是 docker compose 启动 PostgreSQL / Redis 时读取的，release 是
    # git clone 出来的纯净树没有 .env，必须从 shared 链入；否则 containers
    # phase 会因为 "required variable DB_USER is missing a value" 失败。
    local mapping="
web-env/.env.local|apps/web/.env.local
worker-var|apps/worker/var
web-next-cache|apps/web/.next/cache
.env|.env
"
    local line src_rel dst_rel src dst dst_parent
    while IFS= read -r line; do
        [ -n "${line}" ] || continue
        src_rel="${line%%|*}"
        dst_rel="${line#*|}"
        src="${shared_dir}/${src_rel}"
        dst="${release_dir}/${dst_rel}"
        dst_parent="$(dirname "${dst}")"

        # shared 下的源不存在则跳过（例如 .env.local 在某些环境可能没有）。
        if [ ! -e "${src}" ] && [ ! -L "${src}" ]; then
            log_warn "shared 中缺少 ${src_rel}，跳过软链 ${dst_rel}。"
            continue
        fi

        mkdir -p "${dst_parent}" 2>/dev/null || true

        # 若 release 内已经有同名实体，先移走（不删除，备份成 .pre-link.<ts>）。
        if [ -e "${dst}" ] || [ -L "${dst}" ]; then
            local backup
            backup="${dst}.pre-link.$(date -u +%Y%m%d%H%M%S)"
            if ! mv "${dst}" "${backup}" 2>/dev/null; then
                # 兜底删除：通过 lumen_safe_rm_rf 拦截 / /usr 等系统目录路径
                lumen_safe_rm_rf "${dst}" 2>/dev/null || true
            fi
        fi

        if ! ln -s "${src}" "${dst}"; then
            log_error "无法软链 ${dst} -> ${src}"
            return 1
        fi
    done <<EOF
${mapping}
EOF
    return 0
}

# lumen_release_cleanup_old <root> <keep>
# 保留按字典序最新的 <keep> 个 release，其余删除。
# 任何被 current/previous 指向的 release 都不会被删，即使它落在保留窗口外。
lumen_release_cleanup_old() {
    local root="$1"
    local keep="${2:-5}"
    local releases_dir="${root}/releases"
    [ -d "${releases_dir}" ] || return 0

    # 取出 current/previous 指向的 release id（仅 basename，避免跨平台
    # readlink/canonical 路径不一致——macOS /tmp -> /private/tmp 等情况）。
    local current_id previous_id
    current_id="$(lumen_release_current_id "${root}" || true)"
    previous_id=""
    if [ -L "${root}/previous" ]; then
        local prev_link
        prev_link="$(readlink "${root}/previous" 2>/dev/null || true)"
        if [ -n "${prev_link}" ]; then
            previous_id="$(basename "${prev_link}")"
        fi
    fi

    # 列出所有 release 子目录，按字典序倒排（最新的在前）。
    local -a all_ids=()
    local entry
    for entry in "${releases_dir}"/*; do
        [ -d "${entry}" ] || continue
        all_ids+=("$(basename "${entry}")")
    done
    if [ "${#all_ids[@]}" -le "${keep}" ]; then
        return 0
    fi

    # bash 3.2 没有 mapfile，用排序+逐行读。
    local -a sorted=()
    local id
    while IFS= read -r id; do
        sorted+=("${id}")
    done < <(printf '%s\n' "${all_ids[@]}" | sort -r)

    local kept=0
    local target removed=0
    for id in "${sorted[@]}"; do
        target="${releases_dir}/${id}"
        # 当前 current 或 previous 指向的，无条件保留。
        if [ -n "${current_id}" ] && [ "${id}" = "${current_id}" ]; then
            kept=$((kept+1))
            continue
        fi
        if [ -n "${previous_id}" ] && [ "${id}" = "${previous_id}" ]; then
            kept=$((kept+1))
            continue
        fi
        if [ "${kept}" -lt "${keep}" ]; then
            kept=$((kept+1))
        else
            # 删除。Linux 上 rm -rf 数百 MB 通常 < 1s。
            if rm -rf "${target}" 2>/dev/null; then
                removed=$((removed+1))
            fi
        fi
    done
    if [ "${removed}" -gt 0 ]; then
        log_info "release cleanup：删除 ${removed} 个旧 release，保留 ${keep} 个。"
    fi
    return 0
}

# Docker Compose, image verification, release manifest, and image tag helpers
# are loaded from lib/container_release.sh at the end of this facade.

# 输出 ::lumen-step:: 结构化阶段日志；接受 key=val 透传，自动追加 ts。
# 同时写 stdout + stderr，方便 SSE 与 tee 日志双路捕获。
lumen_emit_step() {
    local line
    line="$(printf '::lumen-step::')"
    local arg
    for arg in "$@"; do
        line="${line} ${arg}"
    done
    line="${line} ts=$(lumen_iso_now)"
    printf '%s\n' "${line}"
    printf '%s\n' "${line}" >&2
}

# 输出 ::lumen-info:: 结构化信息行；语义同 lumen_emit_step。
lumen_emit_info() {
    local line
    line="$(printf '::lumen-info::')"
    local arg
    for arg in "$@"; do
        line="${line} ${arg}"
    done
    line="${line} ts=$(lumen_iso_now)"
    printf '%s\n' "${line}"
    printf '%s\n' "${line}" >&2
}

# ---------------------------------------------------------------------------
# 路径安全 & 重试 & release 维护（install/update/uninstall 复用）
# ---------------------------------------------------------------------------

# lumen_path_safe_for_rm <path>
# 校验 <path> 适合作为 rm -rf 的目标。返回 0=safe，1=unsafe（已 log_error）。
# 拒绝：空 / 非绝对 / 长度 < 5 / 含 .. / 等于以下"系统目录"之一：
#   /  /bin /boot /dev /etc /home /lib /lib32 /lib64 /opt /proc /root /run
#   /sbin /srv /sys /tmp /usr /var /Applications /Library /System /Users /private
# 注意：仅拦截"等于"系统目录；/opt/lumen, /opt/lumendata 等子路径不受影响。
lumen_path_safe_for_rm() {
    local p="$1"
    local home_dir="${HOME:-}"
    if [ -z "${p}" ]; then
        log_error "lumen_path_safe_for_rm: 路径为空，拒绝删除。"
        return 1
    fi
    case "${p}" in
        /*) ;;
        *)
            log_error "lumen_path_safe_for_rm: '${p}' 不是绝对路径，拒绝删除。"
            return 1
            ;;
    esac
    if [ "${#p}" -lt 5 ]; then
        log_error "lumen_path_safe_for_rm: '${p}' 路径过短，拒绝删除。"
        return 1
    fi
    case "${p}" in
        *..*)
            log_error "lumen_path_safe_for_rm: '${p}' 包含 '..'，拒绝删除。"
            return 1
            ;;
    esac
    case "${p}" in
        /|/bin|/boot|/dev|/etc|/home|/lib|/lib32|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/Applications|/Library|/System|/Users|/private)
            log_error "lumen_path_safe_for_rm: '${p}' 是系统目录，拒绝删除。"
            return 1
            ;;
    esac
    if [ -n "${home_dir}" ] && [ "${p%/}" = "${home_dir%/}" ]; then
        log_error "lumen_path_safe_for_rm: '${p}' 是当前用户 HOME，拒绝删除。"
        return 1
    fi
    # 移除多余的尾部斜杠后再次校验（避免 "/opt/" 通过）
    local trimmed="${p}"
    while [[ "${trimmed}" == */ && "${trimmed}" != "/" ]]; do
        trimmed="${trimmed%/}"
    done
    case "${trimmed}" in
        /|/bin|/boot|/dev|/etc|/home|/lib|/lib32|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/Applications|/Library|/System|/Users|/private)
            log_error "lumen_path_safe_for_rm: 规范化后 '${trimmed}' 仍是系统目录，拒绝删除。"
            return 1
            ;;
    esac
    if [ -n "${home_dir}" ] && [ "${trimmed}" = "${home_dir%/}" ]; then
        log_error "lumen_path_safe_for_rm: 规范化后 '${trimmed}' 是当前用户 HOME，拒绝删除。"
        return 1
    fi
    return 0
}

# lumen_safe_rm_rf <path>
# 在 rm -rf 之前用 lumen_path_safe_for_rm 把关。返回 rm 的退出码（或校验失败时 1）。
lumen_safe_rm_rf() {
    local target="$1"
    if ! lumen_path_safe_for_rm "${target}"; then
        return 1
    fi
    rm -rf -- "${target}"
}

# lumen_safe_rm_rf_as_root <path>
# 同 lumen_safe_rm_rf，但通过 lumen_run_as_root 执行（处理需要 root 权限的目录）。
lumen_safe_rm_rf_as_root() {
    local target="$1"
    if ! lumen_path_safe_for_rm "${target}"; then
        return 1
    fi
    lumen_run_as_root rm -rf -- "${target}"
}

# lumen_release_remove_unused <root> <release_id>
# 删除一个 release 目录，但拒绝删除 current/previous 当前指向的 release。
# 失败/被拒绝时 log_warn 并返回非零；不抛异常（让调用方决定如何处理）。
lumen_release_remove_unused() {
    local root="$1"
    local release_id="$2"
    if [ -z "${release_id}" ]; then
        log_warn "lumen_release_remove_unused: release_id 为空，跳过。"
        return 1
    fi
    local target="${root}/releases/${release_id}"
    if [ ! -d "${target}" ]; then
        return 0
    fi
    local cur_id prev_id=""
    cur_id="$(lumen_release_current_id "${root}" || true)"
    if [ -L "${root}/previous" ]; then
        local prev_link
        prev_link="$(readlink "${root}/previous" 2>/dev/null || true)"
        [ -n "${prev_link}" ] && prev_id="$(basename "${prev_link}")"
    fi
    if [ "${release_id}" = "${cur_id}" ]; then
        log_warn "lumen_release_remove_unused: ${release_id} 是当前 current，拒绝删除。"
        return 1
    fi
    if [ "${release_id}" = "${prev_id}" ]; then
        log_warn "lumen_release_remove_unused: ${release_id} 是 previous，拒绝删除。"
        return 1
    fi
    if ! lumen_path_safe_for_rm "${target}"; then
        return 1
    fi
    if rm -rf -- "${target}" 2>/dev/null; then
        log_info "已删除未使用的 release：${target}"
        return 0
    fi
    if lumen_run_as_root rm -rf -- "${target}" 2>/dev/null; then
        log_info "已删除未使用的 release（root 权限）：${target}"
        return 0
    fi
    log_warn "无法删除 release 目录：${target}"
    return 1
}

# lumen_retry <max_attempts> <initial_delay_seconds> <label> <cmd...>
# 指数退避重试。每次失败后 sleep delay，下次 delay 翻倍（最大 30s）。
# label 仅用于日志（如 "docker compose pull"）。返回最后一次的退出码。
lumen_retry() {
    local max_attempts="$1"
    local delay="$2"
    local label="$3"
    shift 3
    local attempt=1
    local rc=0
    while :; do
        rc=0
        "$@" || rc=$?
        if [ "${rc}" -eq 0 ]; then
            return 0
        fi
        # 用户中断（SIGINT=130 / SIGTERM=143）立即 break，不要白白退避 5/10/20s
        # 浪费用户时间。下游也能更快进入 EXIT trap 的清理流程。
        if [ "${rc}" -eq 130 ] || [ "${rc}" -eq 143 ]; then
            log_warn "${label}：被信号中断（rc=${rc}），不再重试。"
            return "${rc}"
        fi
        if [ "${attempt}" -ge "${max_attempts}" ]; then
            log_error "${label}：连续 ${attempt} 次失败（rc=${rc}），不再重试。"
            return "${rc}"
        fi
        log_warn "${label}：第 ${attempt} 次失败（rc=${rc}），${delay}s 后重试。"
        sleep "${delay}"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
        if [ "${delay}" -gt 30 ]; then
            delay=30
        fi
    done
}

# ---------------------------------------------------------------------------
# Structured module facade
# ---------------------------------------------------------------------------

# Resolve modules from lib.sh itself, never from caller-owned SCRIPT_DIR. This
# keeps sourcing reliable through release symlinks and paths containing spaces.
_LUMEN_LIB_SOURCE="${BASH_SOURCE[0]:-}"
if [ -z "${_LUMEN_LIB_SOURCE}" ]; then
    log_error "无法解析 scripts/lib.sh 路径，不能加载 shell 模块。"
    if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
        exit 1
    fi
    return 1
fi
_LUMEN_LIB_SCRIPTS_DIR="$(cd "$(dirname "${_LUMEN_LIB_SOURCE}")" && pwd -P)"
_LUMEN_LIB_MODULES=(
    lib/runtime.sh
    lib/locking.sh
    lib/container_release.sh
)
_LUMEN_LIB_MISSING=()
for _LUMEN_LIB_MODULE in "${_LUMEN_LIB_MODULES[@]}"; do
    if [ ! -f "${_LUMEN_LIB_SCRIPTS_DIR}/${_LUMEN_LIB_MODULE}" ]; then
        _LUMEN_LIB_MISSING+=("${_LUMEN_LIB_MODULE}")
    fi
done

# Transition compatibility: an older self-updater only knew about lib.sh. If it
# installs this facade first, fetch the missing version-matched modules before
# sourcing them. Normal checkouts/releases never enter this branch.
if [ "${#_LUMEN_LIB_MISSING[@]}" -gt 0 ]; then
    log_warn "lib.sh 缺少模块，尝试从同一脚本分支补齐：${_LUMEN_LIB_MISSING[*]}"
    _LUMEN_LIB_FORCE_WAS_SET=0
    _LUMEN_LIB_FORCE_PREVIOUS=""
    if [ "${LUMEN_SELF_UPDATE_FORCE+x}" = "x" ]; then
        _LUMEN_LIB_FORCE_WAS_SET=1
        _LUMEN_LIB_FORCE_PREVIOUS="${LUMEN_SELF_UPDATE_FORCE}"
    fi
    LUMEN_SELF_UPDATE_FORCE=1
    lumen_self_update_scripts \
        "${_LUMEN_LIB_SCRIPTS_DIR}" \
        "${LUMEN_SELF_UPDATE_BRANCH:-main}" \
        0 \
        ${_LUMEN_LIB_MISSING[@]+"${_LUMEN_LIB_MISSING[@]}"}
    if [ "${_LUMEN_LIB_FORCE_WAS_SET}" -eq 1 ]; then
        LUMEN_SELF_UPDATE_FORCE="${_LUMEN_LIB_FORCE_PREVIOUS}"
    else
        unset LUMEN_SELF_UPDATE_FORCE
    fi
fi

for _LUMEN_LIB_MODULE in "${_LUMEN_LIB_MODULES[@]}"; do
    if [ ! -f "${_LUMEN_LIB_SCRIPTS_DIR}/${_LUMEN_LIB_MODULE}" ]; then
        log_error "缺少 shell 模块：${_LUMEN_LIB_SCRIPTS_DIR}/${_LUMEN_LIB_MODULE}"
        if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
            exit 1
        fi
        return 1
    fi
    # shellcheck source=/dev/null
    . "${_LUMEN_LIB_SCRIPTS_DIR}/${_LUMEN_LIB_MODULE}"
done

unset _LUMEN_LIB_SOURCE
unset _LUMEN_LIB_SCRIPTS_DIR
unset _LUMEN_LIB_MODULES
unset _LUMEN_LIB_MISSING
unset _LUMEN_LIB_MODULE
unset _LUMEN_LIB_FORCE_WAS_SET
unset _LUMEN_LIB_FORCE_PREVIOUS
