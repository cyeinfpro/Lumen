# Lumen Docker 完整栈一次性切换与最佳更新体验方案

本文档描述把 Lumen 从”PostgreSQL / Redis 使用 Docker，API / Worker / Web / Bot 使用宿主机 uv/npm/systemd”一次性切换为”全部核心服务由 Docker Compose 管理”的实施方案，并吸收 Sub2API 的一键安装/更新经验，把安装和更新体验做到尽量稳定、可预期、少依赖宿主机。

状态：方案文档，尚未执行代码改造。

修订历史：

- 2026-05-03 初稿
- 2026-05-03 修订（落地前必修项）：补 §6.4.1 tag 注入流程、§8.5 stop_grace_period、§10.5 镜像源代理、§12.3.2 runner 的 `LUMEN_UPDATE_BUILD` 改造、§15.1/§15.2 按服务分别 chown、§17.0 named volume → bind mount 迁移 SOP、§21.1 .env 白名单 dry-run；§22 风险清单同步扩充。

## 1. 背景

当前仓库已有 `docker-compose.yml`，但它只管理：

- `postgres`
- `redis`

实际业务运行时仍由宿主机负责：

- `apps/api` 通过 `uv run uvicorn ...` 或 `lumen-api.service` 启动
- `apps/worker` 通过 `uv run python -m arq ...` 或 `lumen-worker.service` 启动
- `apps/web` 通过 `npm run build/start` 或 `lumen-web.service` 启动
- `apps/tgbot` 通过 Python 进程或 `lumen-tgbot.service` 启动

这种模式的问题是更新链路依赖宿主机状态：

- Python / uv / Node / npm 版本可能漂移
- `.venv`、`node_modules`、`.next` 的缓存和权限容易损坏
- systemd 用户、目录权限、端口占用、release symlink 都会参与更新过程
- 更新脚本同时负责拉代码、装依赖、构建、迁移、重启，失败点较多

切到 Docker 完整栈后，最佳更新流程应收敛为”拉取预构建镜像 + 显式迁移 + 原地重建容器”：

```bash
git pull 或 release clone
写入 LUMEN_IMAGE_TAG 到 shared/.env       # §6.4.1
bash scripts/backup.sh                    # §11.3.1 backup_preflight
docker compose pull
docker compose up -d --wait postgres redis
docker compose run --rm migrate           # 失败 fail-fast，不继续
docker compose up -d --wait api worker web tgbot
健康检查
```

没有可用预构建镜像时，才回退到服务器本地 `docker compose build`。宿主机长期只需要稳定运行 Docker 和 nginx。

### 1.1 从 Sub2API 借鉴什么

Sub2API 的体验好，核心不是脚本写得复杂，而是发布形态足够简单：

- 脚本安装：下载 GitHub Release 的预编译二进制，校验 checksum，安装到固定目录，再交给 systemd。
- 后台更新：管理后台检查 GitHub Release，后端下载新二进制，校验域名和 checksum，原子替换当前程序，返回 `need_restart=true`。
- 重启方式：应用进程自己退出，靠 systemd `Restart=always` 拉起新版本，不需要在 Web 后台里直接执行 sudo。
- Docker 更新：更简单，只需要 `docker compose pull && docker compose up -d`。
- 数据目录：推荐 local directory compose，但统一落在 `/opt/lumendata`，迁移服务器时打包 `/opt/lumen-deploy` 和 `/opt/lumendata`。
- 并发保护：系统更新、回滚、重启有全局锁，避免多个管理员同时点更新。

Lumen 不能照抄“替换一个二进制”，因为 Lumen 是 API / Worker / Web / Bot / DB migration 的多进程系统。能借鉴的是产品化原则：

- 不在用户服务器上做复杂编译，默认拉预构建镜像。
- 一键安装脚本只准备部署目录、`.env`、数据目录和 Compose 文件。
- 一键更新必须有清晰阶段、全局锁、失败可解释、可重试。
- 更新和重启要有边界，迁移必须是显式阶段。
- 回滚只承诺应用层回滚，数据库回滚必须依赖备份或兼容迁移。
- Docker 场景不要让普通 API 容器直接拥有宿主机 Docker 权限。

### 1.2 Lumen 的最优体验目标

最终用户视角应该是：

```bash
curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/refs/heads/main/scripts/install.sh | bash
```

安装脚本完成后给出：

- Web 地址
- 管理员账号
- Provider 配置入口
- 状态检查命令
- 日志命令
- 备份位置

日常更新只有一个入口：

```bash
bash scripts/lumenctl.sh update-lumen
```

管理后台也应提供同样能力：

```text
检查更新 -> 显示当前版本/最新版本/变更摘要 -> 一键更新 -> 阶段进度 -> 健康检查 -> 完成
```

后台一键更新不应该要求用户登录服务器，也不应该暴露 Docker socket 给普通 API 容器。推荐由受限 updater 机制执行宿主机脚本，详见“后台一键更新设计”。

## 2. 切换目标

### 2.1 目标

一次性完成以下变化：

- `docker-compose.yml` 成为完整运行栈的唯一默认入口
- API / Worker / Web / Telegram Bot 全部容器化
- 数据库迁移通过一次性 `migrate` 容器执行
- 管理员初始化通过一次性 `bootstrap` 容器执行
- 安装脚本不再依赖宿主机 `uv / node / npm`
- 更新脚本默认不再执行宿主机 `uv sync / npm ci / npm run build`
- 更新脚本默认使用预构建镜像：`docker compose pull`
- 无预构建镜像或开发模式才允许显式 `docker compose build`
- 更新脚本不再重启 `lumen-api / lumen-worker / lumen-web / lumen-tgbot` systemd 服务
- 健康检查改为 HTTP + Compose service 状态检查
- 提供 named volume → `/opt/lumendata` bind mount 的显式迁移 SOP（§17.0），不在切换脚本里隐式搬数据
- 图片和备份继续保留在 `/opt/lumendata`，PostgreSQL / Redis 数据目录也对齐到 `/opt/lumendata/{postgres,redis}`
- 安装/更新输出稳定的阶段日志，管理后台可解析并展示进度
- 更新、回滚、重启共享全局锁，避免并发操作
- 支持脚本更新和管理后台一键更新两种入口
- 支持应用层回滚到上一镜像/上一 release

### 2.2 非目标

本次不做以下事情：

- 不把 PostgreSQL / Redis 换成外部托管服务
- 不改业务 API、前端页面或 Worker 任务逻辑
- 不改变 nginx 对外域名结构
- 不改变 Provider Pool 数据结构
- 不引入 Kubernetes、Swarm 或复杂编排
- 不把 API 容器直接授予无限制 Docker socket 权限
- 不承诺自动回滚数据库 schema；数据库回滚必须依赖备份或人工补偿 SQL

## 3. 体验原则

### 3.1 安装体验

安装脚本应做到：

- 可从 GitHub raw 一行命令启动
- 自动检测 Docker / Compose / OpenSSL
- 自动生成强密钥
- 自动创建 `.env`
- 自动创建数据目录
- 自动拉取镜像
- 自动启动 Postgres / Redis / API / Worker / Web
- 自动执行数据库迁移
- 自动创建或提升首个管理员
- 输出明确的访问地址、日志命令、更新命令、备份路径

安装脚本不应该：

- 要求用户手动安装 Python / Node / uv / npm
- 默认在用户服务器上编译前端和 Python 依赖
- 把密钥打印到长日志里，除非是首次生成的管理员密码且明确提示保存
- 在失败时留下难以判断的半启动状态

### 3.2 更新体验

更新脚本应做到：

- 先检查版本，再决定是否需要更新
- 默认 `pull` 预构建镜像
- 显式执行迁移
- 重建业务容器
- 执行健康检查
- 失败时保留旧容器或给出明确恢复命令
- 输出结构化阶段日志，管理后台可以实时显示

更新脚本不应该：

- 默认在服务器上 `npm ci` / `npm run build`
- 默认在服务器上 `uv sync`
- 让多个更新任务并发执行
- 静默修改用户 `.env` 中的敏感配置
- 在数据库迁移失败后继续切换应用

### 3.3 后台一键更新体验

管理后台应提供：

- 当前版本
- 最新版本
- 是否有更新
- 镜像 tag / git sha
- 变更摘要
- “开始更新”按钮
- 阶段进度：
  - `check`
  - `backup_preflight`
  - `pull_images`
  - `migrate_db`
  - `restart_services`
  - `health_check`
  - `cleanup`
- 失败原因和建议命令
- 更新完成后的版本确认

后台更新不应把执行逻辑塞进 Web API 进程。API 只负责申请更新、读取日志和展示状态；真正执行应由受限 updater 机制完成。

## 4. 目标架构

```text
Browser / Admin / Share
        |
        v
nginx / domain
        |
        v
web:3000  (Next.js)
        |
        | /api/* and /events rewrites
        v
api:8000  (FastAPI)
        |
        +--> postgres:5432
        +--> redis:6379
        +--> /opt/lumendata/storage

worker  (arq)
        |
        +--> postgres:5432
        +--> redis:6379
        +--> upstream providers
        +--> /opt/lumendata/storage

tgbot  (optional)
        |
        +--> api:8000
        +--> redis:6379
        +--> api.telegram.org or proxy

migrate  (one-shot)
        |
        +--> postgres:5432

bootstrap  (one-shot)
        |
        +--> postgres:5432
```

## 5. 服务清单

