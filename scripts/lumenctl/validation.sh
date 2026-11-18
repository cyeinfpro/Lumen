#!/usr/bin/env bash
# Privilege, input validation, and upstream probe helpers for lumenctl.sh.

require_sudo() {
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
        LUMEN_USE_SUDO=0
        return 0
    fi
    ensure_cmd sudo "请安装 sudo，或切换到 root 后重试"
    LUMEN_USE_SUDO=1
}

as_sudo() {
    if [ "${LUMEN_USE_SUDO:-0}" = "1" ]; then
        sudo "$@"
    else
        "$@"
    fi
}

ensure_linux_systemd() {
    if [ "$(detect_os)" != "linux" ]; then
        log_error "image-job systemd/nginx 自动部署仅支持 Linux 服务器。"
        exit 1
    fi
    ensure_cmd systemctl "请使用带 systemd 的 Linux 服务器"
}

strip_trailing_slash() {
    local value="$1"
    while [[ "${value}" == */ ]]; do
        value="${value%/}"
    done
    printf '%s' "${value}"
}

sanitize_name() {
    local value="$1"
    value="${value#http://}"
    value="${value#https://}"
    value="${value%%/*}"
    value="${value%%:*}"
    value="$(printf '%s' "${value}" | tr -c 'A-Za-z0-9_.-' '-')"
    value="${value##-}"
    value="${value%%-}"
    printf '%s' "${value:-site}"
}

validate_no_control_chars() {
    local name="$1"
    local value="$2"
    if printf '%s' "${value}" | LC_ALL=C grep -q '[[:cntrl:]]'; then
        log_error "${name} 不能包含控制字符。"
        return 1
    fi
    return 0
}

validate_no_whitespace() {
    local name="$1"
    local value="$2"
    validate_no_control_chars "${name}" "${value}" || return 1
    if [[ "${value}" =~ [[:space:]] ]]; then
        log_error "${name} 不能包含空白字符。"
        return 1
    fi
    return 0
}

