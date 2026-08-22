# 一键更新排障

下文 `/opt/lumendata/backup` 是默认路径。若
`/opt/lumen/shared/.env` 配置了其他 `LUMEN_BACKUP_ROOT`，安装/更新脚本会把 path
unit 和 runner 日志路径渲染到实际目录；先用
`systemctl cat lumen-update.path lumen-update-runner.service` 核对后再替换命令中的路径。

## mode 或 channel 配置被拒绝

- `LUMEN_UPDATE_MODE` 未设置时默认 `fast`；显式空值、拼写错误或其他未知值返回 `64`。
- 合法 mode 为 `fast`、`standard`、`safe`、`full`；后两者等价于 `standard`。
- `pinned` 必须有完整 `vMAJOR.MINOR.PATCH` 当前 tag。
- `minor` 必须有 `vMAJOR.MINOR[.PATCH]` 锚点；`major` 必须有 `vMAJOR[.MINOR[.PATCH]]` 锚点。
- 缺失或非法锚点不会回退 `main`。只有显式 `channel=main` 才使用 rolling main。

修正 `/opt/lumen/shared/.env` 后重试；不要用空字符串试图恢复默认。

## 生产镜像引用被拒绝

生产 `docker-compose.yml` 只接受完整
`name@sha256:<64 lowercase hex>`。正式 release 的
`release-manifest.json` 的 legacy `images` 声明四个原有应用镜像，
`components.agent-runtime` 声明第五个应用镜像，并记录 Python、Node、Postgres、Redis
依赖镜像。手工部署可检查：

```bash
docker compose --env-file /opt/lumen/shared/.env config --images \
  | python3 /opt/lumen/current/scripts/check_immutable_images.py
```

出现 tag、`latest`、`main`、短 digest 或空输出都必须先修复，不能继续 `up`。

从四镜像安装首次升级后，如果 Runtime 尚未出现，保持两个 Agent 开关为 `0`，
再次执行同一 stable 更新。第一遍让旧 updater 安装新版脚本；第二遍由新版脚本
补齐密钥、绑定第五个 digest 并启动 Runtime。不要在第二遍完成前手工启动该
profile；兼容 fallback 是 API image，不是可用的 Runtime。

若启用 Agent 后 API `/readyz` 失败：

```bash
docker compose --profile agent-runtime ps agent-runtime
docker compose --profile agent-runtime logs --tail=120 agent-runtime worker api
docker compose exec -T agent-runtime node -e \
  "fetch('http://127.0.0.1:8090/readyz').then(async r=>{console.log(await r.text());process.exit(r.ok?0:1)})"
```

Runtime `/readyz` 不调用收费模型。检查两个 Agent 密钥、Runtime digest 和后端 DNS；
不要通过发布宿主端口绕过服务网络。

## has_update=false 但我知道有新版

先确认 channel：

```bash
curl -sS https://你的域名/api/admin/update/check?force=true
```

常见原因：

- `update.channel=main` 时没有 SemVer 比较，UI 只能显示滚动 main。
- `update.check_ttl_sec` 仍命中旧缓存；用 `force=true` 或临时设为 `0`。
- 目标是 prerelease，但 `update.allow_prerelease=0`。
- 当前运行 tag 已经等于 release tag，但需要重启部署；使用 `force_redeploy=true`。

## GitHub 不可达

`/admin/update/check` 应保持 200，并返回 `warning`。若没有缓存，UI 会显示 UNKNOWN。

处理顺序：

1. 在 Admin → 代理池里配置可访问 GitHub 的代理。
2. 设置 `update.proxy_name` 指定代理。
3. 点“重新检查”，确认 `cache.stale=false`。
4. 紧急情况下可在触发更新时传 `target_tag`，但 runner 只接受 `v*`（例如 `v1.2.3`）或 `main`。不要传字面量 `latest`；stable 通道应先把 GitHub latest release 解析成具体 `v*` tag。

## update_running 或锁卡住

先看 runner 状态：

```bash
systemctl status lumen-update-runner.service
tail -n 120 /opt/lumendata/backup/.update.log
```

确认没有真实更新进程后再清理：

```bash
redis-cli DEL lumen:update:lock
rm -f /opt/lumendata/backup/.update.running
```

如果是 path watcher 没启动：

```bash
systemctl enable --now lumen-update.path
systemctl status lumen-update.path
```

## 定时备份反复显示 maintenance lock

锁冲突时 `backup.sh` 返回 `75`，`lumen-backup.service` 每 60 秒重试，并且
`StartLimitIntervalSec=0` 不会在五次后静默停掉。检查：

```bash
systemctl status lumen-backup.service
tail -n 120 /opt/lumendata/backup/.backup.log
cat /opt/lumendata/backup/.backup.last-success.json
```

只有新的 `.backup-pair.<timestamp>.json` 和同步更新的
`.backup.last-success.json` 才证明补跑成功。不要把
`DEFERRED: maintenance lock held` 当成一次成功备份。

## Idempotency 命中看不到新触发

同一个 `Idempotency-Key` 24 小时内会返回第一次触发结果，不会启动第二个 update。前端正常会每次点击生成新 key；如果你手动 curl，请换一个 key。

## 预热拉取没有发生

`/admin/update/check` 返回 `has_update=true` 时会写 `/opt/lumendata/backup/.warm.trigger`，host 上的 `lumen-update-warm.path` 负责启动 pull。

```bash
systemctl enable --now lumen-update-warm.path
systemctl status lumen-update-warm.path
tail -n 80 /opt/lumendata/backup/.update.log
```

预热失败不影响正式更新；`pull_images` 阶段仍会现拉镜像。