| Compose service | 容器名 | 作用 | 默认启动 |
| --- | --- | --- | --- |
| `postgres` | `lumen-pg` | PostgreSQL 16 | 是 |
| `redis` | `lumen-redis` | Redis 7 | 是 |
| `api` | `lumen-api` | FastAPI REST / SSE | 是 |
| `worker` | `lumen-worker` | arq 后台任务 | 是 |
| `web` | `lumen-web` | Next.js Web | 是 |
| `tgbot` | `lumen-tgbot` | Telegram Bot | 条件启动或 profile |
| `migrate` | 临时容器 | Alembic 迁移 | 手动 run |
| `bootstrap` | 临时容器 | 创建/提升管理员 | 手动 run |

建议 `tgbot` 使用 Compose profile，避免没有 bot token 时影响主栈健康：

```bash
docker compose --profile tgbot up -d tgbot
```

主流程默认启动：

```bash
docker compose up -d postgres redis api worker web
```

## 6. 文件改动设计

### 6.1 新增文件

```text
.dockerignore
Dockerfile.python
apps/web/Dockerfile
deploy/docker/README.md
deploy/docker/docker-compose.local.yml
```

可选新增：

```text
deploy/docker/update.sh
deploy/docker/install.sh
deploy/docker/lumen-updater.service
deploy/docker/lumen-updater.path
```

如果希望保持脚本入口集中，可以不新增 `deploy/docker/*.sh`，而是直接改 `scripts/install.sh` 和 `scripts/update.sh`。

### 6.2 修改文件

```text
docker-compose.yml
.env.example
README.md
deploy/README.md
scripts/install.sh
scripts/update.sh
scripts/uninstall.sh
scripts/lib.sh
scripts/lumenctl.sh
tests/test_lumenctl_scripts.py
.github/workflows/docker-release.yml
deploy/systemd/lumen-update-runner.service
```

`lumen-update-runner.service` 当前写死 `Environment=LUMEN_UPDATE_BUILD=1`，切换后必须改为 `LUMEN_UPDATE_BUILD=0`，否则后台一键更新仍会触发宿主机 build，与本方案"减少宿主依赖"目标相悖。详见 §12.3。

不需要修改：

```text
apps/api/app/*
apps/worker/app/*
apps/web/src/*
apps/tgbot/app/*
packages/core/*
```

除非构建过程中发现代码对宿主路径或 `localhost` 有硬编码。

待清理（与本切换无直接耦合，但应在切换 PR 之外评估去留）：

```text
image-job/        独立的 app.py + requirements.txt，不在 monorepo workspace 内
var/              开发态产物目录；生产真实路径应是 /opt/lumendata/storage
```

`deploy/redis/redis-entrypoint.sh` 仍由 redis 服务挂载（见 §8.4 / §10.2.1），随 release 同步分发，不能在改造时漏掉。

### 6.3 发布物设计

Sub2API 的脚本更新之所以稳，是因为它更新的是 GitHub Release 中的预构建二进制。Lumen 应采用等价的 Docker 发布物：

```text
ghcr.io/cyeinfpro/lumen-api:<version-or-sha>
ghcr.io/cyeinfpro/lumen-worker:<version-or-sha>
ghcr.io/cyeinfpro/lumen-web:<version-or-sha>
ghcr.io/cyeinfpro/lumen-tgbot:<version-or-sha>
```

推荐 tag：

```text
latest                 最新稳定版
main                   main 分支最新构建
sha-<git-sha>          精确 commit
v<semver>              正式版本
v<major>.<minor>       某个 minor 的最新 patch，可选
v<major>               某个 major 的最新 minor/patch，可选
```

安装脚本默认写入：

```env
LUMEN_IMAGE_REGISTRY=ghcr.io/cyeinfpro
LUMEN_IMAGE_TAG=latest
```

更新脚本默认执行：

```bash
docker compose pull
docker compose run --rm migrate
docker compose up -d --wait api worker web
```

只有以下情况才本地 build：

- 开发者明确设置 `LUMEN_UPDATE_BUILD=1`
- 当前不是发布部署，而是源码开发部署
- 镜像仓库不可达，且用户确认允许本地构建兜底

### 6.4 版本号机制

必须加版本号机制。只依赖 `latest` 会带来几个问题：

- 不知道当前到底跑的是哪个 commit
- 回滚无法准确指定旧版本
- 管理后台无法可靠判断“是否有更新”
- 用户反馈问题时无法定位镜像内容
- `latest` 被覆盖后，历史状态不可复现

当前已落地的基础设施：

```text
VERSION                                  单一产品版本源，当前为 1.0.0
scripts/version.py                       版本同步/检查/发布 tag 工具
docs/versioning.md                       版本管理说明
.github/workflows/ci.yml                 CI 中检查版本一致性
.github/workflows/release-version.yml    tag 发布前检查 tag 与 VERSION 一致
```

`VERSION` 使用不带 `v` 前缀的 SemVer。正式 Git tag 和 Docker tag 再加 `v` 前缀，例如 `VERSION=1.0.0` 对应 `v1.0.0`。

已同步的版本目标：

```text
pyproject.toml
apps/api/pyproject.toml
apps/worker/pyproject.toml
apps/tgbot/pyproject.toml
packages/core/pyproject.toml
packages/core/lumen_core/__init__.py
apps/web/package.json
apps/web/package-lock.json
uv.lock
```

日常改版本：

```bash
printf '1.2.3\n' > VERSION
python3 scripts/version.py sync
python3 scripts/version.py check
uv lock
```

正式发版前：

```bash
python3 scripts/version.py assert-tag v1.2.3
python3 scripts/version.py docker-tags
```

推荐版本分层：

| 类型 | 示例 | 触发时机 | 用途 |
| --- | --- | --- | --- |
| commit 版本 | `sha-abcdef1` | 每次 push 到 `main` | 可审计、可精确回滚 |
| 分支版本 | `main` | 每次 push 到 `main` | 测试/预览部署 |
| 正式版本 | `v1.2.3` | 推送 Git tag `v1.2.3` | 生产默认更新 |
| 稳定指针 | `latest` | 正式版本发布成功后 | 新用户默认安装 |
| minor 指针 | `v1.2` | `v1.2.x` 发布成功后 | 锁定 minor 自动吃 patch |
| major 指针 | `v1` | `v1.x.y` 发布成功后 | 锁定 major 自动吃 minor/patch |

生产安装默认使用：

```env
LUMEN_UPDATE_CHANNEL=stable
LUMEN_IMAGE_TAG=latest
```

注意：`latest` 只在第一个正式 `vX.Y.Z` tag 推送成功后才会被 CI 创建（见 §6.5）。在首个 release 之前，如果用户从主分支安装，安装脚本必须探测 GHCR 上是否存在 `latest`：不存在则退化为 `LUMEN_IMAGE_TAG=main`，并在 `.env` 写入显式注释提示用户在 v1.0.0 发布后改回 `latest`。否则首发用户会卡在 `manifest unknown`。

高级用户可以选择：

```env
# 每次 main 更新都可拉到，适合测试服
LUMEN_UPDATE_CHANNEL=main
LUMEN_IMAGE_TAG=main

# 锁定精确版本，适合保守生产
LUMEN_UPDATE_CHANNEL=pinned
LUMEN_IMAGE_TAG=v1.2.3

# 锁定 minor，只吃 patch
LUMEN_UPDATE_CHANNEL=minor
LUMEN_IMAGE_TAG=v1.2
```

#### 6.4.1 tag 注入流程

`docker-compose.yml` 中所有应用服务通过 `image: ghcr.io/cyeinfpro/lumen-<svc>:${LUMEN_IMAGE_TAG}` 引用镜像。`LUMEN_IMAGE_TAG` 何时、由谁写入 `.env` 必须明确，否则 `docker compose pull` 会一直拉到旧 tag：

| 入口 | tag 来源 | 写入时机 |
| --- | --- | --- |
| 一键安装脚本 | 探测 GHCR 上的 `latest`/`main`，或用户参数 `--image-tag=vX.Y.Z` | `.env` 创建时 |
| `scripts/update.sh`（命令行） | 读 `LUMEN_UPDATE_CHANNEL` + 查 GitHub Releases API（pinned 时取用户配置） | `clone new release` 之后、`docker compose pull` 之前；写入 `shared/.env` 中的 `LUMEN_IMAGE_TAG` 单行 |
| 后台一键更新 | API 调 `release/check` 已确定的目标版本作为入参 | 写入 `/opt/lumendata/backup/.update.env`，由 `lumen-update-runner.service` 的 `EnvironmentFile=` 加载 |

写入约束：

- 只允许覆盖 `LUMEN_IMAGE_TAG` 一行，不允许碰其他 `.env` 字段
- 写入后必须 `grep -E '^LUMEN_IMAGE_TAG=' .env` 校验唯一存在
- 失败回滚（§18.1）也走同一入口：把 `LUMEN_IMAGE_TAG` 改回上一已知好版本，再 `pull && up -d`

### 6.5 预构建触发机制

预构建应在 CI 中完成，而不是在用户服务器上完成。

推荐 GitHub Actions 触发规则：

```yaml
on:
  push:
    branches:
      - main
    tags:
      - "v*"
  workflow_dispatch:
```

行为：

```text
push main:
  build api/worker/web/tgbot
  push sha-<short-sha>
  push main
  不更新 latest
  不创建 GitHub Release

push tag vX.Y.Z:
  build api/worker/web/tgbot
  push sha-<short-sha>
  push vX.Y.Z
  push vX.Y
  push vX
  push latest
  创建/更新 GitHub Release
  上传 release metadata

workflow_dispatch:
  允许手动重建某个 sha 或 tag
```

