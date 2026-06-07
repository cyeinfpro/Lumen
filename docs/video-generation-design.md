# Lumen 视频生成设计（第一期：火山 Seedance 2）

状态：设计中（已补齐接入位置、数据模型、Provider/Worker 引擎、计费/定价、API、前端、媒体服务、迁移、测试与上线闸门；实现前仍需按当天官方文档复核外部价格与参数）
日期：2026-06-04

## 1. 背景与目标

Lumen 现为 AI 图片生成平台。本设计为其增加**视频生成**能力，第一期接入火山方舟（火山引擎）**Seedance 2**，后续可扩展 Google Veo。

核心约束：视频生成是**长耗时异步**任务（提交 -> 轮询 -> 取结果，通常 1-5 分钟），且**单次成本远高于图片**。设计需在复用现有可靠基建的同时，不污染已经臃肿的图片生成代码（`apps/worker/app/tasks/generation.py` 已达 250KB）。

## 2. 第一期范围与非目标

**范围：**
- 文生视频（T2V）+ 图生视频（I2V，复用现有 `Image` 作首帧）。
- Provider：火山方舟 Seedance 2，**平台统一密钥 + 钱包计费**（不做 BYOK）。
- 入口：**独立页面**，**完全不挂会话**（不碰 `messages.py` / `conversations`）。
- 产物：原始 mp4 + poster jpg，支持浏览器 seek、历史列表、取消、状态恢复。

**非目标（将来再做）：**
- BYOK 用户自带火山密钥。
- 视频转码阶梯 / 多清晰度 / HLS（`VideoVariant` 表）。
- Google Veo 适配（接口预留，本期不实现）。
- 图片/视频统一灵感流（视频先在自己的页面里展示历史）。
- 视频公开分享、作品集混排、视频编辑/延展。

## 3. 整体架构：复用 vs 新增

原则：**复用基建、独立模型**。不把视频逻辑塞进同步的图片 provider 池或 `generation.py`。

**复用（不动或小幅扩展）：**

| 基建 | 位置 | 复用方式 / 必改点 |
|---|---|---|
| 异步派发 | `OutboxEvent` + `tasks/outbox.py` + `arq_job_id` | 新增 `kind="video_generation"`；同步扩展 kind 校验、job-name 映射、DLQ 测试，否则现有 publisher 会把未知 kind 打入 DLQ |
| 钱包计费 | `lumen_core/billing.py` 的 `hold`/`settle`/`release` | 创建时 hold，完成时 settle/release，`ref_type="video_generation"`，新增 worker 侧 video billing helper |
| 定价表 | `PricingRule`（scope/key/variant/unit -> price_micro） | 表结构可复用；但必须扩展 backend schema、前端 TS 类型和 BillingPanel，因为当前只允许 `image_size`/`chat_model` |
| 对象存储 | `apps/worker/app/storage.py` `LocalStorage` | 纯字节存储，存 mp4 + poster jpg；新增 video key 前缀 `u/{user_id}/v/{video_generation_id}/...` |
| 进度推送 | `sse_publish.py` + `/events` + `useSSE` | 复用 `task:{id}` channel；必须让 `/events` 的 task ownership 校验识别 `VideoGeneration` |
| 代理定义 | `lumen_core/providers.py` 的 proxy schema | 视频 provider 单独配置，但可引用同名 shared proxy definition |
| 运行时设置 | `SystemSetting` + `runtime_settings.py` | 新增 `video.enabled`、`video.providers`、`video.token_hold_estimates` 等 setting spec 和校验 |
| 媒体响应 | `routes/images.py` 的安全文件思路 | 不直接复用图片 helper；视频必须新增 Range(206/416) 解析和测试 |
| 余额缓存 | `billing_cache_state.py` / worker `flush_balance_cache_refreshes` | API hold 后失效；worker settle/release 后刷新缓存 |

**新增（独立边界）：**

