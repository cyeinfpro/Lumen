# Lumen

Lumen 是一个自托管的多模态 AI 工作室：在同一个对话流里完成文本聊天、视觉问答、文生图、图生图、长会话压缩、图片分享和 Telegram 机器人生成。

后端采用 FastAPI + arq Worker，前端采用 Next.js，数据层使用 PostgreSQL 16 + Redis 7。图片二进制默认落本地文件系统，元数据与任务状态落 PostgreSQL，实时进度通过 Redis Pub/Sub + Redis Stream + SSE 推送。

## 核心能力

- **统一会话流**：`chat`、`vision_qa`、`text_to_image`、`image_to_image` 四种 intent 共用 `conversations/messages` 数据模型。
- **后台任务可恢复**：API 只负责写库和入队，Worker 独立执行；浏览器关窗、刷新、断线后可通过 SSE replay 和任务快照恢复状态。
- **流式文本与工具进度**：聊天走 Responses SSE，支持 reasoning delta、web search、file search、code interpreter、chat 内 image generation。
- **高分辨率图像**：支持 1K/2K/4K 预设；显式 `fixed_size` 校验最长边 <= 3840、16 对齐、总像素 655,360 到 8,294,400、长宽比 <= 3:1。
- **多 Provider Pool**：支持 priority、weight、启停、代理、provider 级图片并发、image-job 能力、探活、熔断、failover 和账号级配额。
- **图片处理链路**：生成图会保存原图、display2048、preview1024、thumb256；支持 blurhash、透明背景后处理、分享链接与签名图片代理。
- **长上下文治理**：200K 级输入预算、rolling summary、手动压缩、出窗图片 caption、压缩熔断和上下文健康统计。
- **管理后台**：用户白名单、邀请链接、请求事件、Provider、代理池、系统设置、Telegram、备份入口。
- **Telegram Bot**：网页绑定码绑定账号，bot 内菜单式生图、prompt 优化、任务列表、重试、Redis Stream 断点续推图片。
- **可选 image-job sidecar**：把同步图片上游封装为异步任务，并提供临时图片 URL 和 reference URL。

## 架构总览

```text
Browser / Telegram
   |
   | REST / upload / SSE
   v
Next.js Web ---------------> FastAPI API
   |                            |
   | same-origin /api rewrite   | PostgreSQL: users, sessions, conversations,
   |                            | messages, tasks, images, shares, settings
   |                            |
   |                            | Redis: arq queue, Pub/Sub, user event streams,
   |                            | rate limits, leases, health state
   |                            v
   |                       arq Worker
   |                            |
   |                            | Provider Pool + proxies + retries
   |                            v
   |                    OpenAI-compatible upstream
   |                    /v1/responses, /v1/images/*
   |
   +-- optional image-job sidecar for async image relay
```

## 技术栈

| 层 | 技术 |
| --- | --- |
| Web | Next.js 16, React 19, TypeScript, TanStack Query, Zustand, Framer Motion, lucide-react |
| API | FastAPI, Uvicorn, Pydantic v2, SQLAlchemy async, Alembic |
| Worker | arq, httpx, Pillow, blurhash, Prometheus metrics |
| Bot | aiogram 3, httpx, Redis Stream listener |
| Core | 共享 SQLAlchemy models、Pydantic schemas、尺寸校验、上下文窗口、Provider Pool 解析 |
| Infra | PostgreSQL 16, Redis 7, Docker Compose v2 全栈，Nginx；systemd 仅作兜底 |
| Observability | Prometheus metrics, Sentry, OpenTelemetry |

## 仓库结构

```text
.
├── apps/
│   ├── api/        # FastAPI 网关、REST/SSE 路由、Alembic 迁移、管理后台 API
│   ├── worker/     # arq Worker、上游调用、图片处理、任务重试、outbox publisher
│   ├── web/        # Next.js 前端工作台、管理后台、分享页、邀请页
│   └── tgbot/      # Telegram Bot：绑定、菜单生图、任务列表、结果推送
├── packages/
│   └── core/       # API/Worker 共享模型、schema、常量、尺寸、上下文和 provider helpers
├── image-job/      # 可选异步图片 sidecar
├── deploy/         # systemd、nginx、watchdog、image-job 部署模板
├── scripts/        # install/update/test/backup/restore/uninstall 运维脚本
├── docs/           # 架构设计、4K 支持、部署与集成说明
├── docker-compose.yml
└── pyproject.toml  # uv workspace
```

## 系统要求

生产部署：