### 6.6 CI 构建质量门禁

正式 tag 发布前必须通过：

```text
Python tests
Web type-check
Web build
Docker build
Compose config
最小冒烟：api / worker / web 镜像可启动
```

如果任一失败，不推 `latest`。`latest` 只能指向完整通过门禁的正式版本。

建议镜像带 OCI labels：

```text
org.opencontainers.image.version=v1.2.3
org.opencontainers.image.revision=<git-sha>
org.opencontainers.image.source=https://github.com/cyeinfpro/Lumen
org.opencontainers.image.created=<timestamp>
```

容器启动时也应暴露版本：

- API `/healthz` 或 `/version` 返回 `LUMEN_VERSION`
- Web footer/admin 显示当前版本
- Worker 日志启动时打印版本

### 6.7 Release 元数据

为支持管理后台“检查更新”，每次发布应生成一个轻量元数据文件：

```json
{
  "version": "v1.2.3",
  "sha": "abcdef123456",
  "created_at": "2026-05-03T12:00:00Z",
  "images": {
    "api": "ghcr.io/cyeinfpro/lumen-api:sha-abcdef1",
    "worker": "ghcr.io/cyeinfpro/lumen-worker:sha-abcdef1",
    "web": "ghcr.io/cyeinfpro/lumen-web:sha-abcdef1",
    "tgbot": "ghcr.io/cyeinfpro/lumen-tgbot:sha-abcdef1"
  },
  "alembic_heads": ["0015_workflow_runs_apparel_showcase"],
  "notes_url": "https://github.com/cyeinfpro/Lumen/releases/tag/v1.2.3"
}
```

可以放在 GitHub Release asset，也可以由 API 直接查 GitHub Releases。Sub2API 的做法是直接查 GitHub Releases API 并缓存结果，Lumen 可以先复用这条路。

## 7. Docker 镜像设计

### 7.1 Python 镜像

使用单个 `Dockerfile.python`，通过 build arg / `command` 区分运行目标（API / Worker / Bot / migrate / bootstrap 共用同一镜像）。

#### 7.1.1 单镜像 vs 多镜像取舍

- 选项 A（采纳）：单镜像 + 不同 `working_dir` 与 `command`
- 选项 B：API / Worker / Bot 三个独立 Dockerfile

选 A 的原因：API、Worker、Bot 都通过 `packages/core/lumen_core` 共享 Provider Pool / models / schemas 等代码，在 monorepo workspace 下分镜像意味着每个镜像都要单独 `uv sync` 整套依赖图，cache 命中率反而更差。代价是镜像略大（约多 30~50MB 的 ssh / sshpass / libpq5）和攻击面略增。本阶段不优化；当 API 镜像被独立流量分级时再切 B。

#### 7.1.2 基础镜像与系统依赖

```dockerfile
FROM python:3.12-slim AS base
```

runtime 必须有：

- `curl`
- `ca-certificates`
- `openssh-client`
- `sshpass`
- `libpq5`

builder 额外有（不进 runtime）：

- `build-essential`
- `libpq-dev`

安装 `openssh-client / sshpass` 的原因：Provider Pool 支持 `ssh` 类型代理，相关解析与子进程拉起逻辑位于 `packages/core/lumen_core/providers.py`，**API 与 Worker 都通过 `lumen_core` 共享这段代码**，两个角色都需要在 `$PATH` 里能找到 `ssh`/`sshpass`。Bot 不调用 Provider Pool，但用同一镜像，多带这两个二进制可接受。

#### 7.1.3 builder / runtime 多阶段

```text
builder:
  安装 uv
  拷贝 pyproject.toml / uv.lock / 所有 workspace 子项目的 pyproject.toml
  uv sync --frozen --no-dev --all-packages   # .venv 落在 /app/.venv
  拷贝源码到 /app

runtime:
  从 builder 拷贝 /app/.venv 与 /app（包含 apps/* 与 packages/*）
  使用非 root 用户 lumen (uid=10001, gid=10001)
  ENV PATH="/app/.venv/bin:${PATH}"
  ENV PYTHONPATH=""    # 不要额外注入；走 .venv 自身 site-packages
```

#### 7.1.4 工作目录与 import 关系

uv workspace 模式下，`uv sync --all-packages` 会把 `packages/core` 以可编辑方式安装进 `/app/.venv/lib/python3.12/site-packages/lumen_core`（实际是 `.pth` 指向 `/app/packages/core/lumen_core`）。因此：

- 容器统一根目录：`/app`
- API 容器 `working_dir: /app/apps/api`，`command: uvicorn app.main:app ...`，`import lumen_core.*` 来自 `.venv`
- Worker 容器 `working_dir: /app/apps/worker`，`command: python -m arq app.main.WorkerSettings`
- Bot 容器 `working_dir: /app/apps/tgbot`，`command: python -m app.main`
- migrate / bootstrap 一次性容器 `working_dir: /app/apps/api`

所有容器 `python` 解析到 `/app/.venv/bin/python`，通过 `PATH` 优先级保证；不要再在 image 内显式 `cd` 然后 `pip install -e .`。

容器内命令：

```bash
# API
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Worker
python -m arq app.main.WorkerSettings

# Telegram Bot
python -m app.main

# Migrate
alembic upgrade head

# Bootstrap
python -m app.scripts.bootstrap <email> --role admin --password <password>
```

由于 API、Worker、Bot 分别位于不同子目录，Compose 中应设置不同 `working_dir`：

```yaml
api:
  working_dir: /app/apps/api

worker:
  working_dir: /app/apps/worker

tgbot:
  working_dir: /app/apps/tgbot

migrate:
  working_dir: /app/apps/api
```

### 7.2 Web 镜像

`apps/web/Dockerfile` 使用多阶段构建。

建议基础镜像：

```dockerfile
node:22-alpine
```

也可以用 `node:20-alpine`，但当前前端为 Next.js 16 / React 19，建议使用较新的 LTS。

构建阶段：

```text
deps:
  npm ci

builder:
  npm run build

runner:
  npm run start
```

建议启用 Next.js standalone 输出，减少运行镜像体积：

```ts
// apps/web/next.config.ts
const nextConfig: NextConfig = {
  output: "standalone",
  ...
}
```

如果不启用 standalone，运行镜像需要包含：

- `.next`
- `public`
- `package.json`
- `node_modules`

运行命令：

```bash
npm run start
```

Web 容器必须设置：

```env
LUMEN_BACKEND_URL=http://api:8000
NEXT_PUBLIC_API_BASE=/api
```

`LUMEN_BACKEND_URL` 是 Next.js 服务端 rewrite 使用的内部地址，不暴露给浏览器。

## 8. Compose 设计

### 8.1 网络

保留一个内部网络：

```yaml
networks:
  lumen_backend:
    driver: bridge
```

所有服务加入该网络。

服务之间使用 Compose service name 通信：

- `postgres`
- `redis`
- `api`
- `web`

### 8.2 数据卷

默认使用宿主机 bind mount，所有持久数据统一放在 `/opt/lumendata`：

```yaml
services:
  postgres:
    volumes:
      - ${LUMEN_DB_ROOT:-/opt/lumendata}/postgres:/var/lib/postgresql/data
  redis:
    volumes:
      - ${LUMEN_DB_ROOT:-/opt/lumendata}/redis:/data
  api:
    volumes:
      - ${LUMEN_DATA_ROOT:-/opt/lumendata}/storage:/opt/lumendata/storage
      - ${LUMEN_DATA_ROOT:-/opt/lumendata}/backup:/opt/lumendata/backup
  worker:
    volumes:
      - ${LUMEN_DATA_ROOT:-/opt/lumendata}/storage:/opt/lumendata/storage
```

目录结构：

```text
/opt/lumendata/
  postgres/
  redis/
  storage/
  backup/
```

Docker named volumes 只作为兼容模式保留，例如从旧部署平滑升级时先不移动 PG/Redis 数据。长期推荐迁移到 `/opt/lumendata/postgres` 和 `/opt/lumendata/redis`，这样备份、迁移和排查都更直观。

### 8.3 端口暴露

建议：

```yaml
api:
  ports:
    - "${API_BIND_HOST:-127.0.0.1}:8000:8000"

web:
  ports:
    - "${WEB_BIND_HOST:-127.0.0.1}:3000:3000"
```

解释：

- API 默认只暴露给宿主机本地 nginx 或本机调试
- Web **默认也只绑 127.0.0.1**，外部流量必须经 nginx（与 §14 拓扑一致）。当前 `lumen-web.service` 的行为就是 127.0.0.1:3000，切容器后必须保持，否则在 frps 隧道 / 公网服务器场景会出现绕过 TLS 直连 3000 的洞
- 仅当部署拓扑显式没有外层反代时（极少见），用户才设置 `WEB_BIND_HOST=0.0.0.0`
- PostgreSQL / Redis 继续默认绑定 `127.0.0.1`

### 8.4 健康检查

`postgres`：

```yaml
healthcheck:
  test: ["CMD", "pg_isready", "-U", "${DB_USER}", "-d", "${DB_NAME}"]
```

`redis`：

```yaml
healthcheck:
  test: ["CMD-SHELL", "REDISCLI_AUTH=\"$${REDIS_PASSWORD}\" redis-cli --no-auth-warning ping | grep -q PONG"]
```

`api`：

```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:8000/healthz >/dev/null || exit 1"]
  interval: 10s
  timeout: 5s
  retries: 6
  start_period: 30s
```

`web`：