| 模块 | 新文件/位置 | 职责 |
|---|---|---|
| 数据模型 | `packages/core/lumen_core/models.py` | 新增 `VideoGeneration` + `Video` 两表 |
| 状态/错误码枚举 | `lumen_core/constants.py` | 新增 `VideoGenerationStatus` / `VideoGenerationStage` / `VideoGenerationErrorCode`，不要复用图片枚举名 |
| 视频 Provider 配置 | `lumen_core/video_providers.py` | 解析 `video.providers`/env fallback、模型映射、proxy 引用、并发上限 |
| 视频计费 | `lumen_core/video_billing.py` + `apps/worker/app/video_billing.py` | 估价 hold、实际 usage settle、失败 settle/release 决策 |
| 上游适配 | `apps/worker/app/video_upstream.py` | 火山 Seedance submit/poll/fetch/cancel HTTP 封装；预留 `VideoProviderAdapter` |
| Worker 任务 | `apps/worker/app/tasks/video_generation.py` | 提交、短轮询、落库、poster/faststart、结算、reconcile |
| API 路由 | `apps/api/app/routes/videos.py` | 创建、查询、历史、取消、重试、媒体 binary/poster |
| 前端页面 | `apps/web/src/app/video/page.tsx` | 独立创作页 + 进度 + 播放器 + 历史 |
| 前端 API/types | `apps/web/src/lib/apiClient.ts` / `types.ts` | `VideoGeneration`/`Video` types、视频 URL helper、create/list/cancel/retry |
| DB 迁移 | `apps/api/alembic/versions/0026_video_generation.py` | 建两张新表、索引、默认价格/设置种子 |

## 4. 数据模型

`models.py` 内新增两张表，与 `Generation`/`Image` 平行，互不污染。字段使用字符串状态，避免 Postgres enum migration 复杂度。

### `VideoGeneration`（任务记录）

```
id                        uuid7 pk
user_id                   FK users(id) ON DELETE CASCADE
action                    "t2v" | "i2v"
model                     "seedance-2.0" | "seedance-2.0-fast" ...
provider_name             nullable str
provider_kind             "volcano" | "veo"
provider_task_id          nullable str

prompt                    text
input_image_id            nullable str/FK images(id) ON DELETE SET NULL
input_image_storage_key   nullable text      # I2V 创建时快照，避免用户删除首帧后 worker 无法读取
input_image_sha256        nullable str

duration_s                int
resolution                "720p" | "1080p"
aspect_ratio              "16:9" | "9:16" | "1:1" ...
fps                       nullable int
generate_audio            bool
seed                      nullable int
watermark                 bool

upstream_request          jsonb
upstream_response         jsonb
diagnostics               jsonb

status                    queued|submitting|submitted|running|succeeded|failed|canceled|expired
progress_stage            queued|submitting|rendering|fetching|storing|billing|finished
progress_pct              int 0..100
attempt                   int
poll_count                int
deadline_at               timestamptz
next_poll_at              timestamptz nullable
cancel_requested_at       timestamptz nullable
submitted_at              timestamptz nullable
started_at                timestamptz nullable
finished_at               timestamptz nullable

idempotency_key           str
request_fingerprint       str                # 参数 fingerprint，写 audit/diagnostics，辅助排查重复请求
est_token_upper           bigint
est_cost_micro            bigint
billed_tokens             bigint nullable
billed_cost_micro         bigint nullable
error_code                nullable str
error_message             nullable text
```

建议索引/约束：
- `UNIQUE (user_id, idempotency_key)`，与现有 `Generation` 一致。
- `UNIQUE (provider_kind, provider_name, provider_task_id)` where `provider_task_id IS NOT NULL`，避免重复落库同一上游任务。
- `INDEX (user_id, status, created_at)`，供历史/活跃任务列表。
- `INDEX (status, next_poll_at)`，供 reconcile/轮询恢复。
- `CHECK duration_s > 0`、`progress_pct BETWEEN 0 AND 100`、`est_cost_micro >= 0`。

### `Video`（产物）

```
id                        uuid7 pk
user_id                   FK users(id) ON DELETE CASCADE
owner_generation_id        FK video_generations(id) ON DELETE SET NULL
storage_key               text unique        # u/{user_id}/v/{vg_id}/output.mp4
poster_storage_key         text unique        # u/{user_id}/v/{vg_id}/poster.jpg
mime                      "video/mp4"
width                     int
height                    int
duration_ms               int
fps                       nullable float
size_bytes                bigint
sha256                    str
etag                      str
has_audio                 bool
faststart                 bool
visibility                "private"
metadata_jsonb            jsonb
deleted_at                nullable timestamptz
```

### YAGNI 决定（已确认）
- **不建 `VideoVariant` 转码阶梯表**：第一期只存原始 mp4 + 一张 poster。
- **poster 用 `poster_storage_key` 列，不建独立 `Image` 行**：更简单，不牵扯图片变体管线。
- **不挂会话**：没有 `message_id` / `conversation_id`，历史只按 `user_id` 列表展示。

## 5. Provider 适配与配置

### 适配器接口（`video_upstream.py`）

