# Lumen 深度 Bug 审计与 Pi 原生 Agent 体验整改报告

> 审计日期：2026-08-25
> 审计仓库：`cyeinfpro/Lumen`
> 审计分支：`main`
> 审计基线：`bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879`
> 基线提交：`fix(worker): preserve native Pi run lifetime`
> 重点范围：Agent Runtime、Pi 集成、Worker/API/Web 状态机、图片工具、取消/恢复/压缩/计费，以及全仓高风险并发、资源、安全和架构边界

---

## 1. 结论先行

这次审计没有发现可以被严谨证明为 **P0（立即导致任意代码执行、跨用户数据越权、确定性大规模资金损失或不可逆数据破坏）** 的问题。

但当前 `main` 仍存在一组会直接影响 Agent 稳定运行、成本可控性、取消语义、长会话一致性和 Pi 原生体验的问题：

| 严重度 | 数量 | 说明 |
|---|---:|---|
| P0 / 致命 | 0 | 本轮没有证据足以确认 P0 |
| P1 / 高 | 7 | 可能造成无限运行、持续付费、取消后仍请求、Agent 会话串图、不可恢复压缩、服务拒绝或内存耗尽 |
| P2 / 中 | 26 | 会造成错误终态、恢复不一致、性能退化、上下文失真、测试盲区、架构误接线 |
| P3 / 低 | 8 | 主要是边界脆弱性、可观测性、客户端残留状态和 Pi 原生体验偏差 |
| **合计** | **41** | 包括已确认缺陷、高置信运行风险、测试/治理缺口 |

最需要优先处理的不是 UI 细节，而是以下七项：

1. Runtime v2 完全移除生命周期上限后，没有任何 Pi 原生的紧急熔断器。
2. 图片 Tool Gateway 没有独立超时与响应体上限。
3. Worker 明知运行已取消或 epoch 已过期时，仍可能继续启动 Runtime。
4. 每次新消息都会把整个会话历史图片目录重新作为“当前轮视觉输入”发送。
5. 自动压缩产生的 checkpoint 可能无法在下一轮恢复，或插入到错误位置。
6. 未认证的慢请求可占满全部 Runtime 运行准入槽。
7. 64 MiB 请求 × 8 并发，再叠加 JSON/Base64/图片副本，存在明显 OOM 面。

项目的 Agent 方向并不是错的。相反，以下基础做得较扎实：

- Pi 内置工具与资源默认隔离；
- Runtime/Worker 使用 HMAC、nonce、epoch、事件序号和严格 NDJSON；
- 图片副作用有语义幂等与耐久回执；
- 不确定上游结果采取保守计费与恢复；
- Provider origin、redirect、DNS pin 和代理边界有明确约束；
- 当前 GitHub Actions 基线存在成功运行；
- 旧审计中的一批资金、数据库连接池、Redis 锁续租和重复提交问题已经修复。

问题主要集中在 **新 Agent 链路最近快速演进后，生命周期、图片语义、压缩边界和跨层竞态还没有完全收口**。

> 重要说明：任何静态审计都无法数学证明“没有任何 bug”。本报告尽可能扩大覆盖面，并把已确认问题、风险和验证缺口分开，但不能替代真实 Provider、容器网络、故障注入、压力测试和长时间运行测试。

---

## 2. 审计方式与覆盖范围

本轮不是单次关键词扫描，而是按不同攻击面做了多轮交叉审计。

### 第 1 轮：仓库基线、结构、CI 与治理

- 确认默认分支、最新 SHA、应用拓扑和最近 Agent 变更。
- 阅读 `AGENTS.md`、ADR、历史审计、CI workflow。
- 核对历史问题是否已经修复，避免复制旧报告。

### 第 2 轮：Pi 上游行为与 Runtime

- 对照 Lumen 固定使用的 Pi `0.84.2` 源码。
- 检查 Agent loop、未知工具、listener 顺序、原生 compaction、session entry、stop reason。
- 审计 Runtime request contract、认证、nonce、准入、NDJSON、Provider transport、资源清理和图片工具。

### 第 3 轮：Worker Agent 生命周期

- Run claim、Redis lock、epoch、取消监听、流式事件、checkpoint、文本 flush、terminal finalize。
- Provider dispatch 证据、断线恢复、usage 与计费知识状态。
- Runtime context 构建、历史投影、图片 preview、memory、capability。

### 第 4 轮：API、工具副作用与会话图片

- Message submission、session image catalog、stable labels。
- Tool capability redemption、ordinal/semantic idempotency、失败回放、Generation 创建。
- 取消 API 与 queued hold 释放。

### 第 5 轮：Web Agent

- Optimistic submission、SSE、snapshot polling、session switch。
- Run/event reconciliation、generation channel、continue、附件和草稿。
- 测试是否覆盖真实行为而不是只做字符串/正则检查。

### 第 6 轮：全仓横向风险

- Redis lease、queue ownership、proxy pool、视频计费、文件处理、密码重置、更新机制。
- `shell=True`、`os.system`、`verify=False`、`dangerouslySetInnerHTML`、吞异常、无界任务、全局状态、`Any` 依赖袋。
- 对历史审计逐项复查，确认哪些仍存在、哪些已经修复。

### 第 7 轮：反向排除误报

- 对初步怀疑继续追调用方、测试和上游 Pi 实现。
- 不把设计选择直接写成 bug。
- 不把已经修复的旧问题重新列为现存问题。

### 本轮限制

本轮通过 GitHub 连接器做源代码和 Actions 状态审计，没有获得一个可在当前容器中完整 checkout 的工作树，因此：

- 没有在本地执行 `pytest`、Vitest、TypeScript build、Docker Compose 或压力测试；
- 没有调用真实 OpenAI/Anthropic Provider；
- 没有执行 HTTP/SOCKS/SSH proxy 矩阵；
- 没有进行 Linux amd64/arm64 双架构容器验证；
- 没有对生产 Redis/PostgreSQL 做故障注入。

报告中的“已确认”表示代码控制流本身足以证明；“高置信风险”表示还需要动态环境才能量化触发概率；“验证缺口”表示当前自动化不足以支持稳定性结论。

---

## 3. 严重度与证据定义

| 标记 | 含义 |
|---|---|
| **已确认** | 单凭当前 SHA 的代码控制流即可证明 |
| **高置信风险** | 代码结构明确存在风险，但触发频率依赖 Provider、网络、数据规模或部署 |
| **验证缺口** | 目前没有自动化证据覆盖关键行为，不能据此声称稳定 |
| **架构债务** | 当前可能正常，但类型/边界使未来改动极易引入运行缺陷 |

严重度：

- **P0**：跨租户越权、RCE、确定性重大资金或不可逆数据损坏。
- **P1**：服务不可用、无限成本、取消失效、长会话不可恢复、明显 OOM/DoS。
- **P2**：错误终态、局部成本/体验异常、恢复偏差、性能或维护风险。
- **P3**：低频边界问题、诊断不完整、客户端残留、非原生体验。

---

# 4. P1 高优先级问题

## P1-01：Runtime v2 没有任何紧急生命周期熔断，模型可无限循环

**结论：已确认**

### 受影响代码

- `apps/agent-runtime/src/contracts.ts`
- `apps/agent-runtime/src/runtime.ts`
- `apps/agent-runtime/src/server.ts`
- `apps/agent-runtime/tests/runtime.test.ts`
- `docs/adr/0001-agent-runtime-pi-provider-boundary.md`

### 证据

Runtime v1 曾包含：

- `max_turns`
- `max_tool_calls`
- `max_output_tokens`
- `run_timeout_seconds`
- `tool_timeout_seconds`
- `max_output_chars`

Runtime v2 只保留：

- `max_image_tool_calls`
- `max_images_per_run`

同时 Runtime 把 Provider timeout 设置为 `2_147_483_647 ms`，测试还明确断言“disabled timeout”是预期行为。代码没有接入 Pi 的 `shouldStopAfterTurn` 做最后安全熔断。

