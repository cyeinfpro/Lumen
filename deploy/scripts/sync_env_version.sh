#!/usr/bin/env bash
# 把当前 git commit 的短 hash 写入 /opt/lumen/.env 的 LUMEN_VERSION 字段。
# 在发布脚本（rsync 之后、systemctl restart 之前）调用一次。
#
# 用法：
#   sudo deploy/scripts/sync_env_version.sh             # 默认写到 /opt/lumen/.env
#   sudo deploy/scripts/sync_env_version.sh /custom/.env

set -euo pipefail

ENV_FILE="${1:-/opt/lumen/.env}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "error: $ENV_FILE not found" >&2
    exit 1
fi

# 读 git；优先短 hash，没有 git 时回落到时间戳
if command -v git >/dev/null 2>&1 && git -C "$(dirname "$0")/../.." rev-parse --short HEAD >/dev/null 2>&1; then
    VERSION=$(git -C "$(dirname "$0")/../.." rev-parse --short HEAD)
else
    VERSION="release-$(date -u +%Y%m%d%H%M)"
fi

# 替换或追加（GNU sed 与 BSD sed 兼容）
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
OWNER_GROUP=$(stat -c '%u:%g' "$ENV_FILE" 2>/dev/null || stat -f '%u:%g' "$ENV_FILE" 2>/dev/null || true)
MODE=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || true)

if grep -q '^LUMEN_VERSION=' "$ENV_FILE"; then
    awk -v v="$VERSION" 'BEGIN{FS=OFS="="} /^LUMEN_VERSION=/{$2=v; print; next} {print}' "$ENV_FILE" > "$TMP"
else
    cp "$ENV_FILE" "$TMP"
    printf '\nLUMEN_VERSION=%s\n' "$VERSION" >> "$TMP"
fi

# 保留权限与属主。sudo/root 发布时 mv 会让 .env 变成 root 私有文件，导致 lumen 进程无法读取。
chmod --reference="$ENV_FILE" "$TMP" 2>/dev/null || chmod 600 "$TMP"
mv "$TMP" "$ENV_FILE"
if [[ -n "$OWNER_GROUP" ]]; then
    chown "$OWNER_GROUP" "$ENV_FILE" 2>/dev/null || true
fi
if [[ -n "$MODE" ]]; then
    chmod "$MODE" "$ENV_FILE" 2>/dev/null || true
fi

echo "LUMEN_VERSION=$VERSION written to $ENV_FILE"