```python
class VideoProviderAdapter(Protocol):
    async def submit(req: VideoSubmitRequest) -> SubmitResult: ...
    async def poll(provider_task_id: str) -> PollResult: ...
    async def fetch_result(video_url: str) -> bytes: ...
    async def cancel(provider_task_id: str) -> CancelResult | None: ...

class PollResult:
    status: Literal["queued", "running", "succeeded", "failed", "cancelled", "expired"]
    progress: int | None
    video_url: str | None
    failure_class: str | None       # system | timeout | content_policy | invalid_input | capacity ...
    usage_total_tokens: int | None
    upstream_billable: bool | None
    raw: dict[str, Any]
```

第一期实现 `VolcanoSeedanceAdapter`（建任务 -> 查任务 -> 取 mp4 url）。将来加 `VeoAdapter` 时只新增 adapter 和 provider kind，引擎不变。

### 配置来源

新增 runtime setting `video.providers`，API/worker config 提供 `VIDEO_PROVIDERS` env fallback。格式与普通 provider pool 分开，因为火山视频 API 不是 OpenAI `/v1/responses` 形态。

```json
{
  "providers": [
    {
      "name": "volcano-main",
      "kind": "volcano",
      "base_url": "https://ark.cn-beijing.volces.com/api/v3",
      "api_key": "...",
      "enabled": true,
      "priority": 100,
      "weight": 1,
      "proxy": "sg-socks",
      "concurrency": 2,
      "models": {
        "seedance-2.0:t2v": "doubao-seedance-2-0-260128",
        "seedance-2.0:i2v": "doubao-seedance-2-0-260128",
        "seedance-2.0-fast:t2v": "doubao-seedance-2-0-fast-260128",
        "seedance-2.0-fast:i2v": "doubao-seedance-2-0-fast-260128"
      }
    }
  ]
}
```

`video_providers.py` 需要：
- 校验 `kind/base_url/api_key/models/enabled/concurrency`。
- 鉴权用 `Authorization: Bearer <api_key>`；不要实现 AK/SK。
- 解析共享 proxy：优先读 `video.providers.proxies`，缺失时可引用普通 `providers.proxies` 的同名项。
- 对 `(model, action)` 做映射；i2v/t2v 可能是不同 doubao model id。
- 对 Seedance 1.x/2.x 的参数编码差异做 adapter 内封装：2.0 用顶层 `ratio/resolution/duration/generate_audio`；1.x 用 text 内联参数。Worker 引擎不感知。

### HappyHorse / DashScope

Alibaba Cloud Model Studio 的 HappyHorse-1.0 接入使用独立 `dashscope` provider kind，不走火山 `contents/generations/tasks` 接口：

```json
{
  "providers": [
    {
      "name": "dashscope-happyhorse",
      "kind": "dashscope",
      "base_url": "https://dashscope-intl.aliyuncs.com",
      "api_key": "...",
      "enabled": true,
      "concurrency": 2,
      "models": {
        "happyhorse-1.0:t2v": "happyhorse-1.0-t2v",
        "happyhorse-1.0:i2v": "happyhorse-1.0-i2v",
        "happyhorse-1.0:reference": "happyhorse-1.0-r2v"
      }
    }
  ]
}
```

请求差异：
- 提交路径：`POST /api/v1/services/aigc/video-generation/video-synthesis`，Header 需要 `Authorization: Bearer ...` 和 `X-DashScope-Async: enable`。
- 轮询路径：`GET /api/v1/tasks/{task_id}`。
- I2V/R2V 输入图片必须是上游可访问的 HTTP(S) URL，不能发送 data URL；API 会为私有图片生成 `/api/images/reference/{image_id}/binary?token=...`。
- `happyhorse-1.0-r2v` 只支持图片参考；当前 Lumen 的 `reference` 动作如果带视频参考会在 API 层拒绝。
- 官方还有 `happyhorse-1.0-video-edit`；当前 Lumen 没有 video-edit 动作和 UI，不在本次接入范围内。

计费差异：
- 官方价格按输出视频秒计费，720P 原价 $0.14/s，1080P 原价 $0.24/s。
- Lumen 复用视频账本的 `per_mtoken` 单位：内部规定 `1 秒 = 1,000,000` 视频计费 token，`price_micro` 表示每秒人民币微元。
- 默认迁移按仓库 fallback 汇率 USD/CNY=7.2 写入：720p `1_008_000` micro/s，1080p `1_728_000` micro/s；运营手动改价不被覆盖。

### 功能闸门