```yaml
healthcheck:
  test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:3000/ >/dev/null || exit 1"]
  interval: 10s
  timeout: 5s
  retries: 6
  start_period: 30s
```

不要使用 `python - <<'PY' ... PY` 这种 heredoc 形式：YAML 解析能过，但 shell escape 边界很容易踩坑（特别是被外层脚本再次模板化时）。`curl` 已经是 §7.1 镜像必装组件，单行直观且失败语义明确。

`redis` 必须保留自定义 entrypoint 挂载（继承当前 `docker-compose.yml`）：

```yaml
redis:
  entrypoint: ["/bin/sh", "/usr/local/bin/lumen-redis-entrypoint"]
  volumes:
    - ${LUMEN_DATA_ROOT:-/opt/lumendata}/redis:/data
    - ./deploy/redis/redis-entrypoint.sh:/usr/local/bin/lumen-redis-entrypoint:ro
```

`deploy/redis/redis-entrypoint.sh` 负责启动期把 `REDIS_PASSWORD` 注入 redis 配置，丢掉这个挂载会让 redis 回到无密码状态。

`worker` 短期内只能依赖容器进程存活；不要把 "running" 等同于 "healthy"。建议加一条轻量 redis ping 作为 healthcheck，至少能识别"redis URL 错配但容器还在 retry 死循环"的情况：

```yaml
worker:
  healthcheck:
    test: ["CMD-SHELL", "redis-cli -u \"$$REDIS_URL\" --no-auth-warning ping | grep -q PONG"]
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 20s
```

注意：`redis-cli` 不在 `python:3.12-slim` 默认包内，runtime 阶段需追加 `redis-tools` 或者写一个 `python -c` 一行版（详见 §13.3）。

### 8.5 停机宽限期

Docker 默认 `stop_grace_period` 是 10s——`docker compose down` 或滚动重启时，10s 后 SIGKILL。Lumen 的 4K 长任务在 worker 内最长可能跑 1500s（超过则 arq 1800s timeout 兜底），任何短宽限期都会在更新窗口内打断进行中的 4K 任务，造成上游计费但产物丢失。

每个服务的宽限期：

| 服务 | `stop_grace_period` | 理由 |
| --- | --- | --- |
| `worker` | `1830s` | 与 4K timeout envelope 对齐：arq 1800s + 30s buffer。SIGTERM 后 arq 不会接新任务，进行中的 task 自然完成 |
| `api` | `60s` | FastAPI uvicorn graceful shutdown 默认 30s，留余量；SSE 连接需要让客户端收到 close |
| `tgbot` | `30s` | polling/webhook 拆解；管理后台"重启 bot"靠 sys.exit(0) clean exit |
| `web` | `30s` | Next.js 自身 shutdown 时间短 |
| `postgres` | `60s` | 让 PG 完成 checkpoint，避免 crash recovery |
| `redis` | `30s` | RDB/AOF 落盘 |
| `migrate` / `bootstrap` | 默认 | 一次性，不参与 `compose up/down` 滚动 |

`docker-compose.yml` 中显式写出，不要靠默认：

```yaml
worker:
  stop_grace_period: 1830s
  stop_signal: SIGTERM
```

§24 "推荐最终运维命令" 中的 `docker compose up -d --force-recreate` 在没有这套宽限期时会成为 4K 任务杀手——这是必修项。

## 9. 环境变量调整

### 9.1 当前问题

现有 `.env.example` 默认使用：

```env
DATABASE_URL=postgresql+asyncpg://...@localhost:5432/...
REDIS_URL=redis://...@localhost:6379/0
LUMEN_BACKEND_URL=http://127.0.0.1:8000
LUMEN_API_BASE=http://127.0.0.1:8000
```

这些地址适合宿主机进程，不适合容器。

容器内 `localhost` 指向容器自己，不是 Postgres / Redis / API。

### 9.2 Docker 默认值

切换后 `.env.example` 应改为：

```env
DB_USER=lumen_app
DB_PASSWORD=replace-with-strong-db-password
DB_NAME=lumen_app

DATABASE_URL=postgresql+asyncpg://lumen_app:replace-with-strong-db-password@postgres:5432/lumen_app
REDIS_PASSWORD=replace-with-strong-redis-password
REDIS_URL=redis://:replace-with-strong-redis-password@redis:6379/0

POSTGRES_BIND_HOST=127.0.0.1
REDIS_BIND_HOST=127.0.0.1
API_BIND_HOST=127.0.0.1
# Web 默认只绑回环；外部流量必须经 nginx。仅当部署没有外层反代时才改 0.0.0.0。
WEB_BIND_HOST=127.0.0.1

# 镜像源（§10.5）；国内可改自托管/镜像 mirror
LUMEN_IMAGE_REGISTRY=ghcr.io/cyeinfpro
# 安装时探测 GHCR：v1.0.0 发布后用 latest，发布前 fallback 到 main（§6.4）
LUMEN_IMAGE_TAG=latest

LUMEN_BACKEND_URL=http://api:8000
LUMEN_API_BASE=http://api:8000
NEXT_PUBLIC_API_BASE=/api

STORAGE_ROOT=/opt/lumendata/storage
BACKUP_ROOT=/opt/lumendata/backup
```

### 9.3 兼容本地宿主机命令

如果仍需要偶尔从宿主机执行 Alembic 或 Python 脚本，可以临时覆盖：

```bash
DATABASE_URL='postgresql+asyncpg://...@127.0.0.1:5432/...' \
REDIS_URL='redis://:...@127.0.0.1:6379/0' \
uv run ...
```

但正式运维脚本不应再依赖宿主机执行 Python。

## 10. 安装流程改造

### 10.1 旧安装流程

旧流程大致为：

```text
检查 Docker / uv / Node / Python
生成 .env
docker compose pull
uv sync --frozen
npm ci
docker compose up -d postgres redis
uv run alembic upgrade head
uv run bootstrap
npm run build
后台启动 API / Worker / Web
```

### 10.2 新安装流程

新流程应改为：

```text
检查 Docker / Compose / OpenSSL
生成 .env
创建 /opt/lumendata/storage 和 /opt/lumendata/backup
下载/写入 docker-compose.yml
docker compose pull
docker compose up -d postgres redis
docker compose run --rm migrate
docker compose run --rm bootstrap
docker compose up -d api worker web
可选 docker compose --profile tgbot up -d tgbot
健康检查
```

对应命令：

```bash
docker compose pull
docker compose up -d --wait postgres redis
docker compose run --rm migrate
docker compose run --rm bootstrap
docker compose up -d --wait api worker web
```

如果用户是源码开发部署，或显式设置 `LUMEN_INSTALL_BUILD=1`，安装脚本才执行：

```bash
docker compose build
```

这点要向 Sub2API Docker 部署学习：默认让用户拉已发布镜像，不让用户服务器承担构建工作。

### 10.2.1 轻量安装器

一键安装脚本应尽量像 Sub2API 的 `docker-deploy.sh` 一样轻：

```text
下载 compose 模板
下载 .env.example
生成密钥
创建数据目录
拉镜像
启动服务
输出下一步
```

推荐部署目录：

```text
/opt/lumen-deploy/
  docker-compose.yml
  .env
  .env.example
  releases/              可选，保留管理后台版本记录
  logs/                  可选，保存 install/update 日志

/opt/lumendata/
  storage/
  backup/
  postgres/
  redis/
```

生产体验最优的默认方案是把 PostgreSQL / Redis 数据也放进 `/opt/lumendata` 下的明确目录，便于整机迁移和人工备份：

```text
/opt/lumendata/postgres
/opt/lumendata/redis
```

两种 Compose 都可以保留：

| 文件 | 数据位置 | 适用 |
| --- | --- | --- |
| `docker-compose.yml` | `/opt/lumendata` 本地目录 | 默认推荐 |
| `docker-compose.volume.yml` | Docker named volumes | 兼容旧部署或简单试用 |

安装脚本默认使用 `/opt/lumendata` 本地目录版本，除非用户设置 `LUMEN_STORAGE_MODE=volume`。

### 10.3 管理员密码传递

不要把管理员密码写入 `.env`。

安装脚本读取密码后，仅传给一次性容器：

```bash
LUMEN_ADMIN_PASSWORD="$ADMIN_PWD" \
docker compose run --rm bootstrap \
  python -m app.scripts.bootstrap "$ADMIN_EMAIL" --role admin --password "$LUMEN_ADMIN_PASSWORD"
```

更好的做法是在 `bootstrap` service command 中读取 `LUMEN_ADMIN_EMAIL` 和 `LUMEN_ADMIN_PASSWORD`。

### 10.4 systemd 处理

一次性切换时，安装脚本应在 Docker 栈启动成功后提示禁用旧服务：

```bash
sudo systemctl disable --now lumen-api lumen-worker lumen-web lumen-tgbot
```

如果要完全自动化，可以在 Linux 且存在这些 unit 时自动停用，但建议第一次切换时先提示确认。

### 10.5 镜像源与代理

`ghcr.io` 在国内访问经常超时（生产部署 lumen.infpro.cn 通过 frps 隧道架构，家用机器拉镜像更慢）。一键安装脚本必须考虑：

#### 10.5.1 Docker daemon 级代理

在装好 Docker 之后、`docker compose pull` 之前，安装脚本提供配置入口：

```bash
# 用户在交互模式下被询问；或通过 LUMEN_HTTP_PROXY=... 环境变量预先注入
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/http-proxy.conf >/dev/null <<EOF
[Service]
Environment="HTTP_PROXY=${LUMEN_HTTP_PROXY}"
Environment="HTTPS_PROXY=${LUMEN_HTTP_PROXY}"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker
```