Pi 原生循环会在模型持续返回工具调用时继续。即使 `lumen_create_image` 有图片次数限制，模型仍可反复发出：

- 未注册工具；
- 参数校验失败的工具；
- 已达到限制后的工具；
- Provider 自身不断续写或重试的流。

这些路径不一定增加 `imageCalls`，也没有总 turn/provider dispatch 上限。

### 影响

- 单次 Run 长期占用 Runtime 并发槽；
- Worker 因 heartbeat 持续收到事件，不会触发 45 秒 idle timeout；
- Provider 请求和 token 成本可持续增加；
- 用户忘记取消、客户端断线处理异常或恶意提示词可放大问题；
- 8 个长 Run 即可长期阻塞整个 Runtime。

### Pi 原生修复方向

不要恢复一个自定义 Host Agent loop。应继续让 Pi 负责循环，只增加 **外围安全熔断**：

1. 使用 Pi 原生 `shouldStopAfterTurn` 或等价公开 hook。
2. 熔断依据应是：
   - provider dispatch 数；
   - 总 turn 数；
   - 总 usage；
   - 单 Run wall-clock；
   - 连续无进展/重复工具签名；
   - 总输出事件字节数。
3. 熔断后保留已生成文本和已接受工具副作用，终态为 `partial`，错误码如 `agent_safety_budget_reached`。
4. 给用户提供原生“继续”操作，而不是静默截断。
5. 限额应是防事故上限，不应替代 Pi 自己的正常调度。

### 必须新增的回归测试

- 模型无限返回未知工具。
- 模型无限返回超限图片工具。
- 模型返回相同工具参数 100 次。
- Provider 持续输出但不结束。
- 超限后已接受图片不重复提交，文本保留，终态为 partial。
- 取消与安全熔断同时发生时，cancelled 优先级明确。

---

## P1-02：图片 Tool Gateway 无独立超时、无响应体上限

**结论：已确认**

### 受影响代码

- `apps/agent-runtime/src/tools/gateway.ts`
- `apps/agent-runtime/src/runtime.ts`
- `apps/agent-runtime/src/server.ts`

### 证据

`createImageGateway()` 使用裸 `fetch()`：

- 只继承整个 Run 的 AbortSignal；
- 没有 tool deadline；
- 没有 `redirect: "error"`；
- 直接 `response.json()`；
- 不检查 `Content-Length`；
- 不做流式最大字节限制。

Runtime heartbeat 是独立任务。Tool Gateway 即使永久挂住，heartbeat 仍会继续，Worker 的 event idle timeout 因此不会结束 Run。

### 影响

- API/Gateway 半开连接会让整个 Agent 永久等待；
- 大响应体可造成 Runtime 内存压力；
- 发生网关重定向时，能力 token 的边界不够明确；
- 用户取消前，Run 与 Provider/工具状态长期不可收口。

### 修复建议

```ts
const deadline = AbortSignal.timeout(30_000);
const combinedSignal = signal
  ? AbortSignal.any([signal, deadline])
  : deadline;

const response = await fetch(gatewayUrl, {
  method: "POST",
  redirect: "error",
  signal: combinedSignal,
  // ...
});

const payload = await readJsonWithByteLimit(response, 64 * 1024);
```

此外：

- timeout 发生在请求可能送达后，应标记 `resultUnknown=true`；
- 连接失败且可证明请求未送达时才是 `resultUnknown=false`；
- 读取响应前检查 `Content-Length`，读取中再做硬上限；
- 记录 gateway latency、bytes、timeout phase。

---

## P1-03：Worker 忽略 dispatch 状态栅栏结果，取消/旧 epoch 仍可能启动 Runtime

**结论：已确认**

### 受影响代码

- `apps/worker/app/tasks/agent_run_parts/orchestrator.py`
- `apps/worker/app/tasks/agent_run_parts/persistence.py`
- `apps/api/app/services/agent/runs.py`
- `apps/worker/app/agent_runtime_client.py`

### 证据

`update_dispatch_state()` 返回布尔值，用来说明：

- Run 是否仍是当前 execution epoch；
- 状态是否仍允许写入；
- cancellation/terminal fence 是否通过。

但 `_run_prepared_execution()` 调用：

```python
await update_dispatch_state(
    run_id,
    execution_epoch,
    state="starting",
)
```

后完全丢弃返回值，然后继续 `runtime_client.stream()`。

与此同时，取消 API 会：

- 将状态改为 `cancelled`；
- 设置 `finished_at`；
- `execution_epoch += 1`；
- 写 Redis cancel signal。

因此存在明确竞态：

1. Worker 已构建 context；
2. 用户取消，数据库 epoch 前进；
3. Worker 的 `update_dispatch_state()` 返回 `False`；
4. 调用方忽略；
5. Runtime 请求仍发出，甚至继续到 Provider。

### 影响

- 用户看到已取消，但上游仍可能请求并产生费用；
- 后续事件因 stale epoch 被丢弃，结果变成 unknown；
- 计费只能保守结算；
- 取消语义不可信。

### 第一阶段修复

```python
started = await update_dispatch_state(
    state.claim.run_id,
    state.claim.execution_epoch,
    state="starting",
)
if not started:
    background.cancel_requested.set()
    raise AgentRuntimeClientError(
        "agent_stale_execution_epoch",
        delivery="proven_absent",
    )
```

### 完整修复

上面的检查仍存在 check-to-dispatch 微小窗口。若要保证“数据库取消先提交后绝不再向 Provider 发字节”，需要增加 **一次性 provider dispatch ticket**：

- Runtime 在每次真正调用 Provider transport 前向 Worker/API 兑换；
- 兑换事务锁定 Run，校验 active + epoch；
- ticket 只可消费一次；
- 兑换失败时 Pi 被 abort；
- ticket 已兑换后再取消属于“已派发，尽力中止”。

这仍然保留 Pi 原生循环，只把付费边界交给 Lumen。

---

## P1-04：每一轮都把整个会话图片目录重新作为当前视觉输入

**结论：已确认**

### 受影响代码

- `apps/api/app/services/agent/message_submission.py`
- `apps/worker/app/agent_context.py`
- `apps/agent-runtime/src/runtime.ts`
- `apps/api/app/services/agent/session_images.py`

### 跨层证据

API `_session_references()` 会组合：

1. 历史所有 `AgentRunReference`；
2. 历史所有成功 Agent Generation 图片；
3. 当前消息附件。

Worker 随后：

- 读取这些图片；
- PIL 解码；
- 缩放并转 WEBP；
- Base64 编码；
- 放入 Runtime request。

Runtime `currentImages()` 再把 **全部 references** 传入：

```ts
session.prompt(request.current_prompt, { images })
```

会话最多可积累 64 张。

### 影响

一个纯文字追问也会重新携带过去所有图片：

- 模型把无关旧图误认为当前指令的一部分；
- 旧图与当前文字产生语义串场；
- 每轮重复视觉 token 成本；
- 64 图请求可能超过真实 Provider 的单次图片限制；
- 每轮重复对象存储读取、PIL 解码、WEBP 编码；
- 用户无法知道哪些图片实际被发送；
- 历史敏感图片被不必要地重复暴露给 Provider。

### Pi 原生修复方向

必须分开两种概念：

- **Session image catalog**：用户可在后续轮选择的耐久资源目录；
- **Current turn image content**：本轮真正附在 Pi user message 上的图片。

正确模型：

1. 当前附件只附在当前 Pi user message。
2. 历史图片保留在它原来的 Pi message 结构中，不重复挂到新消息。
3. 需要重新使用旧图时：
   - 用户显式选择；
   - 或 Agent 使用一个只读、受限的 `select_session_image` / resource lookup 能力；
   - 然后只把选中的图用于当前工具调用。
4. Tool capability 的 allowed labels 应只包含本轮显式选择，或经过一次受控选择后的集合。
5. Web 清楚展示“本轮将发送 N 张图片”。

