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

# 任务 lease（task:<id>:lease）靠 key 存在性表示「worker 仍持有任务」。
# 任何 allkeys-* 策略都可能驱逐运行中任务的 lease，让对账把在跑的任务判成
# 超时并 release_generation 退费——而上游此时已扣费，平台就吸收了成本，
# 违反「视频计费纯转嫁」。因此默认 noeviction：内存打满时写入报错（可观测、
# 可告警），而不是静默丢 lease。
# lease 本身带 TTL，所以 volatile-* 同样会驱逐它——只有 noeviction 是安全的。
REDIS_MAXMEMORY_POLICY="${REDIS_MAXMEMORY_POLICY:-noeviction}"
if [ "$REDIS_MAXMEMORY_POLICY" != "noeviction" ]; then
    echo "REDIS_MAXMEMORY_POLICY=$REDIS_MAXMEMORY_POLICY can evict task leases" >&2
    echo "and turn running jobs into false timeouts. Only noeviction is allowed." >&2
    exit 1
fi

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
maxmemory-policy $REDIS_MAXMEMORY_POLICY
dir /data
EOF

unset REDIS_PASSWORD escaped_password
exec redis-server "$CONF_FILE"
