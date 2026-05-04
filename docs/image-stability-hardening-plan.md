# Image Stability Hardening Plan

## 背景

sub2api `v0.1.122` 的 OpenAI 请求处理更新，核心不是换模型或换端点，而是把网关场景里常见的“不标准但真实存在”的返回形态兜住：

- 请求声明 `stream=true`，但上游返回普通 JSON
- 上游声明 SSE，但 body 实际更接近完整 JSON 或混合事件
- 流式响应没有按预期 EOF，而是通过 terminal event 结束
- 客户端断开后，上游仍可能继续产出 usage 或最终事件
- OpenAI-compatible 上游不一定支持 `/v1/responses`

Lumen 当前已经有较完整的生图稳定性基础：

- direct image path：`/v1/images/generations` 与 `/v1/images/edits`
- fallback path：`/v1/responses` + `image_generation`
- provider failover、image circuit breaker、rate limit cooldown
- curl 与 httpx 两条异构 SSE 路径
- retry cache buster、partial image 控制、大图 read timeout 分级

本计划只记录对 Lumen 生图稳定性有实际收益的增强项，不包含 raw `/v1/chat/completions` fallback 这类对生图链路收益较低的内容。

## 目标

1. 减少“上游已经成功或已给出明确失败原因，但 Lumen 误判为断流/无图”的情况。
2. 提升对 OpenAI-compatible 网关返回形态差异的容错能力。
3. 让 provider 的真实端点能力参与路由选择，减少无效尝试。
4. 保留现有 direct image 主路径，不做大规模架构切换。

## 优先级 P0：统一 Responses Terminal Event

### 问题

Lumen 的 responses 生图 fallback 和非流式 `responses_call` 主要以 `response.completed` 作为正常终止信号。

相关位置：

- `apps/worker/app/upstream.py::_responses_image_stream`
- `apps/worker/app/upstream.py::responses_call`
- `apps/worker/app/upstream.py::_maybe_record_usage_from_event`

但兼容网关可能返回：

- `response.completed`
- `response.done`
- `response.incomplete`
- `response.failed`
- `error`

如果只认 `response.completed`，就可能出现两类误判：

- 实际已经 terminal，但 Lumen 继续等 EOF 或最后报 missing completed
- 上游已给出 failed/incomplete details，但最终错误只表现为 drained without image

### 建议实现

在 `apps/worker/app/upstream.py` 增加统一 helper：

```python
_RESPONSES_SUCCESS_TERMINAL_EVENTS = {"response.completed", "response.done"}
_RESPONSES_ERROR_TERMINAL_EVENTS = {"response.failed", "response.incomplete", "error"}
_RESPONSES_TERMINAL_EVENTS = (
    _RESPONSES_SUCCESS_TERMINAL_EVENTS | _RESPONSES_ERROR_TERMINAL_EVENTS
)
```

并提供两个小函数：

```python
def _is_responses_success_terminal(event_type: Any) -> bool:
    return isinstance(event_type, str) and event_type in _RESPONSES_SUCCESS_TERMINAL_EVENTS


def _is_responses_error_terminal(event_type: Any) -> bool:
    return isinstance(event_type, str) and event_type in _RESPONSES_ERROR_TERMINAL_EVENTS
```

改造点：

- `_responses_image_stream`
  - `response.completed` 与 `response.done` 都触发 `completed` progress。
  - `response.failed` / `response.incomplete` / `error` 统一提取 error payload。
  - 如果已有 `final_b64`，成功 terminal 缺失不应导致失败。
- `responses_call`
  - SSE 模式下遇到 success terminal，返回其中的 `response` dict。
  - 遇到 error terminal，提取 error/incomplete details 后抛 `UpstreamError`。
  - `[DONE]` 之前没有 success terminal 时，错误信息包含 `last_event_type`。
- `_maybe_record_usage_from_event`
  - 不只在 `response.completed` 上做未知 output item warning；`response.done` 也可按同一逻辑处理。

### 验收标准

- `response.done` + `response` payload 能被当成成功结束。
- `response.incomplete` 能抛出包含 `incomplete_details` 的 `UpstreamError`。
- `[DONE]` 但没有 terminal event 时，错误日志包含 `last_event_type`。
- 已提取到 `final_b64` 的图像流，不因缺少 `response.completed` 被误判失败。

## 优先级 P0：流式生图支持 JSON Fallback

### 问题

sub2api 这次的关键修复之一是：即使请求里声明了 `stream=true`，也只在上游响应头确认为 `text/event-stream` 时按 SSE 处理。否则按普通 JSON 处理。

Lumen 的 `responses_call` 已经按 `Content-Type` 分流，但 `_responses_image_stream` 的 fallback 路径默认走 SSE iterator：