API 创建任务前必须同时满足：
- `video.enabled=1`。
- 至少一个 enabled video provider 支持请求的 `(model, action)`。
- 存在 enabled `PricingRule(scope="video", key=model, variant=action, unit="per_mtoken")`。
- 存在对应 hold 上界估算（见 §8），且钱包余额足够。

缺失 provider/价格/估算时返回 503 或 422，不创建任务、不 hold、不提交上游。

## 6. Worker 引擎：自重排轮询

采用**自重排轮询**而非一个 sleep 几分钟的长任务：重启安全、不长期占用 worker slot。

### API 创建事务

```
POST /videos/generations:
    校验 prompt/参数/首帧 ownership
    估算 token 上界 + hold 金额
    billing.hold(ref_type="video_generation", ref_id=vg.id, idempotency_key=f"video_generation:hold:{vg.id}")
    INSERT VideoGeneration(status=queued, ...)
    INSERT OutboxEvent(kind="video_generation", payload={task_id,user_id,outbox_id})
    commit
    post-commit: invalidate_balance_cache(user_id) + best-effort enqueue/SSE video.queued
```

注意：视频完全不挂 `message_id`，不能复用 `routes/tasks.py` 里要求 `message_id` 的 `_publish_queued`。需要在 `routes/videos.py` 写视频专用 helper。

### Outbox 必改点

当前 `tasks/outbox.py` 只接受 `generation`/`completion`，未知 kind 会被 DLQ。视频实现必须同步改：
- 允许 `ev_kind in {"generation", "completion", "video_generation"}`。
- `video_generation -> run_video_generation`。
- `_job_id=arq_job_id("video_generation", task_id, outbox_id)`。
- 单测覆盖合法 video event、malformed payload、重复 enqueue dedupe、DLQ 仍工作。

### Worker 流程

```
run_video_generation(ctx, task_id):
    获取 lease（video 专用 lease key，带 CAS 释放）
    若 status terminal -> return
    若 cancel_requested 且未 submit -> cancel + release hold
    若 provider_task_id 已存在 -> 不重复 submit，直接 enqueue poll
    选择 provider + acquire provider concurrency slot
    adapter.submit() -> 保存 provider_task_id/status=submitted/submitted_at
    publish video.submitted
    enqueue_job("run_video_poll", task_id, _defer_by=POLL_INTERVAL, _job_id=...)

run_video_poll(ctx, task_id):
    获取短 lease
    若 canceled terminal -> return
    adapter.poll(provider_task_id)
    queued/running -> 更新 progress/poll_count/next_poll_at + SSE；未超时则自重排
    succeeded -> fetch_result(video_url) -> faststart/poster/metadata -> 写 Video -> settle -> SSE video.succeeded
    failed/expired/cancelled/timeout -> resolve_video_billing() -> settle/release -> SSE video.failed/video.canceled
```

**护栏：**
- `POLL_INTERVAL` 约 5-10s；硬超时约 10min；`max_poll_count`。
- per-provider 并发上限，不占用图片 provider pool 的 `image_concurrency`。
- `video_url` 24h 过期、任务历史有限：成功后**立即** fetch 落库；reconcile 只处理仍在官方历史窗口内的任务。
- `callback_url` 可选：Seedance 支持 webhook 时可短路轮询，但仍保留自重排轮询 + reconcile 作可靠兜底。
- Worker shutdown 需要释放 provider concurrency slot、lease；不得在 `CancelledError` 中跳过账务/slot 清理。

### 取消语义

取消必须显式由 `POST /videos/generations/{id}/cancel` 发起，SSE 断开不取消任务。

- 未提交上游：标记 `canceled`，release hold。
- 已提交上游且 provider 支持 cancel：调用 cancel，再继续 poll 到 terminal 或确认未计费。
- 已提交上游但无法确认是否扣费：按纯转嫁原则默认 settle（通常 settle 到 hold 金额），`diagnostics.billing_decision="unknown_default_charge"`。
- cancel 不删除已生成的 `Video`；若成功产物已落库，任务保持 `succeeded`，前端可展示“取消已晚于完成”。

### 重启安全

新增 `reconcile_video_tasks` cron（挂到 `apps/worker/app/main.py` 的 `cron_jobs`）：
- 扫 `status in ('queued','submitting','submitted','running')` 且 `updated_at/next_poll_at` 超时的 `VideoGeneration`。
- 没有 `provider_task_id` 的 queued/submitting 任务重新 enqueue `run_video_generation`。
- 有 `provider_task_id` 的 submitted/running 任务重新 enqueue `run_video_poll`。
- 超过 `deadline_at`：poll 一次拿最终 usage；仍无法确认时走 `unknown_default_charge`。