注意：这一步会重启 Docker daemon，已运行的容器都会重启。在已有部署上做切换时必须放在 §17.0 数据迁移之前完成，且要预告用户。

#### 10.5.2 镜像 registry mirror

`docker-compose.yml` 中所有应用镜像使用 `${LUMEN_IMAGE_REGISTRY:-ghcr.io/cyeinfpro}` 前缀：

```yaml
api:
  image: ${LUMEN_IMAGE_REGISTRY:-ghcr.io/cyeinfpro}/lumen-api:${LUMEN_IMAGE_TAG}
```

允许用户：

- 改用阿里云 ACR / 腾讯云 TCR 镜像（自行同步）
- 改用 `dockerproxy.com` / `gcr.io.dockerproxy.com` 等公开 mirror（注意稳定性与版权风险，本方案不内置）
- 在内网搭 `registry:2` 自托管

#### 10.5.3 GitHub API 代理

`LUMEN_UPDATE_PROXY_URL`（§12.6）只用于 GitHub Releases API 检查更新。镜像本身的 pull 走 §10.5.1 的 Docker daemon 代理，两者解耦。

## 11. 更新流程改造

### 11.1 保留 release 结构

现有更新脚本使用：

```text
ROOT/
  current -> releases/<id>
  previous -> releases/<old-id>
  releases/<id>/
  shared/.env
```

这个结构可以保留。

优点：

- 管理后台已有 release 信息解析逻辑
- 回滚仍然可以切换 `current`
- `.env` 和运行时数据继续在 `shared/`

### 11.2 旧更新流程

旧流程关键步骤：

```text
clone new release
link shared
docker compose up postgres redis
uv sync --frozen --all-packages
uv run alembic upgrade head
npm ci
npm run build
switch current
systemctl restart lumen-worker lumen-web lumen-tgbot lumen-api
health check
cleanup
```

### 11.3 新更新流程

默认发布更新流程：

```text
clone new release
link shared (含 .env)
validate .env
set_image_tag       (写入 LUMEN_IMAGE_TAG 到 shared/.env，见 §6.4.1)
backup_preflight    (强制 PG dump，§11.3.1)
docker compose pull
docker compose up -d --wait postgres redis
docker compose run --rm migrate
switch current
docker compose up -d --wait api worker web
如果 tgbot profile 启用，则 docker compose --profile tgbot up -d tgbot
health check
cleanup
```

注意顺序：

- `set_image_tag` 必须在 `pull` 之前。`LUMEN_IMAGE_TAG` 由 update channel + GitHub Releases 决定（§6.4.1）；不写就会 pull 旧 tag，看起来"成功"但版本未变
- `migrate` 必须在 `current` 切换前或切换后都可以执行；推荐在新 release 目录内先执行
- **迁移失败则 fail-fast，不切 `current`、不重启业务容器**——旧容器继续跑旧代码，DB schema 也没改坏
- 切换后执行 `compose up -d`，确保容器使用新 release 的 compose 文件和新镜像
- 只有显式 `LUMEN_UPDATE_BUILD=1` 时才执行 `docker compose build`
- 如果 migration 是非向前兼容的（罕见），整个流程必须改为"先停旧 API/Worker → migrate → 起新 API/Worker"，并接受短暂全站 503；这种情况必须在 PR 描述里显式标注，CI 也应有 lint 检查（参考 §11.6）

### 11.3.1 更新阶段协议

为了让命令行和管理后台体验一致，更新脚本应输出结构化阶段：

```text
::lumen-step:: phase=check status=start ts=...
::lumen-step:: phase=check status=done rc=0 dur_ms=...
::lumen-step:: phase=pull_images status=start ts=...
::lumen-info:: phase=pull_images key=tag value=sha-abcdef1
::lumen-step:: phase=pull_images status=done rc=0 dur_ms=...
```

推荐阶段：

| phase | 含义 | 默认行为 / 失败后处理 |
| --- | --- | --- |
| `lock` | 获取全局更新锁 | 已被占则返回 `system_operation_busy` + `retry_after`，直接退出 |
| `check` | 检查当前版本和目标版本 | 失败直接退出；`current == target` 时跳过后续 |
| `preflight` | 检查 Docker、磁盘（默认要求 ≥ 5GB 余量）、`.env` 关键字段、数据目录权限 | 失败直接退出 |
| `backup_preflight` | **默认强制**调用 `scripts/backup.sh`（PG dump + redis snapshot）。仅当 `LUMEN_UPDATE_SKIP_BACKUP=1` 时跳过；失败默认 abort | 失败 abort（无备份不允许更新——4K 任务环境下这是死规则） |
| `fetch_release` | 拉 compose/release 元数据，确认目标 tag 在 GHCR 上存在 | 失败直接退出 |
| `set_image_tag` | 把目标 `LUMEN_IMAGE_TAG` 写入 `shared/.env` 唯一一行，校验 grep 唯一存在 | 失败直接退出 |
| `pull_images` | `docker compose pull` | 失败保持旧服务运行；提示检查 GHCR / 代理（§10.5） |
| `start_infra` | 启动 postgres/redis 并等待 healthy | 失败保持旧服务运行 |
| `migrate_db` | 执行 Alembic `upgrade head` | **失败 abort，不切 `current`、不重启业务服务**（fail-fast 死规则） |
| `switch` | 更新 `current` symlink；`previous` 指向旧 release | 失败回滚 symlink |
| `restart_services` | `docker compose up -d --wait api worker web` | 失败自动尝试 `LUMEN_IMAGE_TAG=<previous>` 重新 up |
| `health_check` | API `/healthz` + Web `/` + Worker redis ping | 失败提示 §18 rollback 命令 |
| `cleanup` | `docker image prune` + 删除旧 release（保留 N=3 份） | 不阻断成功 |

### 11.3.2 pull 优先，build 兜底

更新策略：

```text
默认：docker compose pull
失败：提示镜像仓库不可达
如果 LUMEN_UPDATE_BUILD=1：docker compose build
如果管理后台触发：默认不允许 build，除非管理员在服务器配置中显式允许
```

原因：

- `pull` 可控、快、依赖少
- `build` 依赖网络、Docker cache、npm/uv 下载和平台差异，失败面大
- 对普通用户，“更新”应像 Sub2API 一样下载发布物，而不是现场生产发布物

### 11.4 Compose project name

release 目录每次不同，但 Compose project name 必须固定：

```bash
export COMPOSE_PROJECT_NAME=lumen
```

否则 Docker 会认为每个 release 是不同项目，导致：

- 容器名冲突
- volume 名变化
- 数据卷错位

所有脚本中的 `docker compose` 都应设置：

```bash
COMPOSE_PROJECT_NAME=lumen docker compose ...
```

### 11.5 镜像标签

建议用 commit SHA 给本地镜像打标签：

```env
LUMEN_IMAGE_TAG=<git-sha>
```

Compose 中：

```yaml
api:
  image: lumen-api:${LUMEN_IMAGE_TAG:-local}

worker:
  image: lumen-worker:${LUMEN_IMAGE_TAG:-local}

web:
  image: lumen-web:${LUMEN_IMAGE_TAG:-local}
```

如果不设置 image，只使用 build，Compose 也能工作，但回滚时不如带 tag 清晰。

### 11.6 回滚影响

数据库迁移通常不可逆。

回滚只能保证：

- `current` 指回旧 release
- 容器用旧代码重新启动

不能保证：

- 数据库 schema 回到旧状态

因此 Alembic migration 必须遵守向前兼容：

```text
先加列 / 加表 / 双写
再切代码
最后单独清理旧字段
```

## 12. 后台一键更新设计

### 12.1 API 只做控制面

管理后台的一键更新应学习 Sub2API 的产品体验，但不能照搬其“应用进程替换自己”的实现。Lumen 是 Docker 多服务部署，API 容器不应直接持有宿主机 Docker 权限。

推荐职责划分：

```text
Web UI
  -> API: 创建 update request / 查看状态 / 取消或重试

API
  -> 写 update request
  -> 获取全局更新锁
  -> 启动受限 updater
  -> 流式读取 update log

Updater
  -> 在宿主机执行 scripts/update.sh
  -> 调 docker compose pull/run/up
  -> 写结构化日志
```

### 12.2 Updater 方案选择

有三种可选实现：

| 方案 | 做法 | 优点 | 缺点 | 推荐度 |
| --- | --- | --- | --- | --- |
| systemd runner | API 请求宿主机 `systemd-run` 或触发已安装的 `lumen-update-runner.service` | 不暴露 Docker socket 给 API；和现有架构接近 | 依赖 systemd/sudoers | 推荐生产 |
| updater sidecar | 独立 `lumen-updater` 容器挂载 Docker socket 和 deploy 目录 | Docker 化完整；API 权限较小 | Docker socket 权限很大，需要强隔离 | 可选 |
| API 直接执行 Docker | API 容器挂 Docker socket 并执行 compose | 实现最短 | 安全边界差 | 不推荐 |

本方案推荐第一阶段使用 systemd runner，因为仓库已有管理后台触发 update runner 的基础逻辑；切 Docker 后只需要把 runner 内部执行内容改为 Compose 流程。

### 12.3 systemd runner 设计

宿主机已存在：

```text
/etc/systemd/system/lumen-update-runner.service     (deploy/systemd/lumen-update-runner.service)
/etc/systemd/system/lumen-update.path                (deploy/systemd/lumen-update.path)
```

#### 12.3.1 现行触发链（保留）

