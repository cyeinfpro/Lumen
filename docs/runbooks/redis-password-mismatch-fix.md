# Redis 密码漂移修复（backup_preflight 报错）

## 症状

`update.sh` 跑 backup_preflight 阶段失败：

```
[backup ...] ERROR: redis ping failed before BGSAVE — check REDIS_URL/REDIS_PASSWORD vs lumen-redis requirepass
[ERROR] [backup_preflight] 备份失败 → abort（不允许无备份继续，4K 任务环境死规则）。
```

或更老版本的脚本会显示：

```
AUTH failed: WRONGPASS invalid username-password pair or user is disabled.
[backup ...] ERROR: redis BGSAVE did not complete in 60s
```

## 根因

`shared/.env` 里 **`REDIS_PASSWORD=` 那一行的字面值**，跟 **`REDIS_URL=` 里嵌入的密码**不一致。

- API/worker 用 `REDIS_URL` 连 redis（一直工作正常 → URL 嵌入密码 = 容器实际 requirepass）
- backup.sh 历史上单独读 `REDIS_PASSWORD`（漂移版本，跟容器不匹配）→ AUTH 失败

漂移可能由：手工编辑过 `.env` 只改了一边，或 `install.sh` 历史版本只更新一处字段。

## 一键修复（生产服务器执行）

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/fix-redis-password-mismatch.sh)"
```

干跑（只验证不修改）：

```bash
sudo DRY_RUN=1 bash -c "$(curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/fix-redis-password-mismatch.sh)"
```

不动容器、不动数据，只修 `.env`。脚本 = `scripts/fix-redis-password-mismatch.sh`，自包含、可读。

## 它做了什么

1. 前置检查（root + docker + awk + .env 存在）
2. 从 `REDIS_URL=redis://:<pwd>@redis:6379/0` 解析嵌入密码 P_URL
3. 跟当前 `REDIS_PASSWORD=` 比较：一致 → noop 退出
4. **用 P_URL ping 容器验证** —— 不通就退出报错（容器密码跟两边都不一致，不是单纯 .env 漂移）
5. 备份 `.env` 到 `.env.bak.<YYYYMMDD-HHMMSS>`
6. 用 `awk` 精确改写 `REDIS_PASSWORD=` 那一行，不动其他字段
7. 校验 + 恢复 600 权限

环境变量：`LUMEN_SHARED_ENV`（默认 `/root/Lumen/shared/.env`）、`REDIS_CONTAINER`（默认 `lumen-redis`）、`DRY_RUN=1`。

## 预期输出

成功：

```
PONG
OK: REDIS_PASSWORD 已对齐
```

失败 case 1（无法解析 REDIS_URL）：

```
ERR: 无法解析 REDIS_URL
```

→ 说明 `.env` 里 `REDIS_URL` 不是 `redis://:<pwd>@host:port/db` 格式。手工检查。

失败 case 2（URL 嵌入密码也连不上容器）：

```
AUTH failed: WRONGPASS ...
ERR: REDIS_URL 嵌入密码也连不上容器
```

→ 容器密码跟两边都不匹配（罕见）。需要进一步诊断，可能选项：

- `docker exec lumen-redis cat /tmp/lumen-redis.conf | grep ^requirepass` 看容器实际密码
- 或者 `docker compose up -d --force-recreate redis`（**警告：会清空 redis 内存数据，包括 arq 队列**）

## 跑完之后

重新触发 update：

```bash
# admin 网页一键更新按钮
# 或
bash /root/Lumen/current/scripts/lumenctl.sh update-lumen
```

`backup_preflight` 应该通过；后续 phase（fetch_release / set_image_tag / pull / migrate / switch / restart）会按完整 update 流程跑。

## 回滚

如需回滚 `.env` 改动：

```bash
sudo bash -c '
SHARED=/root/Lumen/shared/.env
latest_bak=$(ls -t "$SHARED".bak.* 2>/dev/null | head -n1)
[ -n "$latest_bak" ] && cp "$latest_bak" "$SHARED" && echo "已回滚到 $latest_bak"
'
```

## 相关代码

- `scripts/backup.sh` — `redis_cli` wrapper（v1.0.12 起 fail-fast 识别 AUTH 错误）
- `scripts/lib.sh:lumen_redis_resolve_password` — 优先从 `REDIS_URL` 解析密码（v1.0.12 起的真值来源）
- `deploy/redis/redis-entrypoint.sh` — 容器启动时把 `REDIS_PASSWORD` 写成 `requirepass`
- `scripts/install.sh:880-905` — 首次部署时随机生成密码并写入 `.env` 两处字段