这是实现 Pi 原生多模态会话最关键的整改之一。

---

## P1-05：自动 compaction checkpoint 边界可能无法恢复或插入位置错误

**结论：高置信、接近确定性**

### 受影响代码

- `apps/agent-runtime/src/runtime.ts`
- `apps/worker/app/tasks/agent_run_parts/compaction_checkpoint.py`
- `apps/worker/app/agent_context.py`
- `docs/adr/0001-agent-runtime-pi-provider-boundary.md`

### 证据链

Runtime 只为 **预先 seed 的 history entry** 建立：

```ts
entryMessageIds: Map<PiEntryId, LumenMessageId>
```

当 compaction 完成时：

```ts
const firstKeptMessageId =
  entryMessageIds.get(result.firstKeptEntryId)
  ?? request.user_message_id;
```

如果 auto compaction 发生在当前 prompt/assistant/tool 进入 Pi session 之后，`firstKeptEntryId` 可能属于当前轮的新 entry，不在 map 中，于是 fallback 成当前 `user_message_id`。

Worker 又无论 compaction 实际发生在哪个阶段，都持久化：

```python
"next_message_id": run.user_message_id
"reason": "pre_prompt"
```

下一轮恢复时，Runtime 在遍历 history 遇到 `next_message_id` **之前** 调用 `appendCompaction()`，但 `appendCompaction()` 又要查询 `first_kept_message_id` 对应的 entry。若两者都是上一轮 current user message，该 entry 此时尚未 append，直接失败：

```text
Pi compaction boundary is unavailable
```

即使没有直接抛错，自动/overflow/post-turn compaction 被伪装为 `pre_prompt`，也可能导致摘要插入位置不准确、消息重复或遗漏。

### 影响

- 长会话在恰好触发自动压缩后，下一轮无法启动；
- checkpoint 被判无效后退回全量历史，可能再次超上下文；
- 可能重复包含已摘要消息，或漏掉应保留消息；
- Pi 原生 compaction 的主要价值被破坏。

### 修复建议

1. 为所有 Pi entry 建立耐久映射：
   - 历史 user/assistant；
   - 当前 user；
   - 当前 assistant；
   - tool call/result；
   - compaction entry。
2. checkpoint 事件必须包含：
   - `first_kept_entry_ref`
   - `next_entry_ref`
   - `phase`: `pre_prompt | overflow_retry | post_turn`
   - Pi session revision
3. **禁止 fallback 到 current user ID**。映射不可用时，不应持久化 ready checkpoint。
4. Worker 不得硬编码 `reason` 和 `next_message_id`。
5. Session 级维护“当前有效 checkpoint 指针”，而不是在历史 Run 中猜。
6. 增加进程重启后的真实恢复测试。

---

## P1-06：未认证慢请求可占满全部 Runtime 准入槽

**结论：已确认**

### 受影响代码

- `apps/agent-runtime/src/server.ts`
- `apps/agent-runtime/src/config.ts`

### 证据

服务先检查：

```ts
if (admittedRequests >= maxConcurrentRuns) ...
admittedRequests += 1;
```

随后才：

- 读取最大 64 MiB body；
- 等待最多 10 秒；
- 验证 HMAC；
- 解析 JSON。

因此 `maxConcurrentRuns=8` 时，8 个没有合法签名的慢请求就能占满全部槽。合法 Worker 请求会立即收到 `503 agent_runtime_capacity_exhausted`。

ADR 写的是“pre-auth request admission”，但当前使用的是与真实 Run 相同的全局计数，而不是独立、低成本的 body-read 限流。

### 影响

- 后端网络中任何能访问 Runtime 的错误客户端均可造成周期性拒绝服务；
- Worker 重试策略保守，合法 Run 可能直接失败；
- 攻击不需要合法 HMAC。

### 修复建议

分成两个限流器：

1. `bodyReadSemaphore`：较大但有限，用于未认证短请求；
2. `runSemaphore`：仅在 HMAC + schema 成功后获取。

流程：

```text
accept socket
→ acquire body-read slot
→ bounded read
→ HMAC verify
→ parse schema
→ release body-read slot
→ acquire weighted run slot
→ execute
```

同时：

- 入口只应暴露在 private network；
- 可加连接级速率限制；
- 对超大 `Content-Length` 在读 body 前直接 413；
- 记录 pre-auth capacity rejection 指标。

---

## P1-07：64 MiB × 8 并发的多份内存复制可导致 Runtime OOM

**结论：高置信风险**

### 受影响代码

- `apps/agent-runtime/src/config.ts`
- `apps/agent-runtime/src/server.ts`
- `apps/agent-runtime/src/contracts.ts`
- `apps/agent-runtime/src/runtime.ts`

### 证据

默认：

- `maxRequestBytes = 64 MiB`
- `maxConcurrentRuns = 8`
- references 最多 64 张
- 单张 Base64 字符串上限 700,000
- 解码后单张最多 512 KiB

请求处理会同时产生：

1. `chunks[]`
2. `Buffer.concat`
3. UTF-8 string
4. JSON object 中的 Base64 string
5. schema 检查与后续 ImageContent
6. Provider adapter 可能再次复制/编码

单凭 raw body 就可能达到 512 MiB；加上字符串和对象开销，峰值可能明显超过 1 GiB。Node/V8 容器若设置常见内存限制，容易被 OOMKill。

### 修复建议

- 把默认单请求降到与真实产品上限匹配；
- 增加 `max_total_reference_bytes`，而不是只看每张；
- 使用 weighted semaphore：权重按 `body bytes + decoded image bytes + model context`；
- body 先检查 `Content-Length`；
- references 改成受签名的临时内部对象地址或共享存储句柄，避免 Base64 大包跨进程；
- 若必须内联，采用流式 parser 或临时文件，避免完整多份复制；
- 压测 8 并发最大图片请求并记录 RSS/heap/external memory。

---

# 5. P2 中优先级问题

## P2-01：Provider `length` 截断被标记为成功

**结论：已确认**

`terminalErrorCode()` 对 `stopReason === "length"` 返回 `null`，测试明确断言结果为 `succeeded`。

### 影响

- 用户得到不完整回答，但 UI 显示成功；
- 不会出现继续提示；
- 长 JSON、代码、说明可能在语法中间截断；
- 计费成功但产品语义错误。

### 修复

- 保留 Pi 原生 stop reason；
- Runtime 终态改为 `partial` 或新增 `succeeded_truncated`；
- 错误码 `agent_output_truncated`；
- Web 展示“输出达到模型长度限制，继续生成”；
- Continue 必须基于同一 Pi session，而不是重发父消息。

---

## P2-02：失败或仍在运行的工具 exact replay 被伪装成 200 成功响应

**结论：已确认**

API 只要找到 exact replay，就调用 `_tool_replay_out()`，不区分：

- `succeeded`
- `failed`
- `running`
- `cancelled`
- `timed_out`

失败/运行中的记录通常没有 `generation_ids`，API schema允许空数组，因此返回 200。Runtime 却要求成功响应必须有 1–4 个 ID，于是把确定失败升级为 `agent_tool_result_unknown`。

### 影响

- 可确定失败变成无法对账；
- Run 可能被标记 partial/unknown；
- 运维无法看到原始失败码；
- 恢复逻辑更保守，用户体验更差。

### 修复

| 旧状态 | exact replay 行为 |
|---|---|
| succeeded | 200，回放原成功回执 |
| failed/cancelled/timed_out | 回放原 HTTP 状态与稳定错误码 |
| running | 409 `agent_tool_in_progress`，附 tool call ID |
| 数据不完整 | 409/500，明确 `agent_tool_receipt_incomplete` |

---

## P2-03：工具次数/图片上限失败没有写入 ToolRuntimeState，Run 仍可能成功

**结论：已确认**

`createImageTool.execute()` 在检查：

- `state.imageCalls >= max_image_tool_calls`
- `state.acceptedImages + requestedCount > max_images_per_run`

时直接抛错，但没有：

