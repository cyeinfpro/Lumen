# 一键更新排障

下文 `/opt/lumendata/backup` 是默认路径。若
`/opt/lumen/shared/.env` 配置了其他 `LUMEN_BACKUP_ROOT`，安装/更新脚本会把 path
unit 和 runner 日志路径渲染到实际目录；先用
`systemctl cat lumen-update.path lumen-update-runner.service` 核对后再替换命令中的路径。

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