## 7. 计费：纯转嫁，平台绝不吸收上游成本

**核心原则：平台是成本转嫁方，绝不替用户吃掉昂贵的上游视频费用。** 只要火山扣了平台的钱，用户就必须付；只有火山没扣时才退给用户。

1. **创建时足额预扣 hold（从严）。** 视频价格由 `model x action x 参数 token 上界` 决定。hold 在**提交上游之前**完成。余额不够 -> 402，且不创建可执行任务。
2. **无 secured hold 绝不 submit 上游。** hold 在 API，submit 在 worker，顺序天然满足；worker 仍需防御性检查 hold 是否存在。
3. **结算由上游结果驱动**（`resolve_video_billing(poll_result, hold)`）：
   - 成功且返回 `usage.total_tokens` -> 按实际 tokens settle，释放差额。
   - 成功但无 usage -> 按 hold settle，写 `missing_usage_default_charge`。
   - 失败且返回 usage 或明确 billable -> settle。
   - 失败且明确 not billable -> release。
   - 失败且判不准是否扣费 -> 默认 settle 到 hold 金额，写 `unknown_default_charge`。
4. **提交前校验降低纠纷。** prompt/参数/首帧尺寸/类型先在 API 层挡住，避免可预防的计费型失败。

钱包交易约定：
- `ref_type="video_generation"`，`ref_id=video_generation.id`。
- hold key: `video_generation:hold:{id}`。
- settle key: `video_generation:settle:{id}`。
- release key: `video_generation:release:{id}`。
- `WalletTransaction.meta` 写入 `model/action/resolution/duration_s/estimated_tokens/actual_tokens/provider_name/provider_task_id/billing_decision`。
- API hold 后调用 `invalidate_balance_cache(user_id)`；worker settle/release 后复用 `flush_balance_cache_refreshes(session)` 模式。

## 8. 定价模型：token 计费 + 后台自定义价格

Seedance 按 token 计费，且 token 数通常在完成后由 `usage.total_tokens` 返回。因此视频走 completion 式「估价 hold -> 实际 settle」，而非图片的「固定档位一次扣清」。

### PricingRule 编码

表结构不改，但 schema/UI 必须扩展。

| 列 | 取值 | 说明 |
|---|---|---|
| `scope` | `"video"` | 与 `image_size` / `chat_model` 并列 |
| `key` | model，如 `"seedance-2.0"` | 用户可选模型名，不直接暴露 provider model id |
| `variant` | `"t2v"` \| `"i2v"` | 区分文生/图生；后续可加 `"v2v"` |
| `unit` | `"per_mtoken"` | 每百万 token 平台售价 |
| `price_micro` | 平台售价，单位 micro RMB | 含利润，后台可调 |

必须同步改：
- `packages/core/lumen_core/schemas.py`：`PricingRuleOut.scope` / `PricingRuleUpsertIn.scope` 加 `"video"`，`PricingUnit` 加 `"per_mtoken"`。
- `apps/web/src/lib/types.ts`：同样加 `"video"` / `"per_mtoken"`。
- `apps/web/src/app/admin/_panels/BillingPanel.tsx`：新增“视频定价”区域（模型、T2V 单价、I2V 单价、enabled/note），不能只依赖现有图片/对话两个面板。
- `apps/api/alembic/versions/0026_video_generation.py`：种子默认视频价格规则。默认值应高于上游成本，并在 note 标明“需按火山最新价格复核”。

### 估价与结算

新增 `lumen_core/video_billing.py`：

```
estimate_video_cost(db, *, model, action, resolution, duration_s, fps, generate_audio)
    -> VideoCostEstimate(estimated_tokens, hold_micro, unit_price_micro, source)

settle_video_cost(db, *, model, action, actual_total_tokens)
    -> actual_micro
```

金额计算使用整数或 Decimal，保持 `ROUND_HALF_UP`：

```
actual_micro = round_half_up(actual_total_tokens * price_micro / 1_000_000)
```

hold token 上界不要猜闭式公式，走保守配置表：

```json
{
  "seedance-2.0": {
    "t2v": {"720p:5": 60000, "1080p:5": 130000, "1080p:10": 280000},
    "i2v": {"720p:5": 60000, "1080p:5": 130000, "1080p:10": 280000}
  }
}
```

`video.token_hold_estimates` 缺项时，API 返回 422/503，不提交上游。上线初期可刻意偏高，完成后按实际 usage settle 释放差额。

## 9. API 设计

路由文件：`apps/api/app/routes/videos.py`，在 `main.py` 注册 `target.include_router(videos.router)`。