- macOS 或 Linux
- Docker 24+
- Docker Compose v2.17+（必须支持 `docker compose up --wait`）
- OpenSSL、curl
- Linux 上自动准备 `/opt/lumendata` 目录需要 root 或 sudo 权限
- Docker daemon 必须可启动；macOS 上首次启动 Docker Desktop 后重跑脚本

不再需要在宿主机安装 Python / uv / Node / npm —— API、Worker、Web、Bot 全部以 Docker 镜像运行。开发模式下才需要这些工具，详见下方 "开发模式" 一节。

## Docker 一键安装

最简一行（GitHub raw）：

```bash
curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/install.sh | bash
```

这条命令会先把仓库拉到 `~/Lumen`，再执行仓库内的 `scripts/install.sh`。如果要指定安装目录或分支：

```bash
curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/install.sh \
  | LUMEN_INSTALL_DIR=/opt/Lumen LUMEN_BRANCH=main bash
```

如果已经在项目目录内，直接：

```bash
bash scripts/install.sh
```

或通过统一运维菜单：

```bash
bash scripts/lumenctl.sh
```

`lumenctl.sh` 会把 Lumen、image-job 和 nginx 相关操作放在同一个入口。运行后会出现交互菜单：

```text
1) 安装 Lumen
2) 更新 Lumen
3) 卸载 Lumen
4) 安装 image-job
5) 卸载 image-job
6) 扫描 nginx 配置
7) nginx 反代优化向导
8) 查看运行状态（compose ps + 健康检查）
9) 跟随 API 日志（compose logs -f api）
10) 重启 api/worker/web（compose up -d --force-recreate）
11) 启动 api/worker/web（compose up -d --wait）
12) 停止 api/worker/web/tgbot（compose stop）
13) 执行 DB migrate（compose --profile migrate run --rm migrate）
0) 退出
```

也可以不进菜单，直接执行单个动作：

```bash
# Lumen 生命周期
bash scripts/lumenctl.sh install-lumen
bash scripts/lumenctl.sh update-lumen
bash scripts/lumenctl.sh uninstall-lumen
bash scripts/lumenctl.sh rollback              # 回滚到 previous release（pull 旧 tag）
bash scripts/lumenctl.sh version               # VERSION + 镜像 tag + git sha

# Compose 运维
bash scripts/lumenctl.sh status                # docker compose ps + 健康检查
bash scripts/lumenctl.sh logs api              # 跟随 api 日志
bash scripts/lumenctl.sh start                 # up -d --wait api worker web
bash scripts/lumenctl.sh stop                  # stop api worker web tgbot
bash scripts/lumenctl.sh restart               # up -d --force-recreate
bash scripts/lumenctl.sh migrate               # compose --profile migrate run --rm migrate
bash scripts/lumenctl.sh bootstrap             # 创建初始 admin（需 LUMEN_ADMIN_EMAIL/PASSWORD）
bash scripts/lumenctl.sh backup                # scripts/backup.sh
bash scripts/lumenctl.sh restore <ts>          # scripts/restore.sh <timestamp>

# image-job sidecar
bash scripts/lumenctl.sh install-image-job
bash scripts/lumenctl.sh uninstall-image-job

# nginx
bash scripts/lumenctl.sh nginx-scan
bash scripts/lumenctl.sh nginx-optimize
bash scripts/lumenctl.sh nginx-lumen
bash scripts/lumenctl.sh nginx-sub2api
bash scripts/lumenctl.sh nginx-sub2api-inner
bash scripts/lumenctl.sh nginx-sub2api-outer
bash scripts/lumenctl.sh nginx-image-job
```

安装脚本会执行：

- 检查 Docker / Compose v2 / OpenSSL / curl
- 准备 `/opt/lumendata/{postgres,redis,storage,backup}` 子目录并按服务分别 chown（参见 "数据目录与权限"）
- 准备 release 布局 `${LUMEN_DEPLOY_ROOT:-/opt/lumen}/{releases,shared,current}`
- 生成或合并 `shared/.env`，自动生成强随机密钥
- 探测 GHCR 镜像（`ghcr.io/cyeinfpro/lumen-{api,worker,web,tgbot}`），未发布 latest 时回退到 main
- `docker compose pull` -> 起 PostgreSQL + Redis -> migrate -> 可选 bootstrap admin -> api/worker/web (+tgbot)
- 切换 `current` symlink
- HTTP + compose 健康检查
- 打印汇总（Web 地址、管理员账号、状态/日志命令）

安装完成后访问：

```text
Web: http://<服务器IP>:3000
API health: http://127.0.0.1:8000/healthz
```