- `_iter_sse_curl`
- `_iter_sse_with_runtime`

如果某个兼容网关在 `stream=true` 时返回 `application/json`，当前路径容易表现为 SSE 解析失败或 drained without image。

### 建议实现

优先改 httpx 路径，curl 路径可以随后补：

1. 新增一个专用于 responses image fallback 的 iterator 或 request helper。
2. 发起请求后先检查 `Content-Type`。
3. `text/event-stream` 继续走现有 SSE 解析。
4. 非 SSE 时读取完整 body，按 JSON payload 提取图像和 usage。

JSON payload 图像提取应复用或扩展 `_extract_response_image_b64`，支持：

- Responses object：`output[]`
- Responses event-like object：`response.output[]`
- Image API object：`data[].b64_json`
- URL result：`data[].url`，先记录可见错误或后续再决定是否下载

### 验收标准

- `stream=true` 请求收到 `application/json` 且 body 含 `data[0].b64_json` 时，能成功返回图片。
- `stream=true` 请求收到 `application/json` 且 body 含 Responses `output` 图片时，能成功返回图片。
- JSON body 非对象或无图时，错误包含 `content_type` 与 body 摘要。
- SSE 原路径行为不变。

## 优先级 P1：增强图像结果与用量提取

### 问题

不同 OpenAI-compatible 网关对图片结果和用量的字段命名差异较大。sub2api 的做法是对多种路径做宽容解析，用于 usage 和 billable image count。

Lumen 当前 `_record_usage` 主要记录 token usage，对 image count 没有专门口径。生图结果提取已有基础，但可以继续扩展，减少上游形态差异带来的无图误判。

### 建议实现

新增两个 helper：

```python
def _extract_image_b64_from_payload(payload: Any) -> str | None:
    ...


def _extract_image_billable_count(payload: Any) -> int | None:
    ...
```

建议支持路径：

- `data[].b64_json`
- `data[].url`
- `response.output[].result`
- `response.output[].content[].result`
- `output[].result`
- `output[].content[].result`
- `response.output_item.done.item.result`
- `usage.images`
- `tool_usage.image_gen.images`

注意事项：

- URL 图片是否下载应单独评估。短期可以只记录 URL result detected，仍要求 b64 才进入现有存储链路。
- image count 先用于日志和 metrics，不直接改变计费逻辑，避免账务口径突然变化。

### 验收标准

- 兼容 Image API JSON 的 `data[].b64_json`。
- 兼容 Responses final object 的 `output[].result`。
- 兼容 event wrapper 的 `response.output[]`。
- 对 `usage.images` / `tool_usage.image_gen.images` 有日志或 metrics。

## 优先级 P1：失败事件上下文结构化

### 问题

当前 Lumen 已经记录了 `last_event_type`、`partial_count` 和部分 upstream error，但失败上下文还可以更结构化。生图问题排查时，最有价值的是快速区分：

- 上游策略拒绝
- quota / rate limit
- provider 不支持当前 endpoint
- SSE 中断
- schema 不兼容
- 已出 partial，但 final 没到
- 已拿到 final，但 terminal event 异常

### 建议实现

在 `_responses_image_stream` 的失败路径统一生成 safe diagnostic dict：

```python
diagnostic = {
    "action": action,
    "size": size,
    "quality": image_quality,
    "provider": provider_name,
    "endpoint": "responses:image_generation",
    "last_event_type": last_event_type,
    "partial_count": partial_count,
    "has_final_image": final_b64 is not None,
    "trace_id": call_trace_id,
    "x_request_id": x_request_id,
    "upstream_error": safe_upstream_error,
}
```

改造点：

- `_iter_sse_with_runtime` 和 `_iter_sse_curl` 已经收集 response headers，可把 `x-request-id` 更容易地传回上层。
- 如果不想调整 iterator 返回类型，可以先在日志层补 trace_id，并保留 x-request-id 在 `_log_upstream_call`。

### 验收标准

- 所有 `responses fallback drained without image` 日志都带 `trace_id`。
- failed/incomplete/error 帧中的错误信息不会丢。
- provider failover 日志能看到失败 provider、失败 endpoint 和 reason。

## 优先级 P2：Provider Capability 参与路由

### 问题

Lumen 现在有 provider health probe、image probe 和 endpoint lock，但 endpoint 支持能力更多是“配置假设 + 运行时失败学习”。如果某 provider 天然不支持 `/v1/responses` 或 `/v1/images/generations`，auto 模式仍可能先尝试一次再失败。

sub2api 的借鉴点是：把能力探测结果变成路由条件，而不只是健康状态。

### 建议数据模型

在 provider 配置里增加可选能力字段：

```json
{
  "responses_supported": null,
  "image_generations_supported": null,
  "image_responses_supported": null
}
```

语义：

