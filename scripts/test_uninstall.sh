#!/usr/bin/env bash
# Lightweight checks for uninstall helpers. This is intentionally shell-only so
# it can run on fresh servers before Python or Node dependencies are installed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

test_release_root_resolution() {
    local tmp root release script_dir resolved
    tmp="$(mktemp -d)"
    root="${tmp}/opt/lumen"
    release="${root}/releases/20260503010101"
    script_dir="${release}/scripts"
    mkdir -p "${script_dir}" "${root}/shared"
    ln -sfn "releases/20260503010101" "${root}/current"
    root="$(cd "${root}" && pwd -P)"

    resolved="$(
        # shellcheck source=scripts/lib.sh
        . "${SCRIPT_DIR}/lib.sh"
        lumen_resolve_repo_root "${root}/current/scripts"
    )"

    if [ "${resolved}" != "${root}" ]; then
        printf 'expected release root %s, got %s\n' "${root}" "${resolved}" >&2
        rm -rf "${tmp}"
        return 1
    fi
    rm -rf "${tmp}"
}

test_uninstall_nginx_scan() {
    local tmp match_count
    tmp="$(mktemp -d)"
    mkdir -p "${tmp}/sites-enabled"
    cat > "${tmp}/sites-enabled/lumen.conf" <<'EOF'
# Managed by scripts/lumenctl.sh.
upstream lumen_web_example {
  server 127.0.0.1:3000;
}
server {
  listen 80;
  server_name lumen.example.com;
  location / { proxy_pass http://lumen_web_example; }
}
EOF
    cat > "${tmp}/sites-enabled/other.conf" <<'EOF'
server {
  listen 80;
  server_name other.example.com;
  location / { return 200 "ok"; }
}
EOF

    match_count="$(
        # shellcheck source=scripts/uninstall.sh
        eval "$(
            awk '
            /^LUMEN_NGINX_ACTIVE_DIRS=/ {emit=1}
            /^lumen_uninstall_disable_nginx_configs\(\)/ {emit=0}
            emit {print}
            ' "${SCRIPT_DIR}/uninstall.sh"
        )"
        LUMEN_NGINX_ACTIVE_DIRS=("${tmp}/sites-enabled")
        lumen_uninstall_collect_nginx_candidates | wc -l | tr -d ' '
    )"

    if [ "${match_count}" != "1" ]; then
        printf 'expected one nginx candidate, got %s\n' "${match_count}" >&2
        rm -rf "${tmp}"
        return 1
    fi
    rm -rf "${tmp}"
}

test_release_root_resolution
test_uninstall_nginx_scan
printf 'test_uninstall.sh: ok\n'