- `state.calls += 1`
- `state.failedCalls += 1`
- `state.lastErrorCode`
- `state.errors.set(...)`

事件层会发出 `tool.failed`，但最终结果计算只看 `failedCalls`。模型若收到错误后自然收尾，Run 可被判为 `succeeded`。

### 修复

统一失败入口：

```ts
function failTool(
  state: ToolRuntimeState,
  toolCallId: string,
  code: string,
  resultUnknown = false,
): never {
  state.calls += 1;
  state.failedCalls += 1;
  state.lastErrorCode = code;
  state.errors.set(toolCallId, { code, resultUnknown });
  if (resultUnknown) state.unknownResults += 1;
  throw new ToolGatewayError(code, resultUnknown);
}
```

所有 limit/reference/schema/gateway 失败都走同一条路径。

---

## P2-04：Tool Gateway 成功响应校验不完整，且没有拒绝重定向

**结论：已确认**

Runtime 对 `accepted` 只验证了部分类型，没有完整验证：

- `count` 是否 1–4；
- `aspect_ratio` 是否在允许枚举；
- labels 是否唯一、格式正确、最多 16；
- `accepted.count` 是否等于 generation IDs 数量或请求归一化结果；
- 回执是否与本次 tool call ordinal/semantic identity 一致。

同时 fetch 默认允许 redirect。

### 修复

- 与 API 共用一份生成的 JSON Schema；
- 对 accepted parameters 做完整枚举/范围验证；
- 校验 `generation_ids.length == accepted.count`；
- 返回并验证 `tool_call_id`、`ordinal`、`request_hash`；
- `redirect: "error"`；
- 任一不一致都标记 result unknown，而不是继续。

---

## P2-05：历史会话被压平成文字，丢失 Pi 原生 message/tool/image 结构

**结论：已确认，属于 Pi 原生体验核心问题**

Worker 把历史 Message 投影为：

- 普通 user/assistant text；
- `[Historical image attachment ...; binary omitted]`
- `[Historical tool summary: ...]`

Runtime 再人为构造 assistant message，并把：

- usage 设为 0；
- stopReason 设为 stop；
- provider/model 改为当前 provider/model；
- tool call/result 变成普通文字；
- 图片与原消息的关系丢失。

### 影响

- Pi 无法理解哪个工具结果对应哪个 call；
- 历史图片不是原消息 content，只能靠本轮全部重发；
- compaction 摘要基于失真的历史；
- Provider/model 切换时历史 provenance 被抹掉；
- Agent 可能重复已经执行过的动作。

### Pi 原生整改

持久化并恢复一个 **受控的 Pi canonical projection**：

- typed user content；
- typed assistant text/reasoning；
- toolCall ID/name/arguments；
- toolResult ID/status/摘要；
- image content 的耐久引用；
- stop reason；
- 原 provider/model 仅作为 metadata，不伪装成当前模型；
- usage 可不重放给 Provider，但不能伪造为真实历史 usage。

Lumen 仍可以过滤敏感字段，但不应把结构全部降级成文本。

---

## P2-06：Provider/context 预检没有按真实历史 token 做准入，失败过晚

**结论：高置信风险**

API 选择模型时只估算：

- system prompt；
- 当前 text；
- reference count；
- output reserve；
- safety reserve。

Worker `_pack_history()` 也只验证固定部分，然后直接返回全部 history。真正是否能放入 context 交给 Runtime 后续 compaction。

### 影响

- Run、消息、hold 已创建后才发现 history transport/context 无法承载；
- compaction 本身也可能需要 Provider 调用并产生费用；
- checkpoint 不可恢复时会反复遇到问题；
- 不同 Provider 的上下文能力与图片消耗差异未提前体现。

### 修复

- 使用与 Pi 相同的 token estimator 做 dry planning；
- 预先确定：直接运行、需一次 compaction、需多级 compaction、无法运行；
- compaction 费用纳入明确 reservation；
- 在不可运行时，在创建付费 Run 前返回可解释错误；
- 不要复制一套不同于 Pi 的切片算法。

---

## P2-07：Agent 默认 reasoning effort 为 `max`

**结论：已确认，属于成本/体验问题**

默认值在多层被固定为 max：

- API Pydantic 默认与 validator；
- Web submit fallback；
- Worker fallback；
- GPT-5.6 又额外映射 max。

### 影响

- 简单任务也走最高思考等级；
- 延迟与成本不可预测；
- 与 Provider/Pi 原生默认行为不一致；
- 用户可能认为“普通聊天”卡住。

### 修复

- 增加 `auto` 作为产品默认；
- `auto` 交给 Pi/Provider model metadata；
- 高/最大只由用户显式选择或任务策略提升；
- UI 展示预计延迟/成本等级；
- 旧 session 迁移时不要自动改为 max。

---

## P2-08：每张图片固定按 2048 token 估算，不具备 Provider 感知

**结论：已确认**

API/Worker 使用固定 `_REFERENCE_CONTEXT_TOKENS`。真实视觉 token 取决于：

- Provider；
- 模型；
- 分辨率；
- tile 策略；
- detail 模式；
- 图片数量和 adapter 编码方式。

### 影响

- 低估：运行时溢出、额外压缩、失败；
- 高估：错误拒绝本可运行的请求；
- Provider selection 失真。

### 修复

- Provider capability 增加 `estimate_image_tokens(width,height,mime,detail)`；
- 无法准确估计时使用有证据的保守区间；
- 实际 usage 回流校准；
- 记录估计误差指标。

---

## P2-09：恢复 compaction 只扫描最近 64 个历史 Run

**结论：已确认**

`_pi_compaction()` 对 prior runs `.limit(64)`。

### 风险

若一个有效 checkpoint 位于更早的 Run，之后 64 轮都没有生成新 checkpoint，它会被静默忽略。随后 Worker 重新加载更大历史，可能达到 transport/context limit。

### 修复

- 在 `AgentSession` 上保存 `active_compaction_checkpoint_id/version`；
- checkpoint 替换应事务化；
- Run 只保留审计记录，恢复不依赖向后扫描；
- checkpoint 被删除/失效时有明确降级事件。

---

## P2-10：Session 图片目录自动增长且没有明确移除入口，最终会永久撞 64 张上限

**结论：已确认**

目录会自动吸收：

- 每轮附件；
- 每轮历史 reference；
- 所有成功 Agent 结果。

没有发现针对 `AgentRunReference` 的 session-level eject/prune 操作。除非用户全局删除图片、触发保留策略或删除整个会话，槽位会继续增长。

### 影响

- 长会话最终无法再添加附件；
- 图片工具请求被 `agent_session_reference_limit_reached` 拒绝；
- 用户无法理解为什么本轮只有少量附件却“达到 64 张”。

### 修复

- 增加 session image manager；
- 允许 pin/unpin/eject；
- 生成结果不要自动永久加入目录，先作为 run output；
- 使用 LRU 只能作为提示，不能静默删除用户 pin 的图；
- UI 显示 `used/max`。

---

## P2-11：每轮对全部历史图片重新读存储、PIL 解码和 WEBP 编码

**结论：已确认**

`_reference_previews()` 每次 Run：

- 对所有 reference 读取对象存储；
- PIL `load()`；
- thumbnail；
- 多质量循环编码；
- Base64。

即使已有 `preview1024`，仍需要读、解码、再编码。

### 影响

- 长会话首 token 延迟不断增加；
- Worker CPU/内存、对象存储 QPS 增加；
- 64 张图时，即使用户只发一句文字，也要做完整工作。

### 修复

优先通过 P1-04 取消“全量当前图”。此外：

- preview 产物应持久化为已满足 Runtime 上限的格式；
- 传对象引用或已签名内部 URL；
- 缓存按 image version + target params；
- 记录 preview build latency 与 cache hit。

---

## P2-12：Tool capability 最长 24 小时，但 Run 可无限存活

**结论：已确认**