- `true`：确认支持
- `false`：确认不支持，路由时排除
- `null` 或缺失：未知，保持现有行为，避免破坏旧配置

涉及文件：

- `packages/core/lumen_core/providers.py`
- `packages/core/lumen_core/schemas.py`
- `apps/api/app/routes/providers.py`
- `apps/worker/app/provider_pool.py`

### 探测策略

Responses capability：

- `POST /v1/responses`
- 404 / 405 可判定为 unsupported
- 401 / 403 不应判定 unsupported，应归为认证或权限问题
- 429 / 5xx 不应写死 unsupported，只记临时 unhealthy

Image generations capability：

- `POST /v1/images/generations`
- 可用最小低质量请求，或先做轻量 schema probe。
- 如果成本不可接受，可以先不自动探测，只支持管理员手动标记。

Image responses capability：

- 复用现有 image probe，但默认仍关闭，避免昂贵自动探测。
- 可以在管理员手动 probe 时写入结果。

### 路由改造

在 provider selection 前增加能力过滤：

- route=`image` 且 endpoint_kind=`responses`
  - `image_responses_supported is False` 时排除
  - `responses_supported is False` 时排除
- route=`image` 且 endpoint_kind=`generations`
  - `image_generations_supported is False` 时排除

保留现有 endpoint lock 逻辑；capability 是自动/半自动事实，endpoint lock 是管理员策略。

### 验收标准

- 旧 provider 配置不包含 capability 字段时行为不变。
- capability 为 `false` 的 provider 不会被选到对应 endpoint。
- probe 遇到 404/405 会给出明确 unsupported 结果。
- 429/5xx 不会把 provider 永久标记为 unsupported。

## 优先级 P2：客户端断开与上游 Drain 策略

### 问题

sub2api 的流式修复强调：下游客户端断开后，上游读取仍可继续 drain 到 terminal event，以便拿到 usage 和最终状态。

Lumen 的生图任务主要由 worker 执行，不完全等同于网关直连转发。但如果前端实时订阅断开、任务取消和上游请求取消绑定过紧，可能导致已经在生成中的图片被过早中止。

### 建议原则

- 用户显式取消任务：应取消上游请求。
- 浏览器 SSE 订阅断开：不应自动取消 worker 生图任务。
- worker task deadline 到期：应取消上游请求并清理 curl/httpx 资源。
- 已进入上游生成阶段后，如果只是前端连接断开，继续 drain 到 final image 或 terminal error，并写入 DB/Redis。

### 验收标准

- 关闭浏览器页面不会直接杀掉已提交的生图任务。
- 用户点击取消会中止上游请求。
- 任务超时会清理 curl 子进程和临时文件。
- 前端重新连接后能看到最终成功或失败状态。

## 推荐实施顺序

1. P0：统一 Responses terminal event。
2. P0：为 responses image fallback 增加 JSON fallback。
3. P1：增强图片结果与 image usage 提取。
4. P1：失败上下文结构化。
5. P2：provider capability 字段与路由过滤。
6. P2：梳理客户端断开与 worker drain 策略。

前四项都集中在 `apps/worker/app/upstream.py`，风险和改动面较小。第五项会影响 core schema、API、worker provider pool 和可能的前端管理页，建议单独拆任务。

## 测试建议

### 单元测试

为 `apps/worker/app/upstream.py` 增加或补充测试：

- SSE `response.completed` 正常完成。
- SSE `response.done` 正常完成。
- SSE `response.failed` 抛出包含 upstream error 的异常。
- SSE `[DONE]` 但没有 terminal event 时抛 `BAD_RESPONSE`。
- `application/json` payload 含 `data[].b64_json` 时成功提图。
- `application/json` payload 含 Responses `output[].result` 时成功提图。
- `usage.images` / `tool_usage.image_gen.images` 可被提取并记录。

### 集成测试

用 mock upstream 覆盖以下响应形态：

- SSE 正常 completed。
- SSE done without completed。
- SSE failed after partial image。
- HTTP 200 + `application/json` + Image API payload。
- HTTP 200 + `application/json` + Responses payload。
- HTTP 200 + `text/event-stream`，但最后只有 `[DONE]`。

### 手工验证

- direct `/v1/images/generations` 文生图仍成功。
- direct `/v1/images/edits` 图生图仍成功。
- responses fallback 文生图成功。
- responses fallback 图生图成功。
- provider failover 仍能在 429/5xx 后切号。

## 非目标

本轮不建议做：

- 把主生图链路从 direct image path 切到 responses path。
- 引入 raw `/v1/chat/completions` fallback。
- 改变 image billing 口径。
- 默认开启昂贵的自动 image probe。
- 为 URL 图片结果立即实现下载与转存，除非确认上游会大量返回 URL 而不是 b64。