Web 默认绑定 `0.0.0.0:3000`。如果服务器本机 `curl http://127.0.0.1:3000` 正常，但外部浏览器打不开 `http://<服务器IP>:3000`，请检查云安全组或防火墙是否放行 TCP 3000。

## 更新

默认通过预构建 GHCR 镜像更新（`docker compose pull` -> migrate -> up）：

```bash
bash scripts/lumenctl.sh update-lumen
```

如果服务器访问 GitHub/GHCR 需要代理，在 `shared/.env` 写入 `LUMEN_HTTP_PROXY=http://127.0.0.1:7890` 或 `LUMEN_UPDATE_PROXY_URL=...`；命令行更新和管理面板「一键更新」都会自动读取。

实际阶段：`check` -> `backup_preflight` -> `fetch_release` -> `set_image_tag` -> `pull_images` -> `start_infra` -> `migrate_db` -> `switch` -> `restart_services` -> `health_check` -> `cleanup`。

如果需要在本机用 Dockerfile 重新构建（无 GHCR 访问，或本地有改动）：

```bash
LUMEN_UPDATE_BUILD=1 bash scripts/lumenctl.sh update-lumen
```

回滚到上一个 release（自动还原 `LUMEN_IMAGE_TAG` 并 `pull` 旧镜像）：

```bash
bash scripts/lumenctl.sh rollback
```

## 推荐运维命令

参见 `docs/docker-full-stack-cutover-plan.md` §24。

```bash
docker compose ps                                          # 服务状态
docker compose logs -f api                                 # 跟随 api 日志
docker compose logs -f worker                              # 跟随 worker 日志
docker compose --profile migrate run --rm migrate          # 单独跑迁移
docker compose up -d --force-recreate api worker web       # 重启业务容器
docker compose down                                        # 停止全部容器（保留数据卷）
docker compose down -v                                     # 同时删除数据卷（仅卸载/重建时）
```

带项目名的完整形式（避免和其他 compose 项目混淆）：

```bash
COMPOSE_PROJECT_NAME=lumen docker compose ps
COMPOSE_PROJECT_NAME=lumen docker compose logs -f api
```

## 数据目录与权限

所有持久化数据都落在 `/opt/lumendata`（`LUMEN_DATA_ROOT` 可改）：

```text
/opt/lumendata/postgres   # PostgreSQL PGDATA（容器 uid 70:70）
/opt/lumendata/redis      # Redis dump/aof（容器 uid 999:999）
/opt/lumendata/storage    # 图片原图、display/preview/thumb（容器 uid 10001:10001）
/opt/lumendata/backup     # PG dump + Redis dump（容器 uid 10001:10001）
```

`/opt/lumendata` 顶层归 root，子目录按服务分别 chown，禁止整体 `chown -R 10001:10001 /opt/lumendata` —— 否则 PostgreSQL（uid 70）、Redis（uid 999）启动会失败。安装脚本会自动按这张表设置；手动恢复时参考 `docs/docker-full-stack-cutover-plan.md` §15.2：

```bash
sudo mkdir -p /opt/lumendata/{postgres,redis,storage,backup}
sudo chown -R 70:70   /opt/lumendata/postgres
sudo chown -R 999:999 /opt/lumendata/redis
sudo chown -R 10001:10001 /opt/lumendata/storage /opt/lumendata/backup
sudo chmod 700 /opt/lumendata/postgres /opt/lumendata/redis
sudo chmod 750 /opt/lumendata/storage /opt/lumendata/backup
sudo chown root:root /opt/lumendata
sudo chmod 755 /opt/lumendata
```

## nginx 反代

生产推荐让 nginx 只反代 `Web:3000`，由 Next.js rewrites 转发 `/api/*` 与 `/events`，避免 nginx 维护两条上游。Web 默认同时可通过宿主机公网 `:3000` 直连；如只允许 nginx 本机反代，可在 `shared/.env` 设置 `WEB_BIND_HOST=127.0.0.1`。nginx 反代仍可指向本机回环：

```nginx
proxy_pass http://127.0.0.1:3000;
```

完整模板见 `deploy/nginx.conf.example`，关键约束：

- `client_max_body_size 60m`（与前端上传上限对齐）
- `proxy_buffering off`（SSE 必需）
- `proxy_request_buffering off`（大图上传不缓存到磁盘）
- `proxy_read_timeout 600s` 或更高（4K 图像 timeout 分层）
- `gzip off`（SSE 帧不能压缩）

也可以使用统一向导：`bash scripts/lumenctl.sh nginx-optimize`，进入后按部署拓扑选择：