Capability 最大 TTL 是 24 小时，而 Runtime v2 没有 run deadline。注释称 TTL 不是生命周期上限，但现实中晚于 24 小时的工具调用必然失败。

### 影响

- 长 Agent 前半段正常，后半段图片工具突然 `expired`；
- 模型无法理解产品层 token 失效；
- 运行结果与“只由 active run/epoch 撤销”的设计说明矛盾。

### 修复

两种方案必须二选一：

1. 给 Run 一个明显小于 24 小时的安全生命周期上限；
2. 允许 Runtime 在 active epoch 下兑换短期续期 capability。

不要简单把 token TTL 无限拉长。

---

## P2-13：Runtime SIGTERM 只停止接收新连接，不会主动中止或限时排空现有 Run

**结论：已确认**

shutdown 只调用 `server.close()`。活跃 Run 可能因无限 Provider/Tool 请求长时间不结束。

### 影响

- 部署滚动更新卡住；
- 容器最终被强杀，结果变 unknown；
- 正在流式输出的 usage/checkpoint 来不及持久化。

### 修复

- 维护 active AbortController registry；
- SIGTERM 后：
  1. 标记 not ready；
  2. 停止新准入；
  3. 给现有 Run grace period；
  4. 超时 abort；
  5. 等待 terminal/transport close；
- 向 Worker发送明确 `runtime_shutdown`，并保留已知 usage。

---

## P2-14：Runtime 初始化阶段可能泄漏 Provider transport，清理异常又可能覆盖成功结果

**结论：已确认**

`prepareProvider()` 在最外层 `try/finally` 之前执行。随后以下任一步抛错都可能不调用 `prepared.close()`：

- Settings/SessionManager 创建；
- seed compaction；
- `createAgentSession()`；
- listener/subscriber setup。

反过来，在 finally 中：

```ts
session.dispose();
await prepared.close();
```

若 close 抛错，会覆盖原本成功的 return 或原 RuntimeExecutionError。

### 修复

- Provider prepare 后立即进入 outer `try/finally`；
- cleanup 使用 `Promise.allSettled`；
- 主执行结果优先；
- cleanup failure 只写 metric/log，除非它意味着结果不可信；
- 对 close/dispose 注入失败测试。

---

## P2-15：`validateRuntimeConfig()` 是空函数，程序化配置绕过全部约束

**结论：已确认**

环境变量通过 `integerEnv()` 校验，但测试、嵌入式启动或未来调用方可以传 `options.config`，`validateRuntimeConfig()` 原样返回。

### 影响

可传入：

- 空/短 shared secret；
- 负数或极大 timeout；
- 0 concurrent；
- 非法 maxLineBytes；
- 公开 host 等意外配置。

Readiness 可能拦截一部分，但不是完整配置校验。

### 修复

用 TypeBox/Zod 或手写严格 validator，环境加载和程序化配置必须走同一套 schema。

---

## P2-16：GPT-5.6 的 reasoning level 被硬编码在 Runtime

**结论：已确认**

`extendedThinkingLevelMap()` 只根据 model ID 前缀识别 `gpt-5.6`，手动映射 `xhigh/max`。

### 风险

- 新模型/别名无法使用真实能力；
- 第三方兼容 Provider 使用同名模型可能语义不同；
- Pi 或 Provider metadata 更新后 Lumen 仍旧；
- Runtime 开始承担模型目录职责。

### 修复

- 能力来自 Worker 已验证的 provider envelope；
- 或直接使用 Pi `Model` registry/metadata；
- envelope 中传显式 `thinking_level_map`，并做枚举校验；
- 不根据字符串猜能力。

---

## P2-17：真实 Provider、代理、abort、429/5xx、truncated stream 没有自动化门禁

**结论：验证缺口**

ADR 明确说明当前 faux suite 不验证：

- OpenAI Responses；
- OpenAI Completions；
- Anthropic Messages；
- vision/tool/reasoning usage；
- abrupt EOF、truncated SSE；
- abort；
- 429/5xx；
- HTTP/SOCKS/SSH proxy；
- amd64/arm64；
- Docker DNS/read-only；
- 跨容器 SSH reachability。

### 风险

Agent 最关键的适配层只在 fake provider 上证明。真实 SDK 细节、流事件、usage、停止原因和 abort 很容易与 faux 行为不同。

### 修复

建立三层测试：

1. **本地确定性 mock provider server**：覆盖 chunking、429、5xx、EOF、redirect、slow headers、slow body。
2. **容器网络矩阵**：direct/HTTP/SOCKS/SSH、read-only、DNS、双架构。
3. **付费 live canary**：低频、预算受限、发布前手动/受控执行。

---

## P2-18：Faux Provider 测试没有 await `onDispatch()`，会掩盖顺序和失败

**结论：已确认测试缺口**

测试 monkey-patch `streamSimple` 时：

```ts
void onDispatch();
return streamSimple(...);
```

生产 transport 则 `await onDispatch()` 后才请求。

### 影响

测试无法发现：

- dispatch event 写失败；
- writer backpressure；
- callback rejection；
- provider request 是否早于 durable dispatch checkpoint；
- callback 与响应事件顺序。

### 修复

测试必须 await callback，并新增：

- onDispatch reject；
- event writer false；
- slow backpressure；
- dispatch callback 后 fetch 连接失败；
- callback 与 abort 同时发生。

---

## P2-19：Web 只订阅 generation ID 列表前 48 个，且未按 active/newest 排序

**结论：已确认**

```ts
currentGenerationIds.slice(0, 48)
```

`Object.keys(generationsById)` 的顺序可能包含大量旧终态任务。新的 active generation 排在 48 之后时不会订阅 SSE，只能依赖轮询修复。

### 影响

- 图片进度、成功或失败展示延迟；
- 任务较多的 Agent session 看起来“卡住”；
- SSE 与 polling 频繁互相覆盖。

### 修复

- 只选非终态；
- active 优先、created_at 倒序；
- 或后端支持 `agent-session:{id}` 聚合 generation 事件；
- terminal 后及时解除单任务 channel。

---

## P2-20：“继续”只恢复父 user text，丢失附件和运行参数

**结论：已确认**

`continueFrom()` 只执行：

```ts
setDraftText(currentSessionId, parent.text)
```

没有恢复：

- attachments；
- attachment role/label；
- image defaults；
- allow image；
- reasoning effort；
- system prompt/context scope。

### 影响

对 truncated/partial Run 点击继续，会变成一个语义不同的新请求。

### Pi 原生修复

Continue 不应复制父请求。应由服务端创建：

```text
在同一 Pi session 上继续上一 assistant turn
```

并携带 checkpoint/session revision。只有“重新编辑并重试”才重新装载完整原始请求。

---

## P2-21：AgentWorkspaceController 测试偏静态，未覆盖真实竞态

**结论：验证缺口**

现有测试对 controller 的一些行为主要通过字符串/正则和 mock 断言，缺少真实 React 生命周期下的：

- SSE + polling 竞态；
- session 快速切换；
- cancellation；
- visibility/focus；
- 60 个 generation；
- stale active run；
- optimistic retry；
- identity epoch 变更；
- Continue with attachments。

### 修复

使用 React Testing Library、fake timers、MSW/可控 SSE，做行为测试，不要只校验源码包含某个模式。

---

## P2-22：Memory assembly 任意异常都会静默变成“没有记忆”

**结论：已确认**

`_memory_context()` 捕获所有异常，只日志 warning，然后返回空 memory。

### 影响

- 用户感知为 Agent 随机失忆；
- UI 不知道本轮处于 degraded mode；
- transient Redis/DB 错误与“用户关闭记忆”无法区分；
- 结果可能基于缺失约束执行图片工具。

### 修复

- 区分 disabled / empty / degraded / failed；
- 在 dispatch snapshot 和 Web 显示 memory degraded；
- 对 transient failure 在 Provider dispatch 前做短重试；
- 若 memory 包含硬安全/业务约束，失败应阻止运行而不是静默降级。

---

## P2-23：主 lease 已续租成功后，次要 Redis 刷新失败仍可能被误判 lease lost

