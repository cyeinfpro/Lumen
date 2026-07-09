# Lumen 版本号管理

Lumen 产品版本的唯一源是仓库根目录 `VERSION`，格式为不带 `v` 前缀的 SemVer：

```text
1.0.0
1.2.3
1.3.0-rc.1
```

## 同步版本

修改 `VERSION` 后执行：

```bash
python3 scripts/version.py sync
uv lock
python3 scripts/version.py check
```

`sync` 会把版本同步到：

- `pyproject.toml`
- `apps/api/pyproject.toml`
- `apps/worker/pyproject.toml`
- `apps/tgbot/pyproject.toml`
- `packages/core/pyproject.toml`
- `packages/core/lumen_core/__init__.py`
- `apps/web/package.json`
- `apps/web/package-lock.json`

`check` 还会检查 `uv.lock` 里的 workspace package 版本。若 lockfile 漂移，先运行 `uv lock`，否则 Dockerfile 里的 `uv sync --frozen` 会在 release 构建阶段失败。

## 发布 Tag

正式发布 tag 必须和 `VERSION` 一致：

```bash
VERSION=1.2.3
python3 scripts/version.py sync
uv lock
python3 scripts/version.py check
git tag v1.2.3
git push origin v1.2.3
```

CI 会检查：

```bash
python3 scripts/version.py check
python3 scripts/version.py assert-tag "$GITHUB_REF_NAME"
```

`Docker Release` 的 `workflow_dispatch.ref` 只能用于 branch/SHA 重建，不能用来制造 tag release 语义。正式稳定发布必须通过 push `v*` tag 触发。

## Docker Tag 规则

正式发布时使用：

```bash
python3 scripts/version.py docker-tags
```

输出示例：

```text
v1.2.3
v1.2
v1
latest
```

预发布版本（例如 `1.3.0-rc.1`）只输出精确 tag：

```text
v1.3.0-rc.1
```

预发布版本不能更新 `latest`、`v1`、`v1.3` 这类稳定指针。

每次 push 到 `main` 的预构建镜像应额外使用：

```text
sha-<short-sha>
main
```

`latest` 只能由正式版本 tag 更新，不能由普通 main push 覆盖。

## 一键更新 Channel

管理后台的 `/admin/update/check` 是更新目标的唯一来源。它读取 `runtime_settings.update.channel`，默认 `stable`：

| channel | 行为 |
|---|---|
| `stable` | 读取 GitHub latest release，忽略 prerelease。 |
| `main` | 目标镜像 tag 固定为 `main`，无法精确比较 SemVer，适合跟随主干环境。 |
| `pinned` | 由运维在触发更新时传 `target_tag`，API 和 `update.sh` 都会校验 tag 形态。 |
| `minor` / `major` | 预留给分支升级策略；未配置时按 stable 处理。 |

`stable` 解析或拉取失败时不能静默回退 `main`。需要 rolling 更新时显式设置 `LUMEN_UPDATE_CHANNEL=main`；临时允许 fallback 时显式设置 `LUMEN_UPDATE_FALLBACK_MAIN=1`。

相关设置：

- `update.check_ttl_sec`: 检查缓存 TTL，默认 1200 秒；设为 `0` 可关闭缓存。
- `update.allow_prerelease`: 是否把 prerelease 当成可更新目标，默认关闭。
- `force_redeploy`: 当前已是目标版本时仍重跑部署，适合配置修复或容器漂移修复。

运行时一致性检查：

```bash
python3 scripts/version.py print-runtime
```

输出会对齐 `VERSION`、`LUMEN_IMAGE_TAG` 和 `current/.lumen_release.json`，用于排查“UI 显示版本”和实际 release 不一致。
