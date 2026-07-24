# Lumen API (FastAPI)

## 本地启动

```bash
# 1. 基础设施
docker compose up -d

# 2. 装依赖（推荐 uv；pip 也行）
uv sync                          # 在仓库根目录运行，会同时装 core/api/worker

# 3. 迁移
cd apps/api && uv run alembic upgrade head

# 4. 启动 API
uv run uvicorn app.main:app --reload --port 8000
```

## 目录

- `app/main.py` — FastAPI 入口
- `app/config.py` — pydantic-settings
- `app/db.py` — async SQLAlchemy session
- `app/redis_client.py` — redis.asyncio 单例
- `app/routes/` — 按 DESIGN §5 拆分的路由模块
- `alembic/` — 迁移脚本

路由范围：auth / conversations / messages / tasks / images / events (SSE)。
参考仓库根 `docs/DESIGN.md` §5、§7、§22。

## Production transport contract

HTTPS production keeps the default secure posture:

```dotenv
APP_ENV=prod
PUBLIC_BASE_URL=https://example.com
SESSION_COOKIE_SECURE=1
LUMEN_HSTS_ENABLED=true
LUMEN_HSTS_INCLUDE_SUBDOMAINS=false
```

`SESSION_COOKIE_SECURE` may be omitted to preserve the historical behavior:
cookies are non-Secure in dev/local/test and Secure elsewhere. A direct HTTP
production deployment must opt in explicitly:

```dotenv
APP_ENV=prod
PUBLIC_BASE_URL=http://10.0.0.20:3000
SESSION_COOKIE_SECURE=0
LUMEN_HSTS_ENABLED=false
```

Use HTTP mode only on an isolated loopback, RFC1918/ULA private, or link-local
network. Public domains/IPs are rejected, as is any HTTPS
`PUBLIC_BASE_URL` combined with `SESSION_COOKIE_SECURE=0`. The API emits a
critical startup warning whenever this compatibility mode is active.

HSTS is owned by the outermost nginx template. The API and Web do not emit the
header. `LUMEN_HSTS_ENABLED=false` suppresses it, while
`LUMEN_HSTS_INCLUDE_SUBDOMAINS=true` adds `includeSubDomains` only after all
subdomains have been confirmed HTTPS-ready.

To validate the browser/cookie-jar round trip after login, call `GET /auth/me`.
The endpoint returns the authenticated user only after validating the session
cookie and never exposes cookie values.

## Image-job and rate-limit startup semantics

- `IMAGE_CHANNEL=stream_only` means image-job is disabled, so the API production
  validator does not require `IMAGE_JOB_BASE_URL`.
- `IMAGE_CHANNEL=auto` does not block API startup on static sidecar
  configuration. The worker checks URL/token only after a provider with image
  jobs enabled is selected, then falls back to stream when that configuration
  is unavailable.
- `IMAGE_CHANNEL=image_jobs_only` is strict: production startup requires a
  non-placeholder `http`/`https` `IMAGE_JOB_BASE_URL` with a host, and the
  worker also requires `IMAGE_JOB_SIDECAR_TOKEN`.
- `USER_RATE_LIMIT_ENABLED=0` only disables regular per-user limiters in
  dev/local. Production and tests enforce them fail-closed. Auth, password reset,
  public preview, upload, and other `always_on` limiters remain active.
- Startup logs include both `configured_user` and `effective_user`, plus the
  effective reason, so operators can distinguish the environment fallback from
  the raw flag.