**结论：已确认**

`_renew_generation_lease_once()` 顺序：

1. CAS 续主 lease；
2. `expire(extra_lease_keys)`；
3. refresh inflight；
4. refresh queue ownership。

任一后续步骤抛错，调用方认为整个 renewal 失败，不更新本地 `renewal_deadline`。下一次重试可能再次成功续主 lease，但只要次要刷新持续失败，本地仍会在旧 deadline 到达时标记 lease lost。

### 影响

- Worker 实际仍持有 Redis 主 lease，却主动中止；
- 上游结果变 unknown；
- Generation 进入额外恢复/计费路径；
- Redis 局部故障被放大成业务失败。

### 修复

- 主 ownership CAS 与次要 metadata refresh 分离；
- 主 CAS 成功后立刻推进 renewal deadline；
- secondary failure 单独 metric/retry；
- 只有主 CAS 失败才设置 `lease_lost`；
- 队列 ownership 有自己的 stale/reconcile 机制。

---

## P2-24：Proxy round-robin 使用全局计数器，不是每个候选池自己的轮询

**结论：已确认**

所有调用共享：

```python
_RR_KEY = "lumen:proxy:rr:idx"
```

不同 Provider、用户或候选列表的调用会互相推进同一计数。两个不同长度的 pool 交错时，单个 pool 的选择序列不再 round-robin，甚至可能连续命中同一个代理。

首次 Redis `INCR` 返回 1，长度大于 1 时也会跳过 index 0 作为首选。

### 修复

Key 应按稳定 pool identity 分区，例如：

```text
lumen:proxy:rr:{sha256(sorted candidate names + routing scope)}
```

同时使用 `(INCR - 1) % len(pool)`。

---

## P2-25：巨型 `Any` Ports 只是物理拆文件，没有建立可验证领域边界

**结论：架构债务**

`VideoGenerationPorts` 等对象包含大量 `Any` 字段，把几乎所有依赖放入一个 bag。

### 风险

- type checker 无法发现参数/生命周期误接；
- 测试很容易 mock 错层；
- ownership、shutdown、concurrency 不清楚；
- 模块虽然拆小，运行时耦合仍然很大。

### 修复

拆成小型、强类型 protocol：

- `GenerationPersistencePorts`
- `GenerationLeasePorts`
- `GenerationProviderPorts`
- `GenerationBillingPorts`
- `GenerationArtifactPorts`
- `GenerationEventPorts`

每个应用服务只拿真正需要的 ports。

---

## P2-26：`AGENTS.md` 强制要求读取 `MEMORY.md`，但仓库没有该文件

**结论：已确认治理缺陷**

`AGENTS.md` 明确写“Before changing... read MEMORY.md”。当前仓库根目录没有可读取的 `MEMORY.md`。

### 影响

- 自动 Agent 无法满足仓库自己的前置要求；
- 不同 Agent 会自行猜测；
- 发布/修复流程不可复现；
- 容易产生“看似遵循规范，实际缺少关键上下文”的提交。

### 修复

- 提交真实 `MEMORY.md`；
- 或删除该强制规则，改为指向存在的 ADR/CONTRIBUTING；
- CI 检查 `AGENTS.md` 引用文件存在；
- 文件内容不要包含秘密。

---

# 6. P3 低优先级问题

## P3-01：NDJSON payload 可以覆盖保留 envelope 字段

**结论：已确认边界脆弱性**

Event writer 构造对象时，如果先写固定字段再 spread payload，内部调用方可以覆盖：

- `version`
- `type`
- `seq`
- `run_id`
- `execution_epoch`

当前调用方可信，客户端也会校验，因此暂未形成漏洞，但协议层不应依赖所有未来调用点都不传保留键。

### 修复

- payload 先 spread，固定 envelope 最后写；
- 或显式拒绝 reserved keys；
- 单元测试所有保留字段不可覆盖。

---

## P3-02：Provider transport 遇到 `Request` 输入时只保留 URL

**结论：已确认兼容性风险**

transport wrapper 对 `Request` 使用 `input.url`，其原有 method/headers/body 不会自动带入，完全依赖第二个 `init`。

当前 Pi adapter 大概率使用 URL + init，因此暂未触发；未来 adapter 若传完整 Request，会产生语义变化。

### 修复

使用：

```ts
const request = new Request(input, init);
```

然后验证 origin/redirect，再把完整 request 发送给 undici。

---

## P3-03：NonceCache 清理假设 Map 的插入顺序等于过期顺序

**结论：已确认低频风险**

清理循环遇到第一个未过期 entry 就 `break`。正常单调时间下成立，但：

- 测试注入非单调 now；
- 系统时钟回拨；
- 未来支持不同 TTL；

都可能让后面的已过期 nonce 留在缓存，导致误报 replay 或 capacity full。

### 修复

- 使用 monotonic expiry；
- 或完整 bounded sweep；
- 或 min-heap + map；
- 明确时钟回拨测试。

---

## P3-04：按字符硬切 prompt 可能先保留 memory/reference，再截掉用户请求尾部

**结论：已确认结构风险**

`_current_prompt()` 的顺序是：

1. memory context；
2. 全部 reference lines；
3. user request；

最后整体 `[:40_000]`。当前两部分较长时，用户指令尾部先被截掉。

`_base_system_prompt()` 也采用整体字符切片，而不是 token-aware priority packing。

### 修复

- 用户请求是不可截断最高优先级；
- memory/reference metadata 按 token budget 从低优先级删减；
- 截断必须写入可见诊断；
- 统一使用 Pi/provider tokenizer 估算。

---

## P3-05：Optimistic UI 假设当前附件标签是 `ref_1...`，与服务端 catalog 标签可能不同

**结论：已确认体验问题**

历史目录已有 16 张图时，新附件实际可能是 `ref_17`，但 optimistic message 会显示本地顺序标签。服务端 reconcile 后标签跳变。

### 影响

用户或 Agent 文本中若引用 optimistic label，可能指向错误图片。

### 修复

- optimistic 阶段使用 `pending_ref_1`，不要伪装稳定标签；
- 或先调用服务端 reserve labels；
- reconcile 时清楚显示已分配 label。

---

## P3-06：Web 删除 session 后不清理对应 runs/generations

**结论：已确认客户端内存残留**

`removeSession()` 删除：

- session；
- messages；
- draft；

但不删除：

- `runsById`
- `generationsById`
- `generationSessionIds`

### 影响

长时间使用并删除大量 session 后，Zustand store 持续增长。虽然当前筛选通常不会展示错会话，仍是内存和调试噪声。

### 修复

按 `agent_session_id` 和 `generationSessionIds` 一并清理。

---

## P3-07：Runtime Gateway 的安全错误码 allowlist 不完整

**结论：已确认诊断问题**

API 可能返回 capability/snapshot/in-progress 等更具体错误，但 Runtime 只允许少量 code，其余统一降级 `agent_tool_failed`。

### 影响

- UI 无法给出正确动作；
- 运维难以区分配置、过期、epoch、数据缺失；
- 测试只看到通用失败。

### 修复

- 错误 contract 由 core 生成并在 TS/Python 共用；
- 区分可展示、可重试、result unknown、内部错误；
- 未知 code 仍脱敏，但保留内部 structured cause。

---

## P3-08：基础 system prompt 强制“directly and concisely”，削弱 Pi 原生行为

**结论：产品/架构偏差**

Lumen base prompt 全局要求 Agent 直接且简洁。这会覆盖用户想要的：

- 深度分析；
- 长文；
- 多步骤解释；
- 充分讨论。

### Pi 原生修复

基础 system prompt 只保留产品安全和工具契约。回答风格交给：

- 用户指令；
- session system prompt；
- Pi/模型原生行为；
- 可选产品风格设置。

不要在基础设施层统一压成“简短”。

---

# 7. Pi 原生 Agent 体验的目标架构

当前架构的正确原则应该保留：