- `Lumen 反代`：生成/更新 Lumen Web 反代，包含 `/api/`、`/events` SSE、上传大小和超时配置。
- `sub2api 单机公网反代`：公网域名所在 nginx 直接代理到本机 sub2api，例如 `http://127.0.0.1:8081`。
- `sub2api 内层反代`：sub2api 所在机器也有 nginx，先把本机 `127.0.0.1:8081` 暴露成内网地址/端口。
- `sub2api 外层公网反代`：公网域名在另一台机器上，该机器再代理到上面的内层 nginx。
- `image-job 路由注入`：扫描已有 nginx 站点，备份后注入 `/v1/image-jobs`、`/v1/refs`、`/images/temp/`、`/refs/`。

nginx 写入逻辑会先备份目标配置，写入后执行 `nginx -t`；如果校验失败会回滚。涉及 systemd、`/etc/nginx`、`/opt/image-job` 的操作需要在 Linux 服务器上用有 sudo 权限的用户运行。

## 开发模式

开发与生产分流：开发不要起完整的生产镜像栈，建议宿主机运行 API/Worker/Web，仅 PostgreSQL + Redis 用 docker compose。

```bash
# 1. 基础设施（仅 PG + Redis）
COMPOSE_PROJECT_NAME=lumen docker compose up -d --wait postgres redis

# 2. 同步依赖
uv sync
cd apps/api && uv run alembic upgrade head
cd ../web && npm ci

# 3. API
cd apps/api
uv run uvicorn app.main:app --reload --port 8000

# 4. Worker
cd apps/worker
uv run python -m arq app.main.WorkerSettings

# 5. Web
cd apps/web
npm run dev
```

可选启动 Telegram Bot：

```bash
cd apps/tgbot
uv run python -m app.main
```

手动创建管理员（在宿主机）：

```bash
cd apps/api
uv run python -m app.scripts.bootstrap admin@example.com --role admin --password 'change-me-strong'
```

或在容器栈里：

```bash
LUMEN_ADMIN_EMAIL=admin@example.com LUMEN_ADMIN_PASSWORD='change-me-strong' \
  bash scripts/lumenctl.sh bootstrap
```

