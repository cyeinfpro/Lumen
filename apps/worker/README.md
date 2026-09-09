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

默认 `image.engine=image2`，`/responses` 生图关闭；直调和 image-job 失败均不会
自动启用 Responses。需要原生通道时在后台显式选择 `responses` 或 `dual_race`。
已有显式配置继续保留，升级后可在后台切换为 `image2`。

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
Image API 和 responses image tool。`model` 指定图片模型（GPT Image 2、2.5 Flare、
2.5 Sunburst），与 `responses_model` 主模型分开。两个 2.5 模型支持 `xhigh` / `max`；
GPT Image 2 仅支持 low / medium / high。聊天 Fast 不会影响图片任务。
默认输出格式由系统设置决定；JPEG/WebP 支持 `output_compression`。透明背景通过
OpenAI 原生 `background=transparent` 请求，使用 PNG 或 WebP 保留 alpha；若请求
JPEG，Lumen 会自动改用 PNG。

流式期间只向前端发布轻量进度事件（fallback start / partial index / finalizing），不会把
partial/final base64 写入 Redis 或 SSE。详见
[responses-image-integration-guide.md](../../docs/responses-image-integration-guide.md)。
