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
escaped_password="$(printf '%s' "$REDIS_PASSWORD" | sed 's/\\/\\\\/g; s/"/\\"/g')"

umask 077
cat > "$CONF_FILE" <<EOF
appendonly yes
appendfsync everysec
save ""
requirepass "$escaped_password"
maxmemory 400mb
maxmemory-policy allkeys-lru
dir /data
EOF

unset REDIS_PASSWORD escaped_password
exec redis-server "$CONF_FILE"