开发模式需要的额外宿主机依赖：[`uv`](https://docs.astral.sh/uv/)、Python 3.12（uv 管理）、Node.js >= 20、npm。生产部署不需要这些。

## 环境变量

根目录 `.env` 是 API、Worker、Bot 的主要配置来源；Next.js 也会读取其中的服务端变量，同时可使用 `apps/web/.env.local` 写前端公开变量。

### 数据库与缓存

| 变量 | 说明 |
| --- | --- |
| `DB_USER` / `DB_PASSWORD` / `DB_NAME` | Docker Compose PostgreSQL 初始化变量 |
| `DATABASE_URL` | async SQLAlchemy 连接串，例如 `postgresql+asyncpg://...` |
| `REDIS_PASSWORD` | Docker Compose Redis 密码 |
| `REDIS_URL` | Redis DSN，生产不要写入日志 |
| `POSTGRES_BIND_HOST` / `REDIS_BIND_HOST` | Compose 端口绑定地址，默认 `127.0.0.1` |

### Provider Pool

`PROVIDERS` 是上游配置唯一入口。最小格式：

```env
PROVIDERS='[{"name":"default","base_url":"https://api.example.com","api_key":"sk-xxx","priority":0,"weight":1,"enabled":true}]'
```

带代理的新格式：

```json
{
  "proxies": [
    {
      "name": "s5-us",
      "type": "socks5",
      "host": "127.0.0.1",
      "port": 1080
    }
  ],
  "providers": [
    {
      "name": "default",
      "base_url": "https://api.example.com",
      "api_key": "sk-xxx",
      "proxy": "s5-us",
      "priority": 0,
      "weight": 1,
      "enabled": true,
      "image_concurrency": 1,
      "image_jobs_enabled": false
    }
  ]
}
```

Provider 关键字段：

- `priority`：数值越高越优先。
- `weight`：同优先级内加权轮询。
- `proxy`：引用 `proxies[]` 中的名称。
- `image_rate_limit` / `image_daily_quota`：账号级图片限流与日配额。
- `image_jobs_enabled`：该 provider 是否可走 image-job sidecar。
- `image_jobs_endpoint`：`auto`、`generations` 或 `responses`。
- `image_jobs_endpoint_lock`：锁定 provider 只服务指定 endpoint family。
- `image_jobs_base_url`：该 provider 独立 sidecar 地址，空则使用全局 `image.job_base_url`。
- `image_concurrency`：单 provider 图片并发上限。

### 应用与安全

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_ENV` | `dev` | 非 dev 会启用更严格 secret/cookie 校验 |
| `APP_PORT` | `8000` | API 端口 |
| `SESSION_SECRET` | 必填 | 生产必须显式设置，长度至少 32 字符 |
| `SESSION_TTL_MIN` | `10080` | 会话有效期，5 分钟到 30 天 |
| `IMAGE_PROXY_SECRET` | 空 | 分享页签名图片 URL 的 HMAC key，生产建议设置 >=32 字符 |
| `STORAGE_ROOT` | `/opt/lumendata/storage` | 原图和变体文件根目录 |
| `BACKUP_ROOT` | `/opt/lumendata/backup` | 备份根目录 |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | API/分享链接生成 fallback |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | 允许浏览器访问 API 的前端 origin |
| `TRUSTED_PROXIES` | 空 | 允许信任 `X-Forwarded-For` 的代理 CIDR |

生产要求：

- `SESSION_SECRET` 必须改成强随机值。
- `CORS_ALLOW_ORIGINS` 应设置为真实 HTTPS 前端域名。
- 非 dev 环境 cookie 会启用 `Secure`，请通过 HTTPS 访问。
- 如果启用签名图片代理，`IMAGE_PROXY_SECRET` 轮换会立即作废未过期签名 URL。

### 上游与图片运行时

这些值既可来自 env，也可在管理后台写入 `system_settings`，DB 优先：

| 设置/变量 | 说明 |
| --- | --- |
| `UPSTREAM_DEFAULT_MODEL` | 默认聊天模型，代码默认 `gpt-5.5` |
| `UPSTREAM_GLOBAL_CONCURRENCY` | 全局上游并发上限 |
| `UPSTREAM_PIXEL_BUDGET` | `size_mode=auto` 的默认像素预算 |
| `IMAGE_GENERATION_CONCURRENCY` | Worker 图片 FIFO 队列总并发 |
| `IMAGE_CHANNEL` | `auto`、`stream_only`、`image_jobs_only` |
| `IMAGE_ENGINE` | `responses`、`image2`、`dual_race` |
| `IMAGE_JOB_BASE_URL` | image-job sidecar 根地址 |
| `CHAT_FILE_SEARCH_VECTOR_STORE_IDS` | 默认 file_search vector store ids，逗号分隔 |

### Web

| 变量 | 说明 |
| --- | --- |
| `LUMEN_BACKEND_URL` | Next.js 服务端 rewrite 到 FastAPI 的地址，默认 `http://127.0.0.1:8000` |
| `NEXT_PUBLIC_API_BASE` | 浏览器侧 API 地址。留空时默认同源 `/api`；跨域部署才建议设绝对 URL |
| `LUMEN_UPGRADE_INSECURE_REQUESTS` | 生产 CSP 是否启用 `upgrade-insecure-requests` |
| `LUMEN_HSTS_INCLUDE_SUBDOMAINS` | 是否给 HSTS 加 `includeSubDomains` |

生产推荐让浏览器访问同源 `/api`，由 Next.js rewrites 转发到 API；跨子域部署时再设置 `NEXT_PUBLIC_API_BASE=https://api.example.com`。

### Telegram

| 变量 | 说明 |
| --- | --- |
| `TELEGRAM_BOT_SHARED_SECRET` | Bot 调 API 的共享密钥；非 dev 必须 >=32 字符 |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_BOT_USERNAME` | 不带 `@`，用于 deep link |
| `LUMEN_API_BASE` | Bot 调 API 的内网地址 |
| `TELEGRAM_PROXY_URL` | Bot 出站代理 fallback |
| `TELEGRAM_ALLOWED_USER_IDS` | 可选 Telegram user id 白名单 |
| `BOT_MODE` | `polling` 或 `webhook`；当前入口实现 polling |

## 主要运行机制

### 消息与任务

`POST /conversations/{id}/messages` 会在一个事务内创建：

1. user message
2. assistant placeholder message
3. completion 或 generation 任务
4. outbox event

事务提交后 API 会尽力立即入队并发布 queued 事件；如果 Redis/ARQ 暂时失败，Worker 的 outbox publisher 每 2 秒扫描未发布事件并补偿入队。

### SSE

前端订阅：

```text
GET /events?channels=user:{uid},conv:{cid},task:{task_id}
```

特点：

- API 会强制校验 channel owner。
- 每个用户有 Redis Stream：`events:user:{uid}`。
- 浏览器重连时用 `Last-Event-ID` replay 最近事件。
- Worker 同时向 Pub/Sub 和 user stream 写事件。
- 前端还有 5 秒一次的 in-flight 自愈轮询，避免刷新瞬间错过终态事件。

### 图片生成

支持三类引擎策略：

- `responses`：`/v1/responses` + `image_generation` tool。
- `image2`：直调 `/v1/images/generations` 或 `/v1/images/edits`。
- `dual_race`：responses 和 image2 并发竞速，先完成者展示，后完成者可作为 bonus 图 attach。

支持三类通道策略：

- `auto`：先选 provider，再按 provider 的 `image_jobs_enabled` 决定 sidecar 或流式路径。
- `stream_only`：强制直接走上游流式/同步路径。
- `image_jobs_only`：强制走 image-job，不支持的 provider 会返回 503。

Worker 侧图片任务有：

- 全局 FIFO 队列
- provider 级并发锁
- lease 续租和 stuck reconciler
- cancel flag
- retry + backoff
- provider avoid set
- moderation 多 provider 尝试上限
- 透明背景 prompt 增强、alpha refine 和 QC
- 原图、display、preview、thumb 多文件写入

### 聊天与长上下文

聊天 completion 会打包当前会话上下文：

- 已知模型 `gpt-5.4` / `gpt-5.5` 输入预算为 200K token，未知模型保守 fallback。
- 保留 response reserve，按消息 token 估算做窗口裁剪。
- 支持 rolling summary、sticky original task、summary guardrail。
- 可选 web_search、file_search、code_interpreter、image_generation tools。
- 长对话可自动或手动压缩，压缩失败有 circuit breaker，失败时降级截断。

### 图片存储与分享

- 上传限制：请求体总上限约 60MB，单图片上传 50MB，支持 PNG/JPEG/WebP。
- 上传图片最长边超过 4096 会被缩小。
- 存储 key 限制在 `STORAGE_ROOT` 下，读取时禁止路径逃逸和 symlink。
- 私有图片接口需要登录 owner check。
- 分享链接支持单图或多图，可选择是否展示 prompt，可设置默认过期天数。
- 签名图片端点额外要求图片处于有效 share 中，防止签名 key 泄漏后任意读私图。

## API 概览

| 路径 | 说明 |
| --- | --- |
| `GET /healthz` | 进程存活，不依赖 Redis/PostgreSQL |
| `GET /readyz` | Redis `PING` + DB `SELECT 1` |
| `/auth/*` | signup、login、logout、me、csrf、password reset |
| `/conversations/*` | 会话列表、详情、更新、删除、上下文健康、手动压缩 |
| `POST /conversations/{id}/messages` | 核心消息入口 |
| `/generations/{id}` / `/completions/{id}` | 任务快照、取消、重试 |
| `/tasks` / `/tasks/mine/active` | 用户任务聚合 |
| `/images/*` | 上传、元数据、binary、variants、签名代理 |
| `/events` | SSE |
| `/generations/feed` | 当前用户生成图灵感流 |
| `/share/*` | 创建/撤销/公开分享 |
| `/invite/*` | 邀请链接 |
| `/admin/*` | 用户、白名单、请求事件、provider、代理池、设置、备份、模型、Telegram |
| `/telegram/*` | Bot service-to-service 端点 |

## Telegram Bot

启用步骤：

1. 设置 `TELEGRAM_BOT_SHARED_SECRET`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_BOT_USERNAME`。
2. 启动 API、Worker、Bot。
3. 登录 Web，进入设置或管理后台生成 Telegram 绑定码。
4. 在 Telegram 发送 `/start <code>` 完成绑定。
5. 使用 `/new` 配置参数并发送 prompt。

Bot 结果推送使用 Redis Stream `events:user:{uid}` 和 cursor `tg:bot:cursor:{uid}`。Bot 重启或网络中断后，只要事件还在 stream 保留窗口内，就能继续补推。发送图片统一走 `sendDocument`，避免 Telegram `sendPhoto` 压缩 4K 原图。

管理后台可以调整 `telegram.*` 设置并通过 `/admin/telegram/restart` 发布 Redis 控制消息，让 Bot clean exit 后由 Docker `restart: unless-stopped` 自动拉起。

## image-job Sidecar

`image-job/` 是独立 FastAPI 服务，用于把同步图片上游封装为异步任务：

```text
POST /v1/image-jobs
GET  /v1/image-jobs/{job_id}
POST /v1/refs
GET  /images/temp/...
GET  /refs/...
```

它会：

- 校验并保存 job 到 SQLite
- 后台队列调用 `IMAGE_JOB_UPSTREAM_BASE_URL + endpoint`
- 从 JSON/SSE/URL/data URL 中提取最终图片
- 保存到临时目录并返回公网 URL
- 定期清理过期图片和 job 行
- requeue stuck queued/running job

`image-job` 必须绑定一个已经运行的 sub2api/OpenAI 兼容上游。统一安装菜单会让你填写实际的 `IMAGE_JOB_UPSTREAM_BASE_URL`，例如本机常见默认值 `http://127.0.0.1:8081`，也可以是其他端口、内网地址或公网反代地址。脚本会探测你填写的地址；如果当前机器连不上该上游，会中止安装。

最小启动：

```bash
cd image-job
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
IMAGE_JOB_UPSTREAM_BASE_URL=http://127.0.0.1:8081 \
IMAGE_JOB_PUBLIC_BASE_URL=https://img.example.com \
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8091
```

部署模板见：

- `deploy/image-job/image-job.service`
- `deploy/image-job/image-job.example.com.conf`
- `deploy/image-job/nginx-image-job.locations.conf`

也可以用统一菜单自动执行：

```bash
bash scripts/lumenctl.sh install-image-job
bash scripts/lumenctl.sh nginx-optimize
```

`nginx-optimize` 的各类反代拓扑见上文“快速安装”里的统一运维脚本说明。

## 测试

后端测试应分进程跑，避免 `apps/api` 和 `apps/worker` 都叫顶层包 `app` 时污染 Python module cache：

```bash
bash scripts/test.sh -q
```

单独执行：

```bash
uv run pytest apps/worker/tests -q
uv run pytest apps/api/tests -q
uv run pytest packages/core/tests -q
```

前端：

```bash
cd apps/web
npm run lint
npm run type-check
npm run build
```

常用完整验证：

```bash
bash scripts/test.sh -q
cd apps/web && npm run lint && npm run type-check && npm run build
```

## 生产部署

默认部署方式 = Docker Compose 全栈。代码、镜像 tag、数据目录约定：

```text
/opt/lumen          # release 布局：releases/、shared/、current
/opt/lumen/shared/.env   # 统一环境变量；release/.env 为 -> shared/.env 的 symlink
/opt/lumendata     # postgres + redis + storage + backup
```

详细切换 SOP 与目录权限、镜像构建、回滚方案见 [`docs/docker-full-stack-cutover-plan.md`](docs/docker-full-stack-cutover-plan.md)：

- §1.2 安装 / 更新最佳体验
- §3.1 / §3.2 命令行体验原则
- §15.2 数据目录权限（按服务分别 chown）
- §17 一次性切换实施步骤
- §18 回滚方案（应用层 / systemd 兜底 / 数据恢复）
- §22 风险清单 / §23 完成标准 / §24 推荐运维命令

systemd 不再是默认入口；`deploy/systemd/lumen-{api,worker,web,tgbot}.service` 仅作为 Docker 栈不可用时的兜底（详见 `deploy/README.md`）。`deploy/systemd/lumen-update-runner.service`（默认 `LUMEN_UPDATE_BUILD=0`）和 `deploy/systemd/lumen-backup.{service,timer}` 保留并继续生效。

典型发布流程：

```bash
cd /opt/lumen/current
bash scripts/lumenctl.sh update-lumen
```

或直接：

```bash
bash scripts/update.sh
```

`scripts/update.sh` 会按阶段执行 pull -> migrate -> switch -> restart，失败时输出回滚命令；不再 `uv sync / npm ci / systemctl restart lumen-*`。

## 运维脚本

```bash
# 更新（pull 优先；LUMEN_UPDATE_BUILD=1 改为本地构建）
bash scripts/lumenctl.sh update-lumen

# 卸载向导：docker compose down --remove-orphans，可选 down -v；数据默认保留
bash scripts/uninstall.sh

# 备份 PostgreSQL + Redis（容器内 pg_dump / SAVE，落 /opt/lumendata/backup）
bash scripts/lumenctl.sh backup

# 恢复指定 timestamp 的备份
bash scripts/lumenctl.sh restore 20260424-123000
```

备份默认目录：

```text
/opt/lumendata/backup/pg/<timestamp>.pg.dump.gz
/opt/lumendata/backup/redis/<timestamp>.redis.tgz
```

`lumen-backup.timer` 默认每 4 小时备份一次，保留最近 `MAX_KEEP=40` 份。

## 可观测性

- API：`/metrics` 默认启用，可通过 `METRICS_ENABLED` 关闭。
- Worker：独立 Prometheus metrics server，默认 `WORKER_METRICS_PORT=9100`。
- 支持 Sentry：`SENTRY_DSN`、`SENTRY_ENVIRONMENT`、`SENTRY_TRACES_SAMPLE_RATE`。
- 支持 OTEL HTTP exporter：`OTEL_EXPORTER_OTLP_ENDPOINT`、`OTEL_SERVICE_NAME`。
- 上游指标包括请求数、耗时、token、`x-codex-primary-used-percent`。
- Provider stats 会汇总 total/success/fail/success_rate/traffic_pct。

## 故障排查

**Docker 未运行**

```bash
docker info
```

macOS 启动 Docker Desktop；Linux 启动 Docker 服务并确认当前用户可访问 docker socket。

**5432 或 6379 端口冲突**

```bash
lsof -iTCP:5432 -sTCP:LISTEN -nP
lsof -iTCP:6379 -sTCP:LISTEN -nP
```

Linux 可用：

```bash
ss -ltnp 'sport = :5432'
ss -ltnp 'sport = :6379'
```

**API 启动时报 migration head mismatch**

```bash
cd apps/api
uv run alembic upgrade head
```

生产环境 schema 不在 Alembic head 会拒绝启动；开发环境只 warn。测试或离线脚本可临时设置 `LUMEN_SKIP_MIGRATION_CHECK=1`。

**上传图片 413**

同时检查三层上限：

- Nginx `client_max_body_size 60m`
- Next.js `experimental.proxyClientMaxBodySize = "60mb"`
- API body middleware 约 60MB，图片上传 route 单文件 50MB

**前端 network_error 或 mixed content**

- 同源部署建议不要设置绝对 `NEXT_PUBLIC_API_BASE`，让它默认 `/api`。
- 跨域部署时，`NEXT_PUBLIC_API_BASE` 必须是浏览器可访问的 HTTPS API 地址。
- `CORS_ALLOW_ORIGINS` 必须包含前端 origin。

**SSE 长时间无事件或一次性收到一堆事件**

检查 Nginx：

- `/events` 必须 `proxy_buffering off`
- `gzip off`
- `proxy_read_timeout` 足够长
- `X-Accel-Buffering no`

**Worker 任务卡住**

查看 Worker 日志和 Redis lease：

```bash
docker compose logs --tail=200 worker
docker compose exec redis redis-cli --no-auth-warning keys 'task:*:lease'
```

reconciler 每分钟会扫描 stuck generation/completion，lease 过期后自动 requeue 或标 timeout。

**Provider 全部不可用**

在管理后台检查：

- Provider 是否 enabled
- base_url 是否合法
- api_key 是否正确
- 代理是否可用
- image endpoint lock 是否把当前请求过滤掉
- 账号级 rate limit / daily quota 是否耗尽

**Telegram Bot 收不到图**

- 确认 Bot 已绑定账号：Web 生成绑定码后 `/start <code>`。
- 检查 `TELEGRAM_BOT_SHARED_SECRET` 与 API 一致。
- 检查 bot 是否能访问 Telegram API，国内服务器通常需要代理。
- 检查 Redis Stream 是否有 `events:user:*`。
- 查看日志：`docker compose logs --tail=200 tgbot`。

## 安全说明

- 所有写操作使用 session cookie + CSRF double-submit。
- `SESSION_SECRET`、`REDIS_URL`、`PROVIDERS`、`TELEGRAM_BOT_TOKEN` 不应出现在日志或前端环境变量中。
- Provider/API key 只应存在后端 `.env` 或系统设置敏感字段里。
- API 统一错误结构为 `{ "error": { "code", "message", "details?" } }`，避免泄漏内部异常。
- 图片文件读取做 root 限制、路径逃逸检查、regular file 检查和 symlink 防护。
- 公共分享接口有公开限流；用户级发送/上传限流可通过 `USER_RATE_LIMIT_ENABLED` 控制。

## 文档入口

- `docs/DESIGN.md`：产品、架构、数据模型和任务流设计。
- `docs/docker-full-stack-cutover-plan.md`：全栈 Docker 化切换方案（镜像、Compose、迁移、回滚、运维命令）。
- `docs/4k-support-upgrade-plan.md`：4K 尺寸策略和约束。
- `docs/responses-image-integration-guide.md`：Responses 图片链路集成说明。
- `docs/image-gateway-test-summary.md`：图片网关行为验证摘要。
- `deploy/README.md`：Docker 部署、nginx 反代、systemd 兜底说明。
- `apps/api/README.md`、`apps/worker/README.md`、`image-job/README.md`：子模块说明。

## License

MIT. See `LICENSE`.
