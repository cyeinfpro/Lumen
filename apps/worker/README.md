# Lumen Worker (arq)

## 启动

```bash
# 基础设施先起
docker compose up -d

# Worker
cd apps/worker
uv run python -m arq app.main.WorkerSettings
```

## 目录

- `app/main.py` — WorkerSettings（注册 functions / cron）
- `app/config.py` — pydantic-settings
- `app/db.py` — async SQLAlchemy session
- `app/storage.py` — 本地 fs 对象存储适配器
- `app/tasks/generation.py` — 文生图 / 图生图（DESIGN §6.5.b + §7）
- `app/tasks/completion.py` — chat / vision_qa（DESIGN §6.5.a）
- `app/tasks/outbox.py` — Transactional Outbox publisher + reconciler

图片生成由两个正交设置控制：`image.engine` 选择 `responses` / `image2` /
`dual_race`，`image.channel` 选择 `auto` / `stream_only` / `image_jobs_only`。
`auto` 会先选 Provider，再按该 Provider 的 `image_jobs_enabled` 决定走 image-job
异步任务还是流式路径；`stream_only` 强制走 responses 或 direct image2；
`image_jobs_only` 会在 Provider 不支持 image-job 时直接返回 503。

旧键 `image.primary_route` / `image.text_to_image_primary_route` 仍被 worker 作为
fallback 读取，方便平滑迁移；API 启动时会把旧值 backfill 到
`image.channel + image.engine`，但保留旧行用于回滚。

生成参数从 `Generation.upstream_request` 读取：`render_quality` 映射到上游
`quality`，`output_format/output_compression/background/moderation` 同时透传给 direct
Image API 和 responses image tool。Fast 只切换到 5.4 mini 模型，不传 reasoning，
也不改渲染质量。
默认输出 JPEG，JPEG/WebP 默认 `output_compression=0`
以接近 PNG 的保真度；透明背景请求会强制走 PNG 并保留 alpha。

流式期间只向前端发布轻量进度事件（fallback start / partial index / finalizing），不会把
partial/final base64 写入 Redis 或 SSE。详见 `responses-image-integration-guide.md`。