> Pi 负责模型循环、原生 session、typed messages、工具调度、重试和 compaction；Lumen 负责用户/会话归属、Provider 选择、凭据、计费、能力 token、图片 Generation、耐久事件和恢复。

但落地时应进一步收口。

## 7.1 Pi 应真正拥有的部分

- Agent turn loop；
- tool call → tool result → next turn；
- typed user/assistant/tool messages；
- reasoning effort/provider stop reason；
- native retry；
- native compaction；
- session revision / entry tree；
- continuation。

## 7.2 Lumen 应拥有的部分

- PostgreSQL 产品实体；
- user/session ownership；
- provider/BYOK pin；
- wallet hold/settlement；
- paid tool semantic idempotency；
- capability 与 execution epoch；
- SSE/outbox；
- image artifact；
- operator audit；
- emergency safety budget。

## 7.3 当前最不 Pi 原生的五个点

1. 把历史 typed messages 压成文字摘要。
2. 每一轮把整个图片目录重新挂到当前 user message。
3. `length` stop 被产品层抹平成普通 success。
4. Continue 通过复制父文本而不是继续 Pi session。
5. reasoning 能力靠模型名硬编码且默认 max。

## 7.4 推荐的耐久化方式

不要直接把 Pi 私有 session file 当产品数据库，也不要完全丢掉 Pi 结构。

可以持久化一个 Lumen 控制的版本化 projection：

```json
{
  "schema_version": 2,
  "pi_runtime_version": "pi-0.84.2",
  "session_revision": 42,
  "entries": [
    {
      "entry_ref": "msg:...",
      "kind": "message",
      "role": "user",
      "content": [
        {"type": "text", "text": "..."},
        {"type": "image_ref", "image_id": "...", "version": "..."}
      ]
    },
    {
      "entry_ref": "tool:...",
      "kind": "tool_result",
      "tool_call_id": "...",
      "name": "lumen_create_image",
      "status": "accepted",
      "generation_ids": ["..."]
    }
  ],
  "compaction": {
    "summary": "...",
    "first_kept_entry_ref": "...",
    "next_entry_ref": "...",
    "phase": "post_turn"
  }
}
```

每次升级 Pi 时做 projection contract test，而不是依赖内部 session file 格式。

---

# 8. 必须建立的跨层状态机不变量

以下不变量应写成数据库约束、代码 assertion 和集成测试。

## 8.1 Provider dispatch

- `provider.dispatched` 前必须有 active run + matching epoch 的一次性许可。
- cancellation 提交在许可之前：不得发 Provider 字节。
- 许可之后取消：允许 best-effort abort，但 billing knowledge 不能伪装 proven absent。
- `provider_completed_count <= provider_dispatch_count`。
- 每个 completed request 必须有 canonical usage，或明确 unknown。

## 8.2 Tool

- 同一 `(run, ordinal)` 只对应一个 normalized semantic request。
- succeeded replay 必须返回相同 IDs。
- failed replay 必须返回相同错误，不得变 unknown。
- running replay 不得伪装成功。
- result unknown 后不得自动重新提交 paid tool。
- tool limit 的每次尝试都必须计数并进入最终状态。

## 8.3 Compaction

- checkpoint 的 first/next entry 必须都能映射到 durable session projection。
- checkpoint phase 不可硬编码。
- ready checkpoint 必须可以在一个全新进程中恢复。
- checkpoint 替换是 session-level 原子操作。
- 不可恢复 checkpoint 不得静默降级为 ready。

## 8.4 图片

- Session catalog 与 current turn images 分离。
- 只有当前附件/显式选择图片进入 Provider 当前消息。
- Tool allowed labels 必须是本轮已授权集合。
- 目录达到上限前应可见、可管理。
- 删除/retention 后的 label 不得指向另一张历史图片而无 revision。

## 8.5 Terminal

- `length`、cancel、tool unknown、runtime shutdown、safety budget 均有不同终态/错误码。
- cleanup 失败不得覆盖已知 Provider usage。
- terminal event 恰好一次；
- PostgreSQL terminal 和 SSE projection 最终一致；
- partial 文本与成功工具副作用必须保留。

---

# 9. 推荐修复顺序

## Phase 0：先止住成本与一致性风险

1. 修 P1-03：检查 `update_dispatch_state()` 返回值。
2. 修 P1-02：Tool Gateway deadline、body cap、redirect。
3. 增加 P1-01 的 Pi 原生安全熔断。
4. 修 P2-03：所有工具失败进入统一状态。
5. 修 P2-02：按工具历史状态正确 replay。
6. 修 P2-01：length → partial/truncated。
7. Runtime admission 分层，降低 request/concurrency 默认值。

## Phase 1：修复长会话

1. 分离 session catalog 与 current images。
2. 修 compaction entry mapping 和 phase。
3. Session 上保存 active checkpoint 指针。
4. typed Pi projection 替代文本扁平化。
5. Continue 改成 server-side Pi continuation。
6. 增加 session image manager。

## Phase 2：真实 Provider 与恢复门禁

1. 本地 mock streaming provider。
2. OpenAI Responses/Completions/Anthropic contract。
3. abrupt EOF、429、5xx、length、abort。
4. direct/HTTP/SOCKS/SSH proxy。
5. Runtime SIGTERM/Worker restart/PostgreSQL restart/Redis 局部故障。
6. 8 并发最大图片内存压测。

## Phase 3：结构治理

1. 拆分 `Any` Ports。
2. 实现真正 config validator。
3. reasoning metadata 去硬编码。
4. 修 proxy round-robin key。
5. 补 `MEMORY.md` 或修改规范。
6. 扩展前端行为测试和 store 清理。

---

# 10. 建议增加的自动化测试矩阵

| 场景 | 期望 |
|---|---|
| cancel 在 starting fence 前 | Runtime 不建立请求，release hold |
| cancel 在 Runtime 已接收、Provider 未 dispatch | dispatch ticket 失败，Provider 不发字节 |
| cancel 在 Provider response 后 | partial/cancelled，usage 保留 |
| 模型无限未知工具 | safety breaker，partial，不无限运行 |
| 图片工具超次数 | tool.failed + Run partial/failed |
| 图片工具 count 超剩余额度 | 准确错误码，不能 succeeded |
| failed tool exact replay | 相同失败，不变 unknown |
| running tool replay | 409 in-progress |
| Provider length | partial + continue |
| pre-prompt compaction | 重启后可恢复 |
| overflow retry compaction | text.reset + 正确 checkpoint |
| post-turn compaction | 下一轮正确插入 |
| first kept 是当前 user | 映射正确，不 fallback |
| 64 张目录、当前只选 1 张 | Provider 只收到 1 张 |
| 纯文字追问 | Provider 收到 0 张当前图片 |
| 9 个未认证慢请求 | 合法请求仍有 run slot |
| 8 个 64 MiB 请求 | RSS 不越容器预算 |
| Runtime SIGTERM | stop ready、grace drain、最终 abort |
| Tool Gateway 永不响应 | 30 秒内终止并标 unknown |
| Tool Gateway 返回 1 MiB JSON | bounded failure |
| Gateway 302 | 拒绝，不跟随 |
| OpenAI abrupt EOF | unknown 证据正确 |
| Anthropic 429 | provider response/status 正确 |
| 60 个 generation | active/newest 全部实时更新 |
| Continue 含附件 | 不丢图片和 settings |
| Redis 主 lease renew 成功、secondary 失败 | 不标 lease lost |
| 两个不同 proxy pool 交错 | 每个 pool 独立轮询 |
| cleanup close 抛错 | 主结果/usage 不被覆盖 |

---

# 11. 可观测性建议

至少增加以下指标：

