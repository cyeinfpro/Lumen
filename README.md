# Lumen

Lumen 是一个自托管的多模态 AI 创作工作台。它把聊天、视觉问答、文生图、图生图、视频生成、Storyboard、资产流、分享、Telegram Bot、Provider 管理、计费和更新运维放在同一个私有部署里。

默认部署形态是 Docker Compose 全栈：Next.js Web、FastAPI API、arq Worker、Telegram Bot、PostgreSQL 16 + pgvector、Redis 7，以及可选的 `image-job` 异步图片 sidecar。

原生桌面客户端已停止维护并下架。Lumen 现在只提供自托管的 Web / Docker 部署形态；历史桌面安装包和自动更新清单不再作为受支持的分发渠道。

## 为什么用 Lumen

- **任务可恢复**：API 写库和入队，Worker 执行长任务；浏览器刷新或断线后通过 SSE replay、任务快照和 reconciler 恢复状态。
- **Provider Pool**：支持 priority、weight、启停、代理、账号级限流/日配额、图片并发、探活、熔断和 failover。
- **多模态创作流**：聊天、图片、视频、服装展示、海报、Storyboard 和资产管理共用账户、存储、任务和分享体系。
- **长上下文治理**：支持 rolling summary、手动压缩、图片 caption、上下文健康统计和压缩熔断。
- **可私有化运维**：版本化 release、stable/main channel、备份恢复、后台一键更新、Prometheus/Sentry/OTEL 接入。

## 核心功能

- **Studio**：文本聊天、视觉问答、图片生成/编辑、inpaint、工具调用、全局任务托盘。
- **视频工作台**：视频 provider、参考图/视频、队列进度、计费 hold/settle/release。
- **项目工作流**：服装模特库、自然场景展示、海报风格库、Storyboard 分镜/设定图/关键帧/视频生成。
- **资产与分享**：生成资产流、已加载作品搜索、签名图片代理、多图分享、公开分享页。
- **账户与管理**：登录、邀请、白名单、钱包、兑换码、BYOK、系统提示词、记忆、Provider/Proxy、备份、更新、存储后端。
- **Telegram Bot**：网页绑定码绑定账号，Bot 内生成、任务列表、重试和 Redis Stream 断点续推。
- **image-job sidecar**：可选，把同步图片上游封装成异步任务，并提供临时图片 URL / reference URL。

## 架构

```text
Browser / Telegram
   |
   | REST / upload / SSE
   v
Next.js Web ---------------> FastAPI API
   | runtime proxy              |
   | /api/*, /events            | PostgreSQL: users, tasks, messages,
   |                            | images, videos, shares, billing, settings
   |                            |
   |                            | Redis: arq queue, Pub/Sub, Streams,
   |                            | rate limits, leases, health state
   |                            v
   |                       arq Worker
   |                            |
   |                            | Provider Pool + proxies + retries
   |                            v
   |                    OpenAI-compatible upstreams
   |                    /v1/responses, /v1/images/*, video APIs
   |
   +-- optional image-job sidecar for async image relay
```

### Repository Layout

```text
apps/api/       FastAPI routes, Alembic migrations, admin/update/backup APIs
apps/worker/    arq worker, provider calls, image/video processing, billing
apps/web/       Next.js frontend, shell, Studio, Stream, Projects, Admin
apps/tgbot/     Telegram Bot
packages/core/  shared models, schemas, pricing, provider and URL helpers
image-job/      optional async image sidecar
scripts/        install/update/backup/restore/test/version/lumenctl
deploy/         nginx, systemd, image-job deployment templates
docs/           design docs, plans and runbooks
```

## Quick Start

