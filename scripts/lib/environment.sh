#!/usr/bin/env bash
# Sourced by scripts/lib.sh; do not execute directly.

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
import os
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
    os.chmod(backup, 0o600)
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
