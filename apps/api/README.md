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