- `agent_run_wall_clock_seconds`
- `agent_provider_dispatches_per_run`
- `agent_turns_per_run`
- `agent_safety_breaker_total{reason}`
- `agent_current_turn_images`
- `agent_session_catalog_images`
- `agent_reference_preview_bytes`
- `agent_reference_preview_build_seconds`
- `agent_tool_gateway_seconds{outcome}`
- `agent_tool_gateway_response_bytes`
- `agent_cancel_to_runtime_abort_seconds`
- `agent_compaction_checkpoint_total{phase,outcome}`
- `agent_checkpoint_restore_total{outcome}`
- `agent_runtime_request_bytes`
- `agent_runtime_weighted_capacity`
- `generation_lease_secondary_refresh_failures`
- `proxy_round_robin_pool_cardinality`

日志和审计中不要写：

- API key；
- capability token；
- Base64 图片；
-完整 system prompt；
- 用户原始敏感文本。

---

# 12. 本轮已经排除的初步怀疑与旧问题

为了避免误报，本报告没有把以下项目列为现存 bug：

## 12.1 Pi async subscribe 顺序竞态

初看 `session.agent.subscribe(async ...)` 可能像 fire-and-forget，但 Pi `0.84.2` 的 Agent 实现会按顺序 await listeners，并在 Run settle 前等待，因此没有按初步怀疑列为竞态。

## 12.2 “system prompt 被重复计 token”

Runtime 的 preflight estimator 把 system prompt 放进一个 synthetic message 参与估算，但实际 Pi session 不把它作为第二条 user message发送。这是估算表达方式，不足以证明双倍发送。

## 12.3 Worker 数据库连接池旧问题

历史审计中的连接池问题已在当前基线修复，未重复列出。

## 12.4 Redis owned lock 无续租

当前 `owned_redis_lock` 已具备 owner token 和续租，不再沿用旧结论。

## 12.5 视频实际成本超过预估后静默少扣

当前 worker 会识别 `video_cost_exceeds_estimate`，记录高优先级日志并按真实成本完整 pass-through，不再静默回落到 hold。

## 12.6 Telegram 双击重复提交

当前代码已有提交 fence/锁定保护，未按旧审计重复报告。

## 12.7 Markdown raw HTML

虽然代码中存在 `dangerouslySetInnerHTML`，但调用链使用受控 Markdown/HTML 处理，不足以确认当前 XSS。

## 12.8 Shell 注入

全仓横向搜索未发现当前生产代码使用 `shell=True` 或 `os.system()` 的可确认命令注入路径。更新脚本使用参数数组和严格命令边界。

## 12.9 TLS `verify=False`

当前命中主要出现在文档/示例语境，没有确认生产请求关闭 TLS 验证。

---

# 13. 正向评价

当前代码中值得保留的设计：

1. Runtime 与 Worker 的 HMAC 原始 body 签名。
2. 固定时钟偏差、nonce replay 防护和 constant-time compare。
3. Provider origin pin、redirect 拒绝、DNS pin。
4. Pi built-in tools/resources/session file 默认隔离。
5. paid image tool 的 ordinal + normalized hash + semantic idempotency。
6. PostgreSQL execution epoch 和 event sequence。
7. Worker 对不确定上游结果不盲目重试。
8. 图片 Generation 与 Agent text 计费分离。
9. 视频真实 usage 的 pure pass-through 修复。
10. CI 中已有架构、lint、type、test 和 release gate。

这些基础说明项目不需要推倒重写。优先把生命周期和 session semantics 收紧，收益会非常大。

---

# 14. 最终判断

Lumen 的 Agent 实现已经从“自定义对话拼接”迈向了真正的 Pi Runtime，但当前仍处在边界迁移期：

- Pi loop 是原生的；
- 但历史消息仍被扁平化；
- compaction 仍通过不完整 ID 映射投影；
- 图片 catalog 被误当成每轮视觉内容；
- Continue 和 reasoning 仍由产品层硬编码；
- 生命周期安全从“过多 Host 限制”摆到了“几乎没有紧急保护”的另一个极端。

最合理的方向不是重新写一个 Lumen Agent loop，也不是完全放弃安全边界，而是：

> **让 Pi 原生负责正常行为；让 Lumen 用可验证、极少干预的外围熔断、计费许可、耐久投影和产品状态来保证安全。**

修完 Phase 0 和 Phase 1 后，Agent 的稳定性、图片语义、取消可信度、长会话恢复和 Pi 原生体验都会有明显提升。

再次强调：即使完成本报告全部整改，也不能仅凭静态审计宣称“没有任何 bug”。应以真实 Provider contract、故障注入、压力测试、长会话 soak test 和生产指标继续验证。

---

## 15. 主要证据索引

### Lumen

- [`AGENTS.md`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/AGENTS.md)
- [`docs/adr/0001-agent-runtime-pi-provider-boundary.md`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/docs/adr/0001-agent-runtime-pi-provider-boundary.md)
- [`apps/agent-runtime/src/contracts.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/agent-runtime/src/contracts.ts)
- [`apps/agent-runtime/src/runtime.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/agent-runtime/src/runtime.ts)
- [`apps/agent-runtime/src/server.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/agent-runtime/src/server.ts)
- [`apps/agent-runtime/src/auth.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/agent-runtime/src/auth.ts)
- [`apps/agent-runtime/src/providers/transport.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/agent-runtime/src/providers/transport.ts)
- [`apps/agent-runtime/src/providers/runtime-provider.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/agent-runtime/src/providers/runtime-provider.ts)
- [`apps/agent-runtime/src/tools/create-image.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/agent-runtime/src/tools/create-image.ts)
- [`apps/agent-runtime/src/tools/gateway.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/agent-runtime/src/tools/gateway.ts)
- [`apps/agent-runtime/tests/runtime.test.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/agent-runtime/tests/runtime.test.ts)
- [`apps/worker/app/agent_context.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b91bcb2aeb4d905eda6879/apps/worker/app/agent_context.py)
- [`apps/worker/app/agent_runtime_client.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/worker/app/agent_runtime_client.py)
- [`apps/worker/app/tasks/agent_run_parts/orchestrator.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/worker/app/tasks/agent_run_parts/orchestrator.py)
- [`apps/worker/app/tasks/agent_run_parts/persistence.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/worker/app/tasks/agent_run_parts/persistence.py)
- [`apps/worker/app/tasks/agent_run_parts/compaction_checkpoint.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/worker/app/tasks/agent_run_parts/compaction_checkpoint.py)
- [`apps/api/app/services/agent/message_submission.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/api/app/services/agent/message_submission.py)
- [`apps/api/app/services/agent/tools.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/api/app/services/agent/tools.py)
- [`apps/api/app/services/agent/runs.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/api/app/services/agent/runs.py)
- [`packages/core/lumen_core/schema_models/agent.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/packages/core/lumen_core/schema_models/agent.py)
- [`packages/core/lumen_core/agent_capability.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/packages/core/lumen_core/agent_capability.py)
- [`apps/web/src/features/agent/containers/AgentWorkspaceController.tsx`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/web/src/features/agent/containers/AgentWorkspaceController.tsx)
- [`apps/web/src/store/agent/useAgentStore.ts`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/web/src/store/agent/useAgentStore.ts)
- [`apps/worker/app/tasks/generation_parts/lease.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/worker/app/tasks/generation_parts/lease.py)
- [`apps/api/app/proxy_pool.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/api/app/proxy_pool.py)
- [`apps/worker/app/tasks/video_generation_parts/runtime.py`](https://github.com/cyeinfpro/Lumen/blob/bc8ccbb8190462c8b2b91bcb2aeb4d905eda6879/apps/worker/app/tasks/video_generation_parts/runtime.py)

### Pi 0.84.2

- [`packages/agent/src/agent.ts`](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/agent/src/agent.ts)
- [`packages/agent/src/agent-loop.ts`](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/agent/src/agent-loop.ts)
- [`packages/coding-agent/src/core/session-manager.ts`](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/coding-agent/src/core/session-manager.ts)
- [`packages/coding-agent/src/core/compaction/compaction.ts`](https://github.com/earendil-works/pi/blob/914cf1472e715297caa30db4b9535d534a9eb718/packages/coding-agent/src/core/compaction/compaction.ts)
