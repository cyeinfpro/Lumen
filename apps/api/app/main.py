"""FastAPI 入口。路由分文件（auth / conversations / messages / tasks / images / events）。"""

from __future__ import annotations

import logging
import os
import faulthandler
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from lumen_core.context_window import warm_tiktoken
from lumen_core.constants import MAX_PROMPT_CHARS
from sqlalchemy import text

from .arq_pool import close_arq_pool, get_arq_pool
from .config import settings
from .db import SessionLocal, engine
from .observability import init_otel, init_sentry, setup_prometheus
from .ratelimit import _is_trusted_proxy
from .redis_client import get_redis
from .runtime_settings import migrate_image_primary_route


logger = logging.getLogger(__name__)


def _install_fault_dump_signal() -> None:
    """Let systemd watchdogs request Python stack dumps before restart."""
    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return
    try:
        faulthandler.register(sigusr1, file=sys.stderr, all_threads=True, chain=False)
    except (RuntimeError, ValueError, OSError):
        logger.debug("failed to register faulthandler signal", exc_info=True)


_install_fault_dump_signal()

# Why: hard cap on raw request body. 对齐 routes/images.py 的 MAX_BYTES（50 MiB）
# 再给 multipart 边界 / 任意 JSON 负载 10 MiB 余量。更大的请求在 handler 分配前就拒。
_MAX_REQUEST_BYTES = 60 * 1024 * 1024

_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"x-permitted-cross-domain-policies", b"none"),
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    ),
)
_HSTS_HEADER = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


def _request_is_https(scope) -> bool:  # type: ignore[no-untyped-def]
    if scope.get("scheme") == "https":
        return True
    remote = scope.get("client")
    remote_host = remote[0] if remote else None
    if not remote_host or not _is_trusted_proxy(remote_host):
        return False
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-proto":
            return value.decode("latin-1").split(",", 1)[0].strip().lower() == "https"
    return False


class _SecurityHeadersMiddleware:
    """Pure ASGI middleware: adds security headers without buffering the body.

    Avoids BaseHTTPMiddleware which would interfere with streaming responses
    (SSE / image binary).
    """

    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                existing = {h[0].lower() for h in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in existing:
                        headers.append((name, value))
                if _request_is_https(scope) and _HSTS_HEADER[0] not in existing:
                    headers.append(_HSTS_HEADER)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _BodySizeLimitMiddleware:
    """Pure ASGI middleware: rejects oversized requests before handlers read them."""

    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        cl: str | None = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                cl = value.decode("latin-1")
                break

        if cl is not None:
            try:
                if int(cl) > _MAX_REQUEST_BYTES:
                    response = JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content={
                            "error": {
                                "code": "request_too_large",
                                "message": "request body exceeds limit",
                            }
                        },
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "error": {
                            "code": "invalid_content_length",
                            "message": "invalid content-length header",
                        }
                    },
                )
                await response(scope, receive, send)
                return

        seen = 0
        rejected = False

        async def send_wrapper(message):  # type: ignore[no-untyped-def]
            if rejected:
                return
            await send(message)

        async def limited_receive():  # type: ignore[no-untyped-def]
            nonlocal seen, rejected
            # Once we've decided to reject, never feed more bytes back to the
            # downstream handler. Returning http.disconnect short-circuits any
            # further body assembly so a single oversized chunk cannot leak
            # into handler memory after the 413 has been sent.
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] != "http.request":
                return message
            body = message.get("body", b"")
            seen += len(body)
            if seen > _MAX_REQUEST_BYTES:
                rejected = True
                response = JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={
                        "error": {
                            "code": "request_too_large",
                            "message": "request body exceeds limit",
                        }
                    },
                )
                await response(scope, receive, send)
                return {"type": "http.disconnect"}
            return message

        await self.app(scope, limited_receive, send_wrapper)


def _is_prod_env() -> bool:
    return settings.app_env.strip().lower() not in {"dev", "development", "local", "test"}


