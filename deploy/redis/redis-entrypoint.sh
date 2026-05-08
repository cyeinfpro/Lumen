#!/bin/sh
set -eu

if [ -z "${REDIS_PASSWORD:-}" ]; then
    echo "REDIS_PASSWORD is required" >&2
    exit 1
fi

case "$REDIS_PASSWORD" in
*'
'*)
    echo "REDIS_PASSWORD must not contain newlines" >&2
    exit 1
    ;;
esac

CONF_FILE="${REDIS_CONF_FILE:-/tmp/lumen-redis.conf}"
REDIS_MAXMEMORY="${REDIS_MAXMEMORY:-256mb}"
case "$REDIS_MAXMEMORY" in
""|*[!0-9kKmMgGbB]*)
    echo "REDIS_MAXMEMORY must be a Redis memory size such as 256mb" >&2
    exit 1
    ;;
esac

redis_conf_quote() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

escaped_password="$(redis_conf_quote "$REDIS_PASSWORD")"

umask 077
cat > "$CONF_FILE" <<EOF
appendonly yes
appendfsync everysec
save ""
requirepass "$escaped_password"
maxmemory $REDIS_MAXMEMORY
maxmemory-policy allkeys-lru
dir /data
EOF

unset REDIS_PASSWORD escaped_password
exec redis-server "$CONF_FILE"