API 容器 `/admin/update` 写 `/opt/lumendata/backup/.update.trigger` → `lumen-update.path` 的 `PathChanged` 触发 `lumen-update-runner.service`。**整条链不依赖 sudo**，API 在 sandbox 内只需要对 `/opt/lumendata/backup` 有写权限。

切容器后这条链继续可用：API 容器把 `/opt/lumendata/backup` 作为 bind mount 挂进来即可写 trigger。

#### 12.3.2 必须修改的 unit 字段

当前 `lumen-update-runner.service` 写死了：

```ini
Environment=LUMEN_UPDATE_GIT_PULL=1
Environment=LUMEN_UPDATE_BUILD=1
```

切到预构建镜像后，**`LUMEN_UPDATE_BUILD=1` 必须改为 `LUMEN_UPDATE_BUILD=0`**，否则后台一键更新仍会走 `docker compose build`，与 §11.3.2 "pull 优先，build 兜底" 矛盾。`LUMEN_UPDATE_GIT_PULL=1` 保留——release 目录布局未变。

如需保留 build 能力作为兜底（pull 失败时本地构建），通过 `EnvironmentFile=/opt/lumendata/backup/.update.env` 让 API 在写 trigger 时按需注入 `LUMEN_UPDATE_BUILD=1`，而不是在 unit 里写死。

#### 12.3.3 备选 sudo 触发（不推荐，仅作兜底）

如果未来希望脱离 PathChanged 机制，也可以用受限 sudoers：

```text
systemctl start lumen-update-runner.service
systemctl status lumen-update-runner.service
```

不允许泛化到 `systemctl *` 或 `docker *`。本方案**不采用这条路径**，仅在 PathChanged 不可用的环境下作为兜底。

### 12.4 updater sidecar 设计

如果未来希望“纯 Docker 部署，不依赖 systemd”，可以增加 updater sidecar：

```yaml
lumen-updater:
  image: ghcr.io/cyeinfpro/lumen-updater:${LUMEN_IMAGE_TAG:-latest}
  profiles: ["updater"]
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
    - ./:/deploy
    - /opt/lumendata:/opt/lumendata
  environment:
    - COMPOSE_PROJECT_NAME=lumen
```

安全要求：

- updater 默认不常驻或只监听本地 Unix socket
- API 调 updater 必须有内部共享密钥
- updater 只允许执行白名单动作：
  - `check`
  - `pull`
  - `migrate`
  - `up`
  - `rollback`
  - `logs`
- 禁止任意 shell 命令透传

### 12.5 全局更新锁

学习 Sub2API 的 `SystemOperationLockService`，Lumen 必须对以下动作加同一把全局锁：

- update
- rollback
- restart
- restore
- uninstall

锁可以复用现有数据库表或 Redis key。要求：

- 有 owner operation id
- 有 locked_until
- 长任务自动续租
- 失败后可过期回收
- API 返回明确的 `retry_after`

示例错误：

```json
{
  "error": {
    "code": "system_operation_busy",
    "message": "another update is already running",
    "operation_id": "update-20260503-abcdef",
    "retry_after": 28
  }
}
```

### 12.6 版本检查

Sub2API 直接查 GitHub Releases 并缓存 20 分钟。Lumen 可以类似：

```text
GET /admin/release/check
```

返回：

```json
{
  "current_version": "sha-old",
  "latest_version": "sha-new",
  "has_update": true,
  "build_type": "docker-release",
  "release_info": {
    "name": "v1.2.3",
    "body": "...",
    "html_url": "https://github.com/cyeinfpro/Lumen/releases/tag/v1.2.3"
  },
  "images": {
    "api": "ghcr.io/cyeinfpro/lumen-api:sha-new",
    "worker": "ghcr.io/cyeinfpro/lumen-worker:sha-new",
    "web": "ghcr.io/cyeinfpro/lumen-web:sha-new"
  },
  "cached": false
}
```

GitHub 访问应支持代理：

```env
LUMEN_UPDATE_PROXY_URL=http://127.0.0.1:7890
```

### 12.7 更新日志与前端展示

更新脚本继续写：

```text
/opt/lumen/.update.log
```

或：

```text
/opt/lumen-deploy/logs/update-<operation-id>.log
```

API SSE 读取结构化行并推给前端。前端展示：

```text
检查版本        已完成
拉取镜像        进行中  ghcr.io/cyeinfpro/lumen-web:sha-...
数据库迁移      等待中
重启服务        等待中
健康检查        等待中
```

失败时展示最后 40 行非敏感日志，并给出命令：

```bash
docker compose logs --tail=120 api
docker compose ps
```

日志必须避免输出：

- `DATABASE_URL`
- `REDIS_URL`
- `SESSION_SECRET`
- `PROVIDERS`
- API keys
- bot token

## 13. 健康检查改造

### 13.1 HTTP 检查

保留：

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:3000/
```

### 13.2 Compose 状态检查

新增检查：

```bash
docker compose ps --status running api worker web
```

如果服务定义了 healthcheck，则检查：

```bash
docker inspect --format '{{.State.Health.Status}}' lumen-api
docker inspect --format '{{.State.Health.Status}}' lumen-web
```

### 13.3 Worker 检查

最小可用方案——容器内 redis ping，识别 `REDIS_URL` 错配：

```yaml
worker:
  healthcheck:
    test:
      - "CMD-SHELL"
      - "python -c 'import os,redis; redis.from_url(os.environ[\"REDIS_URL\"]).ping()' || exit 1"
    interval: 30s
    timeout: 5s
    retries: 3
    start_period: 20s