async def _check_alembic_head() -> None:
    """Compare DB schema against alembic head; fail fast in prod, warn in dev.

    跳过条件：env LUMEN_SKIP_MIGRATION_CHECK=1，或 alembic 元数据不可用（开发树外的安装路径）。
    复用 db.engine（不开新连接池）。
    """
    if os.environ.get("LUMEN_SKIP_MIGRATION_CHECK", "").strip() in {"1", "true", "yes"}:
        return

    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
    except ImportError:
        logger.warning("alembic not installed; skipping migration head check")
        return

    alembic_ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    if not alembic_ini.is_file():
        logger.warning("alembic.ini missing at %s; skipping head check", alembic_ini)
        return

    try:
        cfg = Config(str(alembic_ini))
        cfg.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        head_revs = set(script.get_heads())

        async with engine.connect() as conn:
            current_revs = await conn.run_sync(
                lambda sync_conn: set(MigrationContext.configure(sync_conn).get_current_heads())
            )
    except Exception as exc:  # noqa: BLE001
        # connection / parsing failures: warn in dev, raise in prod so prod doesn't
        # silently start with unknown schema.
        if _is_prod_env():
            raise RuntimeError(
                f"alembic head check failed: {exc!r}; refusing to start"
            ) from exc
        logger.warning("alembic head check failed (non-prod, continuing): %s", exc)
        return

    if current_revs != head_revs:
        msg = (
            f"DB schema not at head: db={sorted(current_revs)} "
            f"alembic_head={sorted(head_revs)}; run `alembic upgrade head`"
        )
        if _is_prod_env():
            raise RuntimeError(msg)
        logger.warning(msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 观测层初始化（dsn/endpoint 为空即 no-op，不影响本地 dev）
    init_sentry(
        settings.sentry_dsn,
        settings.sentry_environment or settings.app_env,
        settings.sentry_traces_sample_rate,
    )
    init_otel(
        settings.otel_service_name,
        settings.otel_exporter_endpoint,
        app=app,
    )
    # Alembic 启动门禁：prod 必须在 head；非 prod 仅 warn；测试期通过 env 跳过。
    await _check_alembic_head()
    # 限流可观测性：明确记录限流是否启用，避免生产忘开等于无限流。
    is_rl_enabled = bool(getattr(settings, "user_rate_limit_enabled", False))
    if _is_prod_env() and not is_rl_enabled:
        logger.warning(
            "PRODUCTION MODE: user_rate_limit_enabled=False; "
            "non-always_on rate limiters are DISABLED. API is unprotected "
            "against brute-force on user-scoped endpoints.",
        )
    else:
        logger.info(
            "rate limiting status env=%s user_rate_limit_enabled=%s",
            settings.app_env,
            is_rl_enabled,
        )
    try:
        async with SessionLocal() as session:
            changed = await migrate_image_primary_route(session)
            if changed:
                await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("runtime settings image route migration failed", exc_info=True)
    # 提前建立 redis 连接（失败早暴露）
    r = get_redis()
    await r.ping()
    # 初始化 arq 入队池（与 Worker 注册的 run_generation / run_completion 对接）
    await get_arq_pool()
    # Opportunistic only: if tiktoken's cache is cold and the metadata download is
    # slow, token counting falls back to a local estimate instead of blocking API
    # request handlers.
    logger.info("api.tiktoken_warm loaded=%s", warm_tiktoken(timeout_sec=0.2))
    try:
        yield
    finally:
        await close_arq_pool()
        await r.aclose()


def _cors_allow_origins() -> list[str]:
    from urllib.parse import urlparse

    origins = [
        origin.strip()
        for origin in settings.cors_allow_origins.split(",")
        if origin.strip()
    ]
    if not origins:
        raise ValueError("CORS_ALLOW_ORIGINS must contain at least one origin")
    for origin in origins:
        parsed = urlparse(origin)
        if origin == "*" or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"invalid CORS origin: {origin}")
    return origins


def build_app() -> FastAPI:
    app = FastAPI(title="Lumen API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allow_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "X-CSRF-Token",
            "Authorization",
            "Last-Event-ID",
        ],
    )
    app.add_middleware(_BodySizeLimitMiddleware)
    app.add_middleware(_SecurityHeadersMiddleware)
    return app


app = build_app()


# ---------- 统一错误结构（DESIGN §5.8） ----------