Open the installer / operations menu from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/install.sh | bash
```

Direct install from a checkout:

```bash
git clone https://github.com/cyeinfpro/Lumen.git
cd Lumen
bash scripts/install.sh --install
```

Useful operations:

```bash
bash scripts/lumenctl.sh menu
bash scripts/lumenctl.sh status
bash scripts/lumenctl.sh update-lumen
bash scripts/lumenctl.sh rollback
bash scripts/lumenctl.sh backup
bash scripts/lumenctl.sh restore <timestamp>
```

After install, Web listens on `127.0.0.1:3000` by default. Public access should normally go through nginx/Caddy/Traefik. To expose port `3000` directly, set `WEB_BIND_HOST=0.0.0.0` intentionally and open the firewall/security group yourself.

## Production Deployment

Production is Docker Compose full stack. The installer creates:

```text
/opt/lumen                 releases/, shared/, current
/opt/lumen/shared/.env     runtime environment
/opt/lumendata/storage     uploaded/generated media
/opt/lumendata/backup      PostgreSQL and Redis backups
```

PostgreSQL and Redis data may be split from media storage with `LUMEN_DB_ROOT`, which is recommended when `LUMEN_DATA_ROOT` is CIFS/NAS:

```text
${LUMEN_DB_ROOT:-/opt/lumendata}/postgres   uid 999:999
${LUMEN_DB_ROOT:-/opt/lumendata}/redis      uid 999:999
${LUMEN_DATA_ROOT:-/opt/lumendata}/storage  uid 10001:10001
${LUMEN_DATA_ROOT:-/opt/lumendata}/backup   uid 10001:10001
```

Do not recursively `chown` all of `/opt/lumendata` to the app user. The database/cache directories must keep their own owners.

### Updates

Stable updates follow the latest GitHub Release tag and GHCR release images. `main` is a rolling channel for testing and must be opted into explicitly:

```env
LUMEN_UPDATE_CHANNEL=main
LUMEN_IMAGE_TAG=main
```

Stable install/update does not silently fall back to `main`. If a release image is unavailable, fix the release or use an intentional local build:

```bash
LUMEN_UPDATE_BUILD=1 bash scripts/lumenctl.sh update-lumen
```

Default fast update skips the preflight backup for speed. Use standard mode or trigger a backup first when the deployment requires a restore point:

```bash
bash scripts/lumenctl.sh backup
LUMEN_UPDATE_MODE=standard bash scripts/lumenctl.sh update-lumen
```

### Backup

`scripts/backup.sh` backs up PostgreSQL and Redis only:

```text
/opt/lumendata/backup/pg/<timestamp>.pg.dump.gz
/opt/lumendata/backup/redis/<timestamp>.redis.tgz
```

`MAX_KEEP=56` by default, about 9.3 days at the default 4-hour timer interval. Media files under `/opt/lumendata/storage` need filesystem, NAS, or object-storage snapshots.

### Reverse Proxy

Recommended nginx shape: proxy the whole site to Web on `127.0.0.1:3000`; Next.js `src/proxy.ts` forwards `/api/*` and `/events` to FastAPI via `LUMEN_BACKEND_URL`.

Important nginx invariants:

- `client_max_body_size 80m`
- `proxy_buffering off` for SSE
- `proxy_request_buffering off` for large uploads
- `proxy_send_timeout 3600s` for long uploads and generation requests
- `proxy_read_timeout 1800s` for long generation tasks
- `gzip off` for SSE frames

Use the helper:

```bash
bash scripts/lumenctl.sh nginx-optimize
```

## Configuration

Copy `.env.example` to `.env` for manual compose use, or edit `/opt/lumen/shared/.env` after install.

| Area | Important variables |
| --- | --- |
| Runtime | `APP_ENV`, `PUBLIC_BASE_URL`, `CORS_ALLOW_ORIGINS`, `TRUSTED_PROXIES`, `SESSION_COOKIE_SECURE`, `LUMEN_HSTS_ENABLED`, `LUMEN_HSTS_INCLUDE_SUBDOMAINS` |
| Database/cache | `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DATABASE_URL`, `REDIS_PASSWORD`, `REDIS_URL` |
| Secrets | `SESSION_SECRET`, `IMAGE_PROXY_SECRET`, `BYOK_API_KEY_MASTER_SECRET` |
| Provider pool | `PROVIDERS`, `UPSTREAM_DEFAULT_MODEL`, `UPSTREAM_GLOBAL_CONCURRENCY`, `IMAGE_GENERATION_CONCURRENCY` |
| Storage/backup | `LUMEN_DATA_ROOT`, `LUMEN_DB_ROOT`, `STORAGE_ROOT`, `BACKUP_ROOT`, `MAX_KEEP` |
| Web | `LUMEN_BACKEND_URL`, `NEXT_PUBLIC_API_BASE`, `NEXT_PUBLIC_LUMEN_VERSION`, `LUMEN_UPGRADE_INSECURE_REQUESTS` |
| Telegram | `TELEGRAM_BOT_SHARED_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `LUMEN_API_BASE` |
| image-job | `IMAGE_JOB_UPSTREAM_BASE_URL`, `IMAGE_JOB_PUBLIC_BASE_URL`, `IMAGE_JOB_DATA_DIR`, `IMAGE_JOB_STATE_DIR`, `IMAGE_JOB_CONCURRENCY` |

Production requirements:

- Required secrets:

| Variable | Requirement | Generate |
| --- | --- | --- |
| `SESSION_SECRET` | 必填 | `openssl rand -hex 64` |
| `IMAGE_PROXY_SECRET` | 必填 | `openssl rand -hex 32` |
| `BYOK_API_KEY_MASTER_SECRET` | 必填 | `python -c 'import secrets; print(secrets.token_urlsafe(48))'` |

- `APP_ENV=prod`
- `SESSION_SECRET`, `IMAGE_PROXY_SECRET`, `BYOK_API_KEY_MASTER_SECRET` must be strong random values.
- `SMTP_HOST` and `SMTP_FROM_EMAIL` are required outside development for password reset email.
- `IMAGE_CHANNEL=stream_only` disables image-job routing and does not require sidecar configuration.
- `IMAGE_CHANNEL=auto` only selects image-job for providers with `image_jobs_enabled`; an unavailable URL/token falls back to stream without blocking production startup.
- `IMAGE_CHANNEL=image_jobs_only` is strict and fails fast unless `IMAGE_JOB_BASE_URL` is a non-placeholder `http`/`https` URL with a host and `IMAGE_JOB_SIDECAR_TOKEN` is a whitespace-free token of at least 32 characters.
- HTTPS production should use `SESSION_COOKIE_SECURE=1` and render the outer nginx template with `LUMEN_HSTS_ENABLED=true`. The historical cookie default remains Secure outside dev/local/test.
- Direct production HTTP is an explicit compatibility mode only: `PUBLIC_BASE_URL` must use `http` with a loopback, RFC1918/ULA private, or link-local address, `SESSION_COOKIE_SECURE=0`, and `LUMEN_HSTS_ENABLED=false`. Public domains/IPs and HTTPS with non-Secure cookies are rejected at API configuration load.
- HSTS is emitted only by the outermost nginx. API and Web do not add it. `LUMEN_HSTS_INCLUDE_SUBDOMAINS` defaults to `false`; enable it only after every subdomain is permanently HTTPS-ready.
- `USER_RATE_LIMIT_ENABLED` is a development convenience flag. Production and tests enforce regular per-user limiters regardless of the configured value; auth/reset/public limiters are always on.
- Never expose `REDIS_URL`, `PROVIDERS`, provider keys, BYOK secrets, Telegram token, or session secrets to frontend variables or logs.

After login, verify that the client stored and returned the session cookie without
exposing its value:

```bash
curl -sS -D /tmp/lumen-login.headers -c /tmp/lumen-cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"REPLACE_ME"}' \
  https://example.com/api/auth/login >/dev/null
grep -i '^set-cookie:' /tmp/lumen-login.headers
curl -sS -b /tmp/lumen-cookies.txt https://example.com/api/auth/me
```

The second request returns the authenticated user only after the API has
validated the returned session cookie. Cookie values are never returned.

## Development

Use host processes for API/Worker/Web and Docker only for PostgreSQL + Redis:

```bash
COMPOSE_PROJECT_NAME=lumen docker compose up -d --wait postgres redis
uv sync

cd apps/api
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000

cd ../worker
uv run python -m arq app.main.WorkerSettings

cd ../web
npm ci
npm run dev
```

Optional Telegram Bot:

```bash
cd apps/tgbot
uv run python -m app.main
```

Create an admin user:

```bash
cd apps/api
uv run python -m app.scripts.bootstrap admin@example.com --role admin --password 'change-me-strong'
```

## Tests

Unified gate:

```bash
bash scripts/test.sh -q
```

This runs Python ruff, API/Worker/Core/TgBot/image-job/tool tests, web tests, web lint, web type-check and web build. Targeted commands:

```bash
uv run pytest apps/api/tests -q
uv run pytest apps/worker/tests -q
uv run pytest packages/core/tests -q
uv run pytest image-job/tests -q

cd apps/web
npm test
npm run lint
npm run type-check
npm run build
```

## Release

Maintainer release flow:

```bash
# 1. Bump VERSION, for example 1.2.44
$EDITOR VERSION

# 2. Sync all version targets and lockfile
python3 scripts/version.py sync
uv lock
python3 scripts/version.py check

# 3. Validate
bash scripts/test.sh -q

# 4. Commit and push main
git add .
git commit -m "Release vX.Y.Z"
git push origin main

# 5. Create and push the matching tag
git tag vX.Y.Z
git push origin vX.Y.Z
```

The tag-triggered GitHub Actions `Docker Release` is the production release gate. A main-branch Docker run is not enough for stable/default updates because `latest` is only updated by a successful formal `v*` release tag.

Manual `workflow_dispatch.ref` is for branch/SHA rebuilds only. It must not be used to create tag release semantics.

## Troubleshooting

**Docker not running**

```bash
docker info
```

Start Docker Desktop on macOS or the Docker daemon on Linux.

**Port conflicts**

```bash
lsof -iTCP:3000 -sTCP:LISTEN -nP
lsof -iTCP:8000 -sTCP:LISTEN -nP
```

**Migration mismatch**

```bash
cd apps/api
uv run alembic upgrade head
```

Production refuses to start if the DB is not at Alembic head.

**Upload 413**

Check all three layers: nginx `client_max_body_size 80m`, Next.js `proxyClientMaxBodySize: "80mb"`, API upload/body limits.

**Mixed content or frontend network errors**

Default browser requests should stay same-origin with `NEXT_PUBLIC_API_BASE=/api`. For cross-domain API deployments, use an HTTPS absolute URL and make sure `CORS_ALLOW_ORIGINS` includes the frontend origin.

**SSE stalls or batches events**

Check nginx: `proxy_buffering off`, `gzip off`, `proxy_read_timeout` high enough, `X-Accel-Buffering no`.

**Worker task stuck**

```bash
docker compose logs --tail=200 worker
docker compose exec redis redis-cli --no-auth-warning keys 'task:*:lease'
```

**Provider unavailable**

Check provider enabled state, base URL, API key, proxy, endpoint lock, image rate limits and daily quota in the admin panel.

**Update issues**

See `docs/runbooks/update-troubleshooting.md`.

## Security Notes

- Session writes use cookie auth + CSRF double-submit.
- `TRUSTED_PROXIES` must list only direct reverse proxies; never use `0.0.0.0/0`.
- Signed image URLs are constrained to valid shares.
- Private file reads guard against path traversal and symlinks.
- `image-job` validates downloaded image URLs and every redirect hop against private, loopback, link-local and metadata targets.
- BYOK keys are encrypted at rest with `BYOK_API_KEY_MASTER_SECRET`; rotating it invalidates stored user keys.
- Public share and invite endpoints are rate-limited.

## Documentation Map

- `apps/web/DESIGN.md`: current Web design system and UI implementation rules.
- `docs/DESIGN.md`: legacy product architecture, data model and historical decisions.
- `docs/docker-full-stack-cutover-plan.md`: Docker compose design, migration and rollback details.
- `deploy/README.md`: deployment templates, nginx, systemd backup/update runner and image-job notes.
- `docs/runbooks/update-troubleshooting.md`: update failures.
- `docs/runbooks/redis-password-mismatch-fix.md`: Redis auth drift.
- `docs/runbooks/blue-green-upgrade.md`: blue/green operations.
- `docs/4k-support-upgrade-plan.md`: 4K image constraints.
- `docs/responses-image-integration-guide.md`: Responses image integration.
- `apps/api/README.md`, `apps/worker/README.md`, `apps/web/README.md`, `image-job/README.md`: module-specific notes.

## License

Lumen is licensed under the PolyForm Noncommercial License 1.0.0 + Commercial License upon request. See `LICENSE`.

Noncommercial use is permitted under PolyForm Noncommercial License 1.0.0. Commercial use, including selling hosted Lumen services, paid integrations, resale, or using Lumen primarily for commercial advantage, requires a separate commercial license from the copyright holders.