```

用 `python -c` 而不是 `redis-cli`，避免 §7.1 镜像额外装 `redis-tools`。`redis` Python 包已经在 `.venv` 内。

长期建议 Worker 暴露一个健康信号，例如：

- arq queue 中 worker 自身的 heartbeat key
- 独立的 metrics HTTP server（与 API 分端口）
- `/healthz/worker` 由 API 间接检查（API 查 redis 中 worker heartbeat）

本次切换不强制新增 Worker HTTP server，但 §22 风险清单要标注"worker `running` ≠ `healthy`"是已知 gap。

## 14. nginx 配置影响

如果 nginx 当前代理宿主机端口：

```nginx
proxy_pass http://127.0.0.1:3000;
```

切换后仍可保持不变，因为 Docker 会把 Web 映射到宿主机 `3000`。

如果 nginx 直接代理 API：

```nginx
proxy_pass http://127.0.0.1:8000;
```

也可以保持不变，因为 API 映射到宿主机 `127.0.0.1:8000`。

推荐继续让浏览器只访问 Web，由 Next.js rewrites 转发：

```text
Browser -> Web /api/* -> api:8000
```

这样 nginx 不必维护 `/api` 和 `/events` 的额外分流。

## 15. 安全和权限

### 15.1 容器用户

不同服务进程在容器内运行的 uid 必须分别对齐 bind mount 目录的属主，**不能统一**：

| 服务 | 容器内 uid:gid | 来源 | 对应 bind mount |
| --- | --- | --- | --- |
| `postgres` | `70:70` | `postgres:16-alpine` 内置 `postgres` 用户 | `/opt/lumendata/postgres` |
| `redis` | `999:999` | `redis:7-alpine` 内置 `redis` 用户 | `/opt/lumendata/redis` |
| `api` / `worker` / `web` / `tgbot` | `10001:10001` | 自建 `lumen` 用户（§7.1） | `/opt/lumendata/storage`、`/opt/lumendata/backup` |

postgres / redis 的 uid 是 alpine 镜像内置约定，不可改（改了会失去 PGDATA 兼容性）。当前 `docker-compose.yml:20,46` 已经显式 `user: "70:70"` / `user: "999:999"`，新 compose 必须保留。

### 15.2 `/opt/lumendata` 权限设置

按服务分别 chown，**禁止整体 `chown -R 10001:10001 /opt/lumendata`**——否则 postgres 以 uid 70 启动会读不了 PGDATA，redis 以 uid 999 启动同样失败。

```bash
sudo mkdir -p \
  /opt/lumendata/postgres \
  /opt/lumendata/redis \
  /opt/lumendata/storage \
  /opt/lumendata/backup

sudo chown -R 70:70   /opt/lumendata/postgres
sudo chown -R 999:999 /opt/lumendata/redis
sudo chown -R 10001:10001 /opt/lumendata/storage
sudo chown -R 10001:10001 /opt/lumendata/backup

sudo chmod 700 /opt/lumendata/postgres
sudo chmod 700 /opt/lumendata/redis
sudo chmod 750 /opt/lumendata/storage /opt/lumendata/backup
```

`/opt/lumendata` 顶层目录归 root，不递归改属主：

```bash
sudo chown root:root /opt/lumendata
sudo chmod 755 /opt/lumendata
```

宿主机脚本备份（`scripts/backup.sh`）当前依赖 `docker exec lumen-pg pg_dump`，备份产物落 `/opt/lumendata/backup`，必须由容器内 uid 10001 可写——上面 `chown` 已覆盖。如需让宿主机 root 直接写入 backup（运维人工备份），加 ACL：

```bash
sudo setfacl -R -m u:root:rwx /opt/lumendata/backup
sudo setfacl -R -d -m u:10001:rwx /opt/lumendata/backup
```

### 15.3 Secret

不得写入镜像：

- `SESSION_SECRET`
- `DB_PASSWORD`
- `REDIS_PASSWORD`
- `PROVIDERS`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_BOT_SHARED_SECRET`

这些只能来自 `.env` 或 Docker secret。

本次先沿用 `.env`。

## 16. 备份与恢复

现有 `scripts/backup.sh` / `scripts/restore.sh` 依赖容器名：

- `lumen-pg`
- `lumen-redis`

如果保持容器名不变，备份脚本大体可以继续工作。

需要检查：

- `docker exec lumen-pg pg_dump ...`
- `docker exec lumen-redis redis-cli ...`
- Redis 数据路径仍为 `/data`
- Postgres 数据卷仍挂载 `/var/lib/postgresql/data`

切换前必须先做一次备份：

```bash
bash scripts/backup.sh
```

并确认输出：

```text
/opt/lumendata/backup/pg/<timestamp>.pg.dump.gz
/opt/lumendata/backup/redis/<timestamp>.redis.tgz
```

## 17. 一次性切换实施步骤

### 17.0 已有部署的数据迁移（named volume → bind mount + COMPOSE_PROJECT_NAME）

已有部署的 PG/Redis 数据在 Docker named volume 里：

```text
<dirname>_lumen_pg_data
<dirname>_lumen_redis_data
```

`<dirname>` 是当前 `docker-compose.yml` 所在目录名（例如 `Image` 或 `image`）。

新方案要求两件事同时切换，**两件都会让旧 volume 失效**：

1. `COMPOSE_PROJECT_NAME=lumen`（§11.4）—— Compose 会去找 `lumen_lumen_pg_data`，找不到就静默创建空 volume
2. `volumes:` 改 bind mount 到 `/opt/lumendata/{postgres,redis}`（§8.2）—— Compose 直接挂宿主机目录，不再用 named volume

如果不做迁移就直接切，结果是 PG/Redis **看起来正常启动但数据全空**——这是本切换最危险的隐患。

迁移 SOP：

```bash
# 1. 完整停掉旧栈，确保 PG/Redis 没有写操作
cd /opt/lumen/current   # 或当前部署目录
docker compose down

# 2. 备份现有数据（双保险）
bash scripts/backup.sh

# 3. 记录现有 volume 名，确认非空
OLD_PG_VOL="$(docker volume ls -q | grep '_lumen_pg_data$' | head -n1)"
OLD_REDIS_VOL="$(docker volume ls -q | grep '_lumen_redis_data$' | head -n1)"
test -n "$OLD_PG_VOL" -a -n "$OLD_REDIS_VOL" || { echo "未找到旧 volume，请人工确认"; exit 1; }
docker run --rm -v "$OLD_PG_VOL":/src alpine du -sh /src
docker run --rm -v "$OLD_REDIS_VOL":/src alpine du -sh /src

# 4. 创建 bind mount 目标目录与权限（§15.2）
sudo mkdir -p /opt/lumendata/postgres /opt/lumendata/redis
sudo chown -R 70:70   /opt/lumendata/postgres
sudo chown -R 999:999 /opt/lumendata/redis
sudo chmod 700 /opt/lumendata/postgres /opt/lumendata/redis

# 5. 拷贝（保留属主和权限）
docker run --rm \
  -v "$OLD_PG_VOL":/src:ro \
  -v /opt/lumendata/postgres:/dst \
  alpine sh -c "cp -a /src/. /dst/"
docker run --rm \
  -v "$OLD_REDIS_VOL":/src:ro \
  -v /opt/lumendata/redis:/dst \
  alpine sh -c "cp -a /src/. /dst/"

# 6. 校验关键文件存在
sudo test -f /opt/lumendata/postgres/PG_VERSION || { echo "PG 数据未就位"; exit 1; }
sudo ls /opt/lumendata/redis/ | grep -E '\.(rdb|aof)$' || echo "redis 数据未就位（如果是首次部署可忽略）"

# 7. 启动新栈（COMPOSE_PROJECT_NAME=lumen，bind mount 生效）
COMPOSE_PROJECT_NAME=lumen docker compose up -d --wait postgres redis

# 8. 业务验证后再删除旧 volume（强烈建议保留至少一周）
# docker volume rm "$OLD_PG_VOL" "$OLD_REDIS_VOL"
```

如果用户只切 project name 不切 bind mount（简化迁移），可以用 `external: true` 引用旧 volume，但长期不推荐；本方案不在默认路径中支持这种半切状态。

### 17.1 切换前检查

```bash
git status --short
docker compose version
docker info
df -h
ls -la /opt/lumendata/ 2>/dev/null
docker volume ls | grep lumen
```

确认：

- 没有未备份的重要本地改动
- Docker daemon 正常，`docker compose` v2.17+（要支持 `--wait`）
- 磁盘 `/opt` 至少预留 10GB（旧数据 + 镜像 + 备份）
- 当前服务可访问
- `/opt/lumendata` 目录确实存在；如果生产是 `var/storage` 自部署，先按 `STORAGE_ROOT` 实际值核对
- 旧 named volume `<dirname>_lumen_pg_data` / `<dirname>_lumen_redis_data` 是否存在 + 数据量级，对应 §17.0 迁移路径

### 17.2 备份

```bash
bash scripts/backup.sh
```

记录当前 release：

```bash
readlink current || true
git rev-parse --short HEAD
```

### 17.3 构建验证

```bash
docker compose config
docker compose build api worker web
```

如启用 Bot：

```bash
docker compose build tgbot
```

### 17.4 停旧运行时

如果当前使用 systemd：

```bash
sudo systemctl stop lumen-api lumen-worker lumen-web lumen-tgbot
```

暂时不 disable，等 Docker 验证成功后再 disable。

### 17.5 启动基础设施

```bash
COMPOSE_PROJECT_NAME=lumen docker compose up -d --wait postgres redis
```

### 17.6 执行迁移

```bash
COMPOSE_PROJECT_NAME=lumen docker compose run --rm migrate
```

**fail-fast**：迁移退出码非零必须立即停止整个切换流程，不进入 §17.7。在已运行业务的服务器上，旧业务此时仍在跑（旧代码 + 旧 schema）；如果继续往下走，新业务会接到已变形 schema 的 DB，但旧 systemd 服务还没 stop（§17.4 是 stop 不 disable），双写冲突会更严重。

### 17.7 启动业务服务

```bash
COMPOSE_PROJECT_NAME=lumen docker compose up -d --wait api worker web
```

如果启用 Bot：

```bash
COMPOSE_PROJECT_NAME=lumen docker compose --profile tgbot up -d tgbot
```

### 17.8 验证

```bash
docker compose ps
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:3000/
```

功能验证：

- 登录
- 创建会话
- 发送文本消息
- 上传图片
- 生图任务入队
- SSE 进度返回
- 图片保存和预览
- 管理后台 Provider 列表
- 备份入口
- Telegram Bot，如果启用

### 17.9 禁用旧 systemd

Docker 栈确认稳定后：

```bash
sudo systemctl disable --now lumen-api lumen-worker lumen-web lumen-tgbot
```

保留 systemd unit 文件也可以，但不要让它们开机自启。

## 18. 回滚方案

### 18.1 应用层回滚

如果 Docker 新版本启动失败，但数据库迁移没有破坏兼容性：

```bash
# 1. 切回旧 release 目录
ln -sfn releases/<old-id> current
cd current

# 2. 把 LUMEN_IMAGE_TAG 改回上一已知好版本（§6.4.1 同入口）
#    旧 tag 通常存在 .update.env 历史日志里，或从 GHCR tag 列表挑选
sed -i 's|^LUMEN_IMAGE_TAG=.*|LUMEN_IMAGE_TAG=<old-tag>|' shared/.env

# 3. 拉旧镜像并重启业务容器
COMPOSE_PROJECT_NAME=lumen docker compose pull
COMPOSE_PROJECT_NAME=lumen docker compose up -d --wait api worker web
```

回滚走 `pull` 而非 `build`，与正向更新对称——这是预构建镜像策略的回报。`build` 只在镜像仓库不可达且用户显式 `LUMEN_UPDATE_BUILD=1` 时使用。

如果有 previous symlink：

```bash
OLD_ID="$(basename "$(readlink previous)")"
ln -sfn "releases/${OLD_ID}" current
```

CI 在每次 release 时把 `(version, sha)` 写入 GitHub Release notes 与 `releases/<id>/.image-tag` 文件，方便回滚时找到精确 tag。

### 18.2 回滚到 systemd

如果 Docker 栈完全不可用，且旧 systemd runtime 仍存在：

```bash
COMPOSE_PROJECT_NAME=lumen docker compose stop api worker web tgbot
sudo systemctl start lumen-api lumen-worker lumen-web
```

如果已 disable：

```bash
sudo systemctl enable --now lumen-api lumen-worker lumen-web
```

前提：

- 宿主机 `.venv`、`.next`、`node_modules` 仍可用
- 旧代码能兼容当前 DB schema

### 18.3 数据恢复

如果数据库迁移造成不可恢复问题，只能从备份恢复：

```bash
bash scripts/restore.sh
```

这会造成备份时间点之后的数据丢失。生产执行前必须人工确认。

## 19. 测试计划

### 19.1 静态检查

```bash
bash -n scripts/install.sh scripts/update.sh scripts/uninstall.sh scripts/lib.sh scripts/lumenctl.sh
docker compose config
```

### 19.2 镜像构建

```bash
docker compose build api worker web
docker compose build migrate bootstrap
```

### 19.3 单元测试

容器化后可以保留宿主机测试，也可以增加容器内测试。

宿主机：

```bash
bash scripts/test.sh
```

容器内：

```bash
docker compose run --rm api-test pytest apps/api/tests
docker compose run --rm worker-test pytest apps/worker/tests
```

是否新增 `api-test / worker-test` service 可后续决定。

### 19.4 集成冒烟

```bash
docker compose up -d --wait postgres redis
docker compose run --rm migrate
docker compose up -d --wait api worker web
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:3000/
```

### 19.5 更新脚本冒烟

在测试服务器执行：

```bash
bash scripts/lumenctl.sh update-lumen
```

观察：

- `.update.log`
- `docker compose ps`
- `docker compose logs api`
- `docker compose logs worker`
- `docker compose logs web`

## 20. 代码改造顺序

推荐按以下顺序提交，降低排查难度：

1. 版本号管理基础设施（已完成：`VERSION`、`scripts/version.py`、CI 检查、`docs/versioning.md`）
2. 新增 Dockerfile 和 `.dockerignore`
3. 扩展 `docker-compose.yml`
4. 调整 `.env.example`
5. 增加 GitHub Actions 预构建镜像 workflow
6. 改 `scripts/lib.sh` 的健康检查和 Compose helper
7. 改 `scripts/install.sh`
8. 改 `scripts/update.sh`
9. 改 `scripts/uninstall.sh`
10. 改 `scripts/lumenctl.sh`
11. 改 README / deploy README
12. 改测试断言
13. 本地 `docker compose config`
14. 构建 API / Worker / Web 镜像

## 21. 关键实现细节

### 21.1 自动修正容器内 URL

安装脚本生成 `.env` 时直接写容器地址：

```env
DATABASE_URL=postgresql+asyncpg://...@postgres:5432/...
REDIS_URL=redis://:...@redis:6379/0
LUMEN_BACKEND_URL=http://api:8000
LUMEN_API_BASE=http://api:8000
```

对于旧 `.env`，**严格按变量名白名单**进行替换，禁止全局 sed `localhost → 服务名`。原因：`.env` 中混合了"容器内地址"和"浏览器/CORS 地址"两类字段，全局替换会把 `PUBLIC_BASE_URL` / `CORS_ALLOW_ORIGINS` 等给浏览器用的 URL 也改坏，导致前端 CSP / CORS 失败。

替换字段对照表：

| 字段 | 旧值（典型） | 新值 | 说明 |
| --- | --- | --- | --- |
| `DATABASE_URL` | `...@localhost:5432/...` | `...@postgres:5432/...` | 必改 |
| `REDIS_URL` | `redis://:...@localhost:6379/0` | `redis://:...@redis:6379/0` | 必改 |
| `LUMEN_BACKEND_URL` | `http://127.0.0.1:8000` | `http://api:8000` | 必改（Next.js 服务端 rewrite 用） |
| `LUMEN_API_BASE` | `http://127.0.0.1:8000` | `http://api:8000` | 必改（tgbot 调 api 用） |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | **不改** | 给浏览器，必须是公网/反代地址 |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | **不改** | 给浏览器，按部署域名独立配置 |
| `NEXT_PUBLIC_API_BASE` | `/api` | **不改** | 浏览器同源相对路径 |

迁移脚本必须：

1. 默认 dry-run，输出 `before / after` diff，让用户确认
2. `--apply` 才落盘
3. 落盘前 `cp .env .env.bak.<timestamp>`
4. 替换 DB/Redis URL 时**保留原密码**（用 `awk` 切 `://` 后段，重写 host:port，不要拼字符串）
5. 替换后 grep 验证 `localhost`/`127.0.0.1` 仅剩在白名单中保留的字段里

### 21.2 `depends_on` 不能替代迁移

Compose 的 `depends_on.condition: service_healthy` 只能保证 Postgres/Redis 可连接，不能保证 schema 已迁移。

因此：

```bash
docker compose run --rm migrate
```

必须是安装和更新流程的显式步骤。

### 21.3 API 启动期迁移检查保留

API 当前启动时会检查 Alembic head。

这个检查应保留：

- 迁移没跑时，生产 API 应拒绝启动
- 这能防止旧脚本漏掉 `migrate`

### 21.4 `container_name`

可以继续保留固定 `container_name`：

```yaml
container_name: lumen-api
```

优点：

- 备份脚本容易兼容
- 运维排查命令稳定

缺点：

- 无法横向扩容同一个 service 多副本

当前 Lumen 是单机自托管，保留固定容器名更实用。

### 21.5 Bot 不应拖垮主栈

Bot 没有 token 或 shared secret 时会干净退出。若它在默认 `up -d --wait` 中，可能导致整体返回失败。

建议：

```yaml
tgbot:
  profiles: ["tgbot"]
```

启用时：

```bash
docker compose --profile tgbot up -d tgbot
```

## 22. 风险清单

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 首次镜像构建慢 | 更新时间变长 | CI 预构建并推 GHCR；用户侧默认 pull |
| `.env` 仍使用 localhost | 容器连不上 DB/Redis/API | §21.1 白名单 dry-run 替换，禁全局 sed |
| `PUBLIC_BASE_URL`/`CORS_ALLOW_ORIGINS` 被误改 | 前端 CSP / CORS 失败 | §21.1 显式排除清单 |
| 已有 named volume 在切 `COMPOSE_PROJECT_NAME` 后失联 | PG/Redis 看似启动但数据全空 | §17.0 显式迁移 SOP，禁止隐式切换 |
| `/opt/lumendata` 全局 chown 10001 | postgres(uid 70) / redis(uid 999) 启动失败 | §15.2 按服务分别 chown |
| `WEB_BIND_HOST=0.0.0.0` 默认值 | 绕过 nginx 直连 3000，frps 隧道架构下出网 | §8.3 默认 `127.0.0.1` |
| `LUMEN_IMAGE_TAG` 未在 pull 前写入 | pull 拉到旧 tag，更新看似成功实际未变 | §6.4.1 + §11.3.1 `set_image_tag` 阶段 |
| `lumen-update-runner.service` 写死 `LUMEN_UPDATE_BUILD=1` | 后台一键更新仍触发宿主 build | §12.3.2 改 `0`，build 仅经 EnvironmentFile 注入 |
| `stop_grace_period` 默认 10s | `compose up -d --force-recreate` 中断进行中的 4K 任务 | §8.5 worker `1830s`，逐服务显式声明 |
| Web standalone 配置不完整 | Web 容器启动失败 | 先用非 standalone 方案，§19 加冒烟用例 |
| Bot 无 token 导致 compose wait 失败 | 主栈启动失败 | §5 / §21.5 放 profile |
| 迁移后回滚旧代码不兼容 | 回滚失败 | §11.6 强制 forward-compatible migration；切换前备份 |
| 镜像占用磁盘 | 磁盘打满 | `cleanup` 阶段 `docker image prune`；保留近 N=3 旧 release |
| 管理后台 update 仍假设 systemd | 后台更新失败 | 同步改 `scripts/update.sh` 阶段和测试，runner 走 PathChanged |
| ghcr.io 国内访问慢 / 失败 | install / update 卡 pull | §10.5 Docker daemon 代理 + `LUMEN_IMAGE_REGISTRY` 可改 |
| 单架构镜像（amd64）部署到 arm64 | 无法启动 | §6.5 GitHub Actions 引入 buildx matrix（本期可先 amd64-only，但需写入 README） |
| `redis-entrypoint.sh` 漏挂载 | redis 回到无密码状态 | §6.2 / §8.4 显式声明挂载 |
| Worker `running` ≠ `healthy` | redis URL 错配但容器仍 up | §13.3 加 `python -c redis.ping` healthcheck |
| `backup_preflight` 未强制 | 4K 任务环境下回滚无依据 | §11.3.1 默认强制 PG dump，仅 `LUMEN_UPDATE_SKIP_BACKUP=1` 跳过 |

## 23. 完成标准

切换完成需满足：

- `docker compose config` 通过
- `docker compose build api worker web` 通过
- `docker compose up -d --wait postgres redis api worker web` 通过
- `docker compose ps` 中核心服务为 running 或 healthy
- `curl http://127.0.0.1:8000/healthz` 通过
- `curl http://127.0.0.1:3000/` 通过
- 登录、消息、上传、生图、SSE、图片访问通过
- `bash scripts/lumenctl.sh update-lumen` 不再执行宿主机 `uv sync / npm ci / systemctl restart lumen-*`
- `bash scripts/uninstall.sh` 能停止完整 Compose 栈
- README 和 deploy 文档不再把 systemd 作为默认运行方式
- `lumen-update-runner.service` 中 `LUMEN_UPDATE_BUILD=0`（§12.3.2）
- 4K 长任务在 `docker compose up -d --force-recreate` 期间不被中断（验证 worker `stop_grace_period: 1830s`）
- bind mount 目录属主对应 §15.2 的服务-uid 表
- `LUMEN_IMAGE_TAG` 在更新流程的 `set_image_tag` 阶段被正确写入（验证 `shared/.env` diff）
- 国内网络环境下 `docker compose pull` 在配置 `LUMEN_HTTP_PROXY` 后可以稳定完成（§10.5）

## 24. 推荐最终运维命令

安装：

```bash
bash scripts/lumenctl.sh install-lumen
```

更新：

```bash
bash scripts/lumenctl.sh update-lumen
```

查看状态：

```bash
docker compose ps
```

查看日志：

```bash
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f web
```

迁移：

```bash
docker compose run --rm migrate
```

重启：

```bash
docker compose up -d --force-recreate api worker web
```

停止：

```bash
docker compose down
```

停止并删除 DB/Redis 数据卷：

```bash
docker compose down -v
```

最后一条会删除数据库和 Redis 数据，只能在明确要卸载或重建时执行。