def _wrap_error(
    code: str,
    message: str,
    http: int,
    details: dict[str, Any] | None = None,
    retry_after_ms: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    if retry_after_ms is not None:
        body["error"]["retry_after_ms"] = retry_after_ms
    return JSONResponse(status_code=http, content=body, headers=headers)


@app.exception_handler(HTTPException)
async def http_exc_handler(_req: Request, exc: HTTPException) -> JSONResponse:
    # Routes raise HTTPException(detail={"error": {...}}) to preserve structure.
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail and isinstance(detail["error"], dict):
        err = detail["error"]
        return _wrap_error(
            code=str(err.get("code", "http_error")),
            message=str(err.get("message", "")),
            http=exc.status_code,
            details=err.get("details"),
            retry_after_ms=err.get("retry_after_ms"),
            headers=getattr(exc, "headers", None),
        )
    # Fall back: raw string detail.
    return _wrap_error(
        code="http_error",
        message=str(detail) if detail is not None else "",
        http=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exc_handler(_req: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    for err in errors:
        loc = tuple(str(part) for part in err.get("loc", ()))
        if err.get("type") == "string_too_long" and loc and loc[-1] in {
            "text",
            "prompt",
        }:
            return _wrap_error(
                code="prompt_too_long",
                message=f"提示词不能超过 {MAX_PROMPT_CHARS} 字，请精简后再发送",
                http=status.HTTP_422_UNPROCESSABLE_ENTITY,
                details={"errors": errors, "max_chars": MAX_PROMPT_CHARS},
            )
    return _wrap_error(
        code="invalid_request",
        message="request validation failed",
        http=status.HTTP_422_UNPROCESSABLE_ENTITY,
        details={"errors": errors},
    )


@app.exception_handler(Exception)
async def unhandled_exc_handler(_req: Request, exc: Exception) -> JSONResponse:
    # Why: never leak internal exception details (stack traces, SQL errors,
    # secrets in repr) to clients. Log the full exception server-side instead.
    logger.exception("unhandled exception", exc_info=exc)
    return _wrap_error(
        code="internal_error",
        message="internal server error",
        http=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}


@app.get("/readyz")
async def readyz(
    redis: Any = Depends(get_redis),
) -> dict[str, str]:
    try:
        await redis.ping()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness check failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": {"code": "not_ready", "message": "dependency check failed"}},
        ) from exc
    return {"status": "ok"}


# 路由挂载
from .routes import auth, conversations, events, images, messages, tasks  # noqa: E402
from .routes import generations as generations_router  # noqa: E402

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(conversations.router, prefix="/conversations", tags=["conversations"])
app.include_router(messages.router, tags=["messages"])
# 静态路径 /generations/feed 必须先于 tasks.router 注册，否则会被 tasks 里的
# /generations/{gen_id} 通配路由吞掉（gen_id="feed" 查 DB → 稳定 404）。
app.include_router(generations_router.router, prefix="/generations", tags=["generations"])
app.include_router(tasks.router, tags=["tasks"])
app.include_router(images.router, prefix="/images", tags=["images"])
app.include_router(events.router, tags=["events"])

from .routes import admin as admin_router  # noqa: E402
from .routes import admin_backups as admin_backups_router  # noqa: E402
from .routes import admin_models as admin_models_router  # noqa: E402
from .routes import shares as shares_router  # noqa: E402
from .routes import me as me_router  # noqa: E402
from .routes import invites as invites_router  # noqa: E402
from .routes import providers as providers_router  # noqa: E402
from .routes import system_settings as system_settings_router  # noqa: E402
from .routes import system_prompts as system_prompts_router  # noqa: E402
from .routes import regenerate as regenerate_router  # noqa: E402
from .routes import prompts as prompts_router  # noqa: E402
from .routes import telegram as telegram_router  # noqa: E402
from .routes import admin_proxies as admin_proxies_router  # noqa: E402
from .routes import admin_telegram as admin_telegram_router  # noqa: E402
from .routes import admin_update as admin_update_router  # noqa: E402

app.include_router(admin_router.router)
app.include_router(admin_backups_router.router)  # /admin/backups
app.include_router(admin_models_router.router)  # /admin/models
app.include_router(shares_router.router_authed)
app.include_router(shares_router.router_public)
app.include_router(me_router.router)
app.include_router(invites_router.router_authed)  # /admin/invite_links
app.include_router(invites_router.router_public)  # /invite/{token}
app.include_router(providers_router.router)  # /admin/providers
app.include_router(system_settings_router.router)  # /admin/settings
app.include_router(system_prompts_router.router)
app.include_router(regenerate_router.router)  # /conversations/{cid}/messages/{mid}/regenerate
app.include_router(prompts_router.router)  # /prompts/enhance
app.include_router(telegram_router.router_me, tags=["telegram"])  # /me/telegram/*
app.include_router(telegram_router.router_bot, tags=["telegram"])  # /telegram/* (bot-token auth)
app.include_router(admin_proxies_router.router)  # /admin/proxies/*
app.include_router(admin_telegram_router.router)  # /admin/telegram/restart
app.include_router(admin_update_router.router)  # /admin/update

# Prometheus /metrics（路由挂载后）
if settings.metrics_enabled:
    setup_prometheus(app)