### Schema

```python
class VideoCreateIn(BaseModel):
    action: Literal["t2v", "i2v"]
    model: str
    prompt: str
    input_image_id: str | None = None
    duration_s: int
    resolution: Literal["720p", "1080p"]
    aspect_ratio: str
    fps: int | None = None
    generate_audio: bool = False
    seed: int | None = None
    idempotency_key: str

class VideoGenerationOut(BaseOut):
    id: str
    action: str
    model: str
    prompt: str
    status: str
    progress_stage: str
    progress_pct: int
    est_cost: MoneyOut
    billed_cost: MoneyOut | None
    video: VideoOut | None
    error_code: str | None
    error_message: str | None

class VideoOut(BaseOut):
    id: str
    url: str
    poster_url: str | None
    width: int
    height: int
    duration_ms: int
    has_audio: bool
```

### Endpoints

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/videos/options` | 返回 enabled、可用模型/参数、价格、hold estimate；前端初始化用 |
| `POST` | `/videos/generations` | 创建 T2V/I2V；CSRF；幂等；hold；outbox |
| `GET` | `/videos/generations/{id}` | 查任务详情，owner check |
| `GET` | `/videos/generations` | 历史/活跃列表，cursor pagination，支持 `status` filter |
| `POST` | `/videos/generations/{id}/cancel` | 显式取消；CSRF |
| `POST` | `/videos/generations/{id}/retry` | 复制原参数创建新任务和新 hold；不复用旧 `provider_task_id` |
| `DELETE` | `/videos/{video_id}` | soft delete 视频产物；不删除账务 |
| `GET` | `/videos/{video_id}/binary` | mp4，owner check，Range 支持 |
| `GET` | `/videos/{video_id}/poster` | poster jpg，owner check |

### API 细节

- I2V 必须校验 `input_image_id` 属于当前用户且未 soft delete；创建时快照 `storage_key/sha256`。
- T2V 禁止传 `input_image_id`；I2V 必须传。
- 参数限制：prompt 长度、duration/resolution/aspect_ratio/fps/generate_audio 必须由 `video.options` 返回的 allowlist 驱动。
- 幂等：同一 `user_id + idempotency_key` 命中旧 row 时返回旧 `VideoGenerationOut`，不得重复 hold/outbox。
- 价格缺失、估算缺失、provider 缺失都在 API 层失败，不进 worker。
- `GET /events?channels=task:{video_generation_id}` 的 owner 校验必须查 `VideoGeneration`。

## 10. 前端设计

页面：`apps/web/src/app/video/page.tsx`。第一屏就是视频创作工作台，不做营销落地页。

核心组件：
- 模式 segmented control：`文字生成` / `首帧生成`。
- Prompt 输入区：沿用语义 token，不用硬编码暗色。
- I2V 首帧：复用 `/images/upload` 上传入口，并支持从已有图片历史选择。
- 参数控件：模型、时长、比例、分辨率、音频、seed；全部来自 `/videos/options`。
- 价格预估：显示 hold 金额、预计 token 上界、完成后按实际 usage 退还差额。
- 提交按钮：余额不足/功能关闭/provider 缺失时禁用并显示明确错误。
- 活跃任务区：订阅 `task:{id}`，显示 queued/submitted/running/fetching/billing/succeeded/failed。
- 播放器：原生 `<video controls preload="metadata" poster={poster_url}>`，使用 `video.url`。
- 历史列表：cursor pagination；支持重新生成、复制 prompt、删除。

SSE 事件名：
- `video.queued`
- `video.submitted`
- `video.progress`
- `video.fetching`
- `video.succeeded`
- `video.failed`
- `video.canceled`

事件 payload 至少包含：

```
{
  "video_generation_id": "...",
  "kind": "video_generation",
  "status": "running",
  "stage": "rendering",
  "progress_pct": 42,
  "video_id": null,
  "error_code": null,
  "event_id": "..."
}
```

前端实现点：
- `apps/web/src/lib/apiClient.ts` 增加 video API helper 和 `videoBinaryUrl` / `videoPosterUrl`。
- `apps/web/src/lib/types.ts` 增加 video types。
- `useSSE` 可直接复用，但页面挂载时要动态注册上面的视频事件名。
- 任务完成后以 `GET /videos/generations/{id}` 为最终 truth，SSE payload 只作为实时更新。

## 11. 播放与媒体服务

**前提：落库后只服务自家 `LocalStorage` 的 mp4。** 上游 `video_url` 是签名 URL，绝不直接交给前端。

必须实现：
- **HTTP Range（206 Partial Content）**：`GET /videos/{id}/binary` 支持单 range，返回 `Accept-Ranges: bytes`、`Content-Range`、正确 `Content-Length`；非法 range 返回 416。
- **poster 首帧**：`poster_storage_key` 作 `<video poster>`，列表和播放前不空白。
- **`preload="metadata"`**：先取时长/尺寸，不预载整片。
- **faststart（moov atom 前置）**：落库时检测；非 faststart 才 `ffmpeg -movflags +faststart`，只搬运 moov、不重编码。
- **不可变缓存**：完成的视频内容不变 -> `Cache-Control: private, max-age=31536000, immutable` + `ETag`/`Last-Modified`。
- **安全文件访问**：沿用 `_fs_path` 防穿越思路；不能按用户传入 key 直接开文件。

实现注意：
- 图片 `_storage_streaming_response` 没有 Range 解析，视频应新增 `video_media_response()`，并写独立单测。
- 若 `LUMEN_INTERNAL_REDIRECT_ENABLED=1`，可以让 nginx `X-Accel-Redirect` 处理大文件；但 Python fallback 必须完整支持 Range。
- Worker 镜像需要 `ffmpeg`/`ffprobe`。若生产镜像未安装，任务应仍可存原始 mp4，但 `faststart=false`、poster 生成失败要明确写 diagnostics；上线前应把 ffmpeg 加入镜像并测试。

## 12. 外部依赖核对（火山官方文档）

已按 2026-06-04 的公开官方文档方向核对，关键事实如下；实现前仍需再查当天文档/控制台，因为模型 id、价格和可选参数可能变化。

参考入口：
- 火山方舟模型价格：https://www.volcengine.com/docs/82379/1099320
- Seedance 2.0 系列模型资源包使用规则：https://www.volcengine.com/docs/82379/2191775
- BytePlus/ModelArk Video generation API create：https://docs.byteplus.com/en/docs/modelark/1520757
- BytePlus/ModelArk Video generation API retrieve：https://docs.byteplus.com/en/docs/modelark/1521309
- BytePlus/ModelArk Video generation API cancel/delete：https://docs.byteplus.com/en/docs/modelark/1521720

**已确认的设计依赖：**
- Base URL / 端点：国内走 `https://ark.cn-beijing.volces.com/api/v3`；建任务 `POST /contents/generations/tasks`；查任务 `GET /contents/generations/tasks/{task_id}`。
- 鉴权：`Authorization: Bearer <ARK_API_KEY>` + JSON body。是 Bearer API Key，不是 AK/SK 签名。
- 请求体：`model` + `content` 数组；content 支持 text 和 image_url，I2V 使用 first_frame/reference_image 等角色。
- 模型 id 需要按控制台/文档映射；i2v/t2v 可能不是同一个上游 id。
- 任务状态含 `queued / running / succeeded / failed / cancelled / expired`；内部统一映射为 `canceled` 单 L。
- 产物为 `content.video_url`，成功后要立即 fetch 落库。
- 用量字段包含 `usage.completion_tokens` / `usage.total_tokens`；settle 以 `usage.total_tokens` 为真值。
- Seedance 支持 `callback_url`，但本期只作为可选优化，不能替代轮询/reconcile。

