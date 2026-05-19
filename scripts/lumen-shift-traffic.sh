#!/usr/bin/env bash
set -euo pipefail

color="${1:-}"
weight_new="${2:-}"

if [ "${color}" != "green" ] && [ "${color}" != "blue" ]; then
    echo "usage: $0 green|blue 0|50|100" >&2
    exit 2
fi
case "${weight_new}" in
    0|50|100) ;;
    *)
        echo "usage: $0 green|blue 0|50|100" >&2
        exit 2
        ;;
esac

nginx_conf="${LUMEN_NGINX_UPSTREAM_CONF:-/etc/nginx/conf.d/lumen-upstream.conf}"
nginx_bin="${NGINX_BIN:-nginx}"
blue_addr="${LUMEN_BLUE_UPSTREAM:-127.0.0.1:8000}"
green_addr="${LUMEN_GREEN_UPSTREAM:-127.0.0.1:18001}"

if [ "${color}" = "green" ]; then
    blue_weight=$((100 - weight_new))
    green_weight="${weight_new}"
else
    blue_weight="${weight_new}"
    green_weight=$((100 - weight_new))
fi

tmp="$(mktemp "${nginx_conf}.XXXXXX")"
backup=""
cleanup() {
    rm -f "${tmp}"
    if [ -n "${backup}" ]; then
        rm -f "${backup}"
    fi
}
trap cleanup EXIT

cat > "${tmp}" <<EOF
upstream lumen_api {
    zone lumen_api 64k;
    server ${blue_addr} weight=${blue_weight} max_fails=2 fail_timeout=5s;
    server ${green_addr} weight=${green_weight} max_fails=2 fail_timeout=5s;
    keepalive 32;
}
EOF

if [ -f "${nginx_conf}" ]; then
    backup="$(mktemp "${nginx_conf}.bak.XXXXXX")"
    cp "${nginx_conf}" "${backup}"
fi
install -m 0644 "${tmp}" "${nginx_conf}"
if ! "${nginx_bin}" -t; then
    if [ -n "${backup}" ]; then
        install -m 0644 "${backup}" "${nginx_conf}"
    else
        rm -f "${nginx_conf}"
    fi
    "${nginx_bin}" -t >/dev/null 2>&1 || true
    exit 1
fi
"${nginx_bin}" -s reload

echo "::lumen-info:: phase=shift_traffic key=color value=${color}"
echo "::lumen-info:: phase=shift_traffic key=weight_new value=${weight_new}"
echo "::lumen-info:: phase=shift_traffic key=blue_weight value=${blue_weight}"
echo "::lumen-info:: phase=shift_traffic key=green_weight value=${green_weight}"
