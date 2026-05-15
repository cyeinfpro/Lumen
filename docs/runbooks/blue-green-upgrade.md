# 蓝绿升级与回滚 Runbook

默认更新路径不启用蓝绿。启用前需要确认 nginx upstream 文件由 Lumen 管理，并且新老代码满足 expand-then-contract 迁移规则。

## 前置条件

```bash
python3 scripts/lint_alembic_breaking.py
bash -n scripts/update.sh scripts/lib.sh scripts/lumen-shift-traffic.sh
```

nginx upstream 默认写入：

```text
/etc/nginx/conf.d/lumen-upstream.conf
```

可用环境变量覆盖：

- `LUMEN_NGINX_UPSTREAM_CONF`
- `LUMEN_BLUE_UPSTREAM`，默认 `127.0.0.1:${API_BIND_PORT:-8000}`
- `LUMEN_GREEN_UPSTREAM`，默认 `127.0.0.1:${API_GREEN_BIND_PORT:-18001}`

## 启用蓝绿

在 update runner 环境中设置：

```bash
LUMEN_UPDATE_BLUE_GREEN=1
API_GREEN_BIND_PORT=18001
```

执行更新后应看到新增阶段：

```text
start_green
shift_traffic_50
shift_traffic_100
drain_blue
stop_blue
start_blue
shift_traffic_blue
stop_green
```

最终会把流量切回 canonical `api` 服务，`api-green` 只是临时影子容器，避免下一轮更新状态漂移。

## 手动切流

```bash
sudo LUMEN_BLUE_UPSTREAM=127.0.0.1:8000 \
  LUMEN_GREEN_UPSTREAM=127.0.0.1:18001 \
  bash scripts/lumen-shift-traffic.sh green 50

sudo LUMEN_BLUE_UPSTREAM=127.0.0.1:8000 \
  LUMEN_GREEN_UPSTREAM=127.0.0.1:18001 \
  bash scripts/lumen-shift-traffic.sh green 100
```

回到 blue：

```bash
sudo bash scripts/lumen-shift-traffic.sh blue 100
```

## 失败矩阵

| 阶段 | 现象 | 处理 |
|---|---|---|
| `start_green` | green healthz 不通 | 保持 blue 100，查看 `docker compose logs api-green`。 |
| `shift_traffic_50` | `nginx -t` 失败 | 脚本会恢复旧 upstream；修复 nginx conf 后重试。 |
| `shift_traffic_100` 后业务异常 | 立即 `scripts/lumen-shift-traffic.sh blue 100`，再 stop `api-green`。 |
| `start_blue` 失败 | green 仍在承载 100% 流量；修复 canonical `api` 后重跑 `docker compose up -d api`。 |
| `stop_green` 失败 | 不影响服务；手动 `docker compose -f docker-compose.yml -f docker-compose.bluegreen.yml stop api-green`。 |

## 压测验收

```bash
wrk -t4 -c100 -d120s https://你的域名/healthz
```

蓝绿切换期间非 200 响应应为 0。若仍有 502，先确认 nginx upstream 是否确实引用 `lumen_api`，再看 `proxy_next_upstream` 和 keepalive 配置。