**仍需 console/工单最终确认（设计已对其鲁棒）：**
- 失败是否计费的逐字 FAQ。设计不依赖猜测，按返回 usage/billable 决策；不明确默认收费。
- 精确 token 公式。设计不依赖闭式公式，hold 用保守上界表，settle 用实际 usage。
- 输出 mp4 是否已 faststart。落库时检测，必要时搬 moov。
- `generate_audio`、`watermark`、`return_last_frame` 等参数在目标模型上的可用性。

## 13. 迁移与实施顺序

按以下阶段落地，避免一次改太多导致账务/媒体/前端互相遮蔽问题：

1. **Schema/迁移基础**
   - models + alembic 0026。
   - constants/video schemas。
   - runtime setting specs：`video.enabled=0` 默认关闭、`video.providers`、`video.token_hold_estimates`。
   - Pricing schema/types 支持 `scope="video"` / `unit="per_mtoken"`。

2. **后台配置与定价**
   - BillingPanel 增加视频定价区域。
   - 可选：ProvidersPanel 暂不混入视频 provider，先用 SystemSetting JSON；后续再做结构化 UI。
   - 默认价格种子 + 管理端 upsert/list 测试。

3. **API 创建/查询/媒体**
   - `routes/videos.py` create/list/get/cancel/retry。
   - `/events` task ownership 支持 `VideoGeneration`。
   - video binary/poster endpoint + Range tests。

