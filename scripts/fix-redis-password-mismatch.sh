#!/usr/bin/env bash
# Lumen Redis 密码漂移修复脚本
#
# 用途：当 update.sh 报 "redis ping failed before BGSAVE" 或 backup.sh 报
# "AUTH failed: WRONGPASS" 时使用。根因是 shared/.env 里 REDIS_PASSWORD 那一行
# 跟 REDIS_URL 嵌入的密码漂移；容器实际 requirepass = REDIS_URL 嵌入密码
# （因为 api/worker 用它且工作正常）。本脚本把 REDIS_PASSWORD 对齐 REDIS_URL
# 嵌入密码，不动容器、不动数据。
#
# 一键调用：
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/fix-redis-password-mismatch.sh)"
#
# 干跑（只检查不修改）：
#   sudo DRY_RUN=1 bash -c "$(curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/fix-redis-password-mismatch.sh)"
#
# 环境变量：
#   LUMEN_SHARED_ENV   .env 路径（默认 /root/Lumen/shared/.env）
#   REDIS_CONTAINER    redis 容器名（默认 lumen-redis）
#   DRY_RUN=1          只验证不写入

set -euo pipefail

SHARED="${LUMEN_SHARED_ENV:-/root/Lumen/shared/.env}"
REDIS_CONTAINER="${REDIS_CONTAINER:-lumen-redis}"
DRY_RUN="${DRY_RUN:-0}"

log()  { printf '[fix-redis %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
fail() { printf '[fix-redis ERROR] %s\n' "$*" >&2; exit 1; }

# 0. 前置检查
[ -f "$SHARED" ]           || fail "找不到 .env: $SHARED（可用 LUMEN_SHARED_ENV 覆盖）"
command -v docker >/dev/null 2>&1 || fail "docker 不可用"
command -v awk    >/dev/null 2>&1 || fail "awk 不可用"
[ "$(id -u)" -eq 0 ]       || fail "需要 root 权限（请用 sudo 执行）"

# 1. 从 REDIS_URL 解析嵌入密码
P_URL="$(sed -n 's|^REDIS_URL=redis://:||p' "$SHARED" | head -n1 | sed 's|@.*||')"
[ -n "$P_URL" ] || fail "无法从 REDIS_URL 解析嵌入密码；期望格式 REDIS_URL=redis://:<pwd>@host:port/db"

# 2. 当前 REDIS_PASSWORD
P_NOW="$(sed -n 's/^REDIS_PASSWORD=//p' "$SHARED" | head -n1)"

# 3. 已一致 → noop
if [ "$P_URL" = "$P_NOW" ]; then
    log "REDIS_PASSWORD 已经跟 REDIS_URL 嵌入密码一致，无需修复（exit 0）"
    exit 0
fi
log "检测到漂移：REDIS_URL 嵌入密码 (len=${#P_URL}) ≠ REDIS_PASSWORD (len=${#P_NOW})"

# 4. 用 P_URL 验证可连容器（不通说明根因不只是 .env 漂移）
log "用 REDIS_URL 嵌入密码 ping ${REDIS_CONTAINER} ..."
ping_out="$(REDISCLI_AUTH="$P_URL" docker exec -e REDISCLI_AUTH "$REDIS_CONTAINER" \
    redis-cli --no-auth-warning PING 2>&1)" || ping_out="<docker exec failed>"
if [ "$ping_out" != "PONG" ]; then
    fail "容器 ping 失败：${ping_out}
REDIS_URL 嵌入密码连不上 ${REDIS_CONTAINER}。容器实际 requirepass 跟 .env 两处都不一致 —
不是单纯 .env 漂移。请贴本输出寻求人工支持；或重建容器（注意会清空 redis 内存数据）：
  docker compose up -d --force-recreate redis"
fi
log "ping ok — 容器密码 == REDIS_URL 嵌入密码"

# 5. 干跑模式
if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN=1 — 不修改 ${SHARED}；如需写入请去掉 DRY_RUN 重跑"
    exit 0
fi

# 6. 备份 + awk 精确改写 REDIS_PASSWORD 那一行
ts="$(date -u +%Y%m%d-%H%M%S)"
bak="${SHARED}.bak.${ts}"
cp -a "$SHARED" "$bak"
log "备份：$bak"

tmp="$(mktemp)"
awk -v v="$P_URL" '
    /^REDIS_PASSWORD=/ { print "REDIS_PASSWORD=" v; next }
    { print }
' "$SHARED" > "$tmp"
mv "$tmp" "$SHARED"
chmod 600 "$SHARED"

# 7. 校验
P_VERIFY="$(sed -n 's/^REDIS_PASSWORD=//p' "$SHARED" | head -n1)"
[ "$P_VERIFY" = "$P_URL" ] || fail "写入未生效（请回滚：cp ${bak} ${SHARED}）"

log "OK: REDIS_PASSWORD 已对齐 REDIS_URL 嵌入密码"
log "下一步：触发 admin 一键更新，或 bash /root/Lumen/current/scripts/lumenctl.sh update-lumen"
log "如需回滚：sudo cp ${bak} ${SHARED}"