validate_nginx_token() {
    local name="$1"
    local value="$2"
    validate_no_control_chars "${name}" "${value}" || return 1
    if [[ "${value}" =~ [\;\{\}\'\"\\] ]]; then
        log_error "${name} 不能包含 ; { } 引号或反斜杠。"
        return 1
    fi
    return 0
}

validate_domain_list() {
    local name="$1"
    local value="$2"
    validate_nginx_token "${name}" "${value}" || return 1
    if [ -z "${value}" ]; then
        log_error "${name} 不能为空。"
        return 1
    fi
    local token
    local tokens=()
    IFS=' ' read -r -a tokens <<< "${value}"
    for token in "${tokens[@]}"; do
        [ -n "${token}" ] || continue
        if [[ ! "${token}" =~ ^(\*\.[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+|_)$ ]]; then
            log_error "${name} 包含无效 server_name：${token}"
            return 1
        fi
    done
    return 0
}

validate_url_like() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    validate_nginx_token "${name}" "${value}" || return 1
    if [[ ! "${value}" =~ ^https?:// ]]; then
        log_error "${name} 必须以 http:// 或 https:// 开头。"
        return 1
    fi
    return 0
}

validate_host_port_target() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    validate_nginx_token "${name}" "${value}" || return 1
    if [ -z "${value}" ]; then
        log_error "${name} 不能为空。"
        return 1
    fi
    return 0
}

validate_tcp_port() {
    local name="$1"
    local value="$2"
    validate_no_control_chars "${name}" "${value}" || return 1
    if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
        log_error "${name} 必须是数字。"
        return 1
    fi
    if [ "${value}" -lt 1 ] || [ "${value}" -gt 65535 ]; then
        log_error "${name} 必须在 1-65535 之间。"
        return 1
    fi
    return 0
}

validate_positive_int() {
    local name="$1"
    local value="$2"
    validate_no_control_chars "${name}" "${value}" || return 1
    if [[ ! "${value}" =~ ^[0-9]+$ ]] || [ "${value}" -lt 1 ]; then
        log_error "${name} 必须是 >= 1 的整数。"
        return 1
    fi
    return 0
}

validate_path_value() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    if [ -z "${value}" ]; then
        log_error "${name} 不能为空。"
        return 1
    fi
    if [[ "${value}" =~ [[:cntrl:]\;\{\}\'\"\\] ]]; then
        log_error "${name} 不能包含控制字符、;、{ }、引号或反斜杠。"
        return 1
    fi
    if [ "${value}" = "/" ]; then
        log_error "${name} 不能是根目录 /。"
        return 1
    fi
    return 0
}

validate_absolute_path() {
    local name="$1"
    local value="$2"
    validate_path_value "${name}" "${value}" || return 1
    if [[ "${value}" != /* ]]; then
        log_error "${name} 必须是绝对路径。"
        return 1
    fi
    return 0
}

validate_service_user_name() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    if [[ ! "${value}" =~ ^[A-Za-z_][A-Za-z0-9_.-]*\$?$ ]] && [ "${value}" != "root" ]; then
        log_error "${name} 不是有效的 Linux 用户名：${value}"
        return 1
    fi
    return 0
}

validate_python_command() {
    local name="$1"
    local value="$2"
    validate_no_whitespace "${name}" "${value}" || return 1
    if [[ "${value}" = */* ]]; then
        if [[ "${value}" != /* ]]; then
            log_error "${name} 如包含 /，必须是绝对路径。"
            return 1
        fi
    elif [[ ! "${value}" =~ ^[A-Za-z0-9_.+-]+$ ]]; then
        log_error "${name} 不是有效命令名：${value}"
        return 1
    fi
    return 0
}

ensure_python_min_version() {
    local python_bin="$1"
    local min_major="$2"
    local min_minor="$3"
    if ! lumen_require_python_min_version \
            "${python_bin}" "${min_major}" "${min_minor}"; then
        exit 1
    fi
}

probe_sub2api_upstream() {
    local upstream_base="$1"
    local probe_path probe_url status

    ensure_cmd curl "请安装 curl，用于安装 image-job 前探测 sub2api 上游"
    log_step "检查 sub2api/OpenAI 兼容上游是否可访问"
    log_info "上游地址（按实际部署填写，不固定端口）：${upstream_base}"

    for probe_path in /v1/models /v1/images/generations /v1/responses; do
        probe_url="${upstream_base}${probe_path}"
        status="$(curl -k -sS -o /dev/null -w '%{http_code}' \
            --connect-timeout 3 \
            --max-time 8 \
            "${probe_url}" 2>/dev/null || true)"
        case "${status}" in
            2??|3??|400|401|403|404|405|422)
                log_info "sub2api/OpenAI 兼容端点探测通过：${probe_url} -> HTTP ${status}"
                return 0
                ;;
        esac
    done

    for probe_path in /health /healthz /; do
        probe_url="${upstream_base}${probe_path}"
        status="$(curl -k -sS -o /dev/null -w '%{http_code}' \
            --connect-timeout 3 \
            --max-time 8 \
            "${probe_url}" 2>/dev/null || true)"
        case "${status}" in
            2??|3??|401|403)
                log_warn "上游地址可达：${probe_url} -> HTTP ${status}；请确认它是 sub2api/OpenAI 兼容服务。"
                return 0
                ;;
        esac
    done

    log_error "无法连接 sub2api/OpenAI 兼容上游：${upstream_base}"
    log_error "image-job 必须绑定一个已运行的 sub2api/OpenAI 兼容上游。"
    log_error "请先启动 sub2api，并确认当前机器可访问你填写的地址，例如：curl -i ${upstream_base}/v1/models"
    exit 1
}

validate_nginx_no_root_csp() {
    local file="$1"
    if [ ! -f "${file}" ]; then
        log_error "nginx 配置文件不存在：${file}"
        return 1
    fi
    local check_rc=0
    if command -v python3 >/dev/null 2>&1; then
        python3 - "${file}" <<'PY' || check_rc=$?
from pathlib import Path
import re
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")

def matching_brace(text: str, start: int) -> int:
    depth = 0
    quote = None
    comment = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if comment:
            if ch == "\n":
                comment = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch == "#":
            comment = True
        elif ch in {"'", '"'}:
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    return -1

def server_root_statements(body: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    nested = 0
    quote = None
    comment = False
    escaped = False
    for ch in body:
        if comment:
            if ch == "\n":
                comment = False
                if nested == 0:
                    buf.append(" ")
            continue
        if quote:
            if nested == 0:
                buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch == "#":
            comment = True
            continue
        if ch in {"'", '"'}:
            quote = ch
            if nested == 0:
                buf.append(ch)
            continue
        if ch == "{":
            if nested == 0:
                buf.clear()
            nested += 1
            continue
        if ch == "}":
            nested = max(0, nested - 1)
            if nested == 0:
                buf.clear()
            continue
        if nested == 0:
            if ch == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf.clear()
            else:
                buf.append(ch)
    return statements

csp_re = re.compile(r"(?is)^add_header\s+[\"']?content-security-policy[\"']?(?:\s|$)")
for match in re.finditer(r"(?m)^[ \t]*server[ \t]*\{", source):
    brace = source.find("{", match.start())
    end = matching_brace(source, brace)
    if end == -1:
        continue
    for stmt in server_root_statements(source[brace + 1 : end]):
        if csp_re.match(stmt):
            sys.exit(2)
PY
    else
        awk '
            BEGIN { in_server = 0; depth = 0; bad = 0 }
            {
                line = $0
                sub(/#.*/, "", line)
                if (!in_server && line ~ /^[[:space:]]*server[[:space:]]*\{/) {
                    in_server = 1
                    depth = 1
                    sub(/^[[:space:]]*server[[:space:]]*\{/, "", line)
                }
                if (in_server) {
                    if (depth == 1 && tolower(line) ~ /(^|[[:space:];])add_header[[:space:]]+["\x27]?content-security-policy(["\x27[:space:]]|$)/) {
                        bad = 1
                    }
                    n_open = gsub(/\{/, "{", line)
                    n_close = gsub(/\}/, "}", line)
                    depth += n_open - n_close
                    if (depth <= 0) {
                        in_server = 0
                        depth = 0
                    }
                }
            }
            END { exit(bad ? 2 : 0) }
        ' "${file}" || check_rc=$?
    fi
    if [ "${check_rc}" -ne 0 ]; then
        log_error "禁止在 nginx server {} 根块配置 add_header Content-Security-Policy（会与 Next.js / API CSP 交集锁死）：${file}"
        return 1
    fi
    return 0
}