4. **Worker + fake adapter**
   - `video_upstream.py` protocol + fake adapter tests。
   - `tasks/video_generation.py` submit/poll/fetch/storage/billing。
   - outbox kind 扩展 + worker main 注册 + reconcile cron。

5. **Volcano adapter**
   - request serialization、poll mapping、usage parsing、error classification。
   - provider concurrency、proxy、timeout。
   - 成功落库、poster、faststart。

6. **前端页面**
   - `/video` 页面、apiClient/types、SSE 事件、历史/播放器。
   - 移动端布局与主题 token 检查。

7. **端到端验收**
   - 本地 fake provider 端到端。
   - 真实火山小参数 smoke（只在明确配置和预算允许时跑）。
   - 根据用户要求再进入正式 release flow。

## 14. 测试矩阵

后端单测：
- `video_providers`：配置校验、模型映射、proxy 引用、enabled/provider 缺失。
- `video_billing`：hold 估算、实际 token settle、缺 pricing、缺 estimate、ROUND_HALF_UP。
- `routes/videos.py`：T2V/I2V 创建、首帧 ownership、幂等 replay、不足余额 402、功能关闭、取消、retry 新 hold。
- `/events`：`task:{video_generation_id}` owner 校验通过；别人任务 403。
- 媒体端点：完整文件 200、Range 206、非法 Range 416、ETag 304、poster 404/200。

Worker 单测：
- Outbox `video_generation` enqueue 到 `run_video_generation`。
- submit 幂等：已有 `provider_task_id` 不重复提交。
- poll 自重排：running 更新 progress + enqueue 自己。
- succeeded：fetch -> storage -> Video row -> settle -> SSE。
- failed/canceled/expired：usage present settle；明确 not billable release；unknown default charge。
- reconcile：queued/submitted/running 任务恢复；deadline 超时走 billing decision。
- cancel race：cancel 与 success 同时发生时以已落库成功为准，不重复 release/settle。

前端检查：
- `npm run type-check`、`npm run lint`、`npm run build` from `apps/web`。
- 页面在 desktop/mobile 下 prompt/参数/价格/历史不重叠。
- `<video>` 能 seek；poster 显示；任务完成后历史刷新。

全仓库/发布前：
- `git diff --check`。
- `bash scripts/test.sh -q`（实现完成后）。
- 若用户要求“提交/推送/发布/更新”，按本仓库正式 release 规则：VERSION -> sync/check -> commit -> push main -> tag -> 等 tag-triggered Docker Release 成功。

## 15. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 上游失败是否计费不透明 | `usage/billable` 驱动；不确定默认收费；diagnostics 留证 |
| 价格/模型文档变化 | 实现前再查官方文档；价格后台可改；默认 seed note 标明需复核 |
| 视频 URL 过期导致丢产物 | 成功后立即 fetch；reconcile 只作为短窗口兜底 |
| 重复 submit 造成重复扣费 | `provider_task_id` 持久化；submit 幂等；Outbox stable job id；`UNIQUE provider_task_id` |
| Worker 重启丢轮询 | 自重排短任务 + reconcile cron |
| Range/大文件拖垮 API | nginx internal redirect 可选；Python fallback 支持 range；缓存不可变 |
| 用户取消后平台仍被扣费 | 取消语义明确：已提交上游按 usage/billable/unknown 结算 |
| 前端 schema 接入导致 admin pricing 崩 | 先扩 `PricingRule` schema/types，再 seed video pricing |
| ffmpeg 缺失 | 镜像加依赖；缺失时 diagnostics 明确，测试覆盖 |

## 16. 关键设计决策记录

- **复用基建 + 独立模型（混合）**，而非塞进现有图片管线（API 形态不同、`generation.py` 已 250KB）或完全另起炉灶（浪费可靠基建）。
- **视频完全解耦会话**（无 message_id），独立页面、独立历史。
- **自重排轮询**而非长 sleep 任务（重启安全）。
- **计费纯转嫁**，平台绝不吸收上游成本（视频很贵）。
- **token 计费走「估价 hold -> 实际 settle」**，因为 Seedance 按 token 计费、token 完成后才知道。
- **以返回的 `usage` 为计费真值**；失败/取消不明确时默认收费并写 diagnostics。
- **价格后台可自定义，但不是零改动复用**：必须扩 `PricingRule` schema/types 和 BillingPanel。
- **播放尽力优化**（Range/poster/faststart/immutable 缓存），但只服务自家落库副本。
