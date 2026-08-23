# Pi Agent 一级入口与对话/生图集成计划

> 状态：Implemented in v1.2.139
>
> 首期范围：新增一级导航 `Agent`，在独立 Agent 会话中使用 Pi 完成文本对话、文生图和图生图。
>
> 核心边界：Pi 负责 AgentSession、原生上下文压缩、推理循环、thinking level 和工具生命周期；Lumen 继续负责用户、业务会话事实、记忆、Provider 选择、任务、图片、计费、SSE、恢复和审计。

## 1. 目标

在现有一级导航中新增 `Agent`：

```text
创作 / Agent / 视频 / 项目 / 素材
```

移动端保留账户入口：

```text
创作 / Agent / 视频 / 项目 / 素材 / 我的
```

用户进入 `/agent` 后，可以：

1. 新建、切换、重命名、归档和删除 Agent 会话。
2. 与由 Pi 驱动的 Agent 进行流式文本对话。
3. 用自然语言要求 Agent 生成图片。
4. 上传或选择参考图，让 Agent 发起图生图。
5. 在同一条消息流里看到 Pi 工具调用、图片任务和最终图片。
6. 刷新页面、切换一级导航或短暂断网后恢复当前状态。
7. 继续使用 Lumen 现有的账号记忆、系统提示词、BYOK、Provider Pool、钱包和图片资产体系。

首期需要打通两条完整链路：

```text
文本目标
  -> Pi 直接回答
  -> 流式文本写入 Lumen Message
```

```text
文本目标 + 可选参考图
  -> Pi 判断是否调用图片工具
  -> Lumen 创建 TEXT_TO_IMAGE 或 IMAGE_TO_IMAGE Generation
  -> 图片 Worker 独立执行
  -> Agent 页面持续展示任务和图片结果
```

## 2. 产品定位与范围

### 2.1 Agent 与现有「创作」的区别

| 一级入口 | 核心交互 | 谁决定执行方式 | 能力 |
| --- | --- | --- | --- |
| 创作 | 用户显式选择聊天、生图、图生图、修图等模式 | 用户和现有 intent 路由 | 直接、可预测的创作工具 |
| Agent | 用户描述目标，Agent 自主决定回答或调用工具 | Pi Agent 循环 | 首期为对话、文生图、图生图 |

`Agent` 不是 Studio 的别名，也不复用同一个当前会话状态。两个入口可以复用底层图片任务、上传、图片卡片和灯箱，但会话列表、请求入口、运行状态和客户端 store 必须隔离。

### 2.2 首期包含

- 一级导航和 `/agent` 路由。
- 独立 Agent 会话列表。
- 文本消息流式输出。
- Pi 受控图片工具，支持文生图和图生图。
- 当前消息上传/选择参考图，支持结构化角色和顺序。
- 图片参数：数量、比例、1K/2K/4K、生成质量、背景、输出格式。
- Lumen 账号记忆和系统提示词注入。
- 钱包/BYOK 两种账户模式。
- Agent run、tool call、generation 的状态恢复、取消和审计。
- 桌面端、移动端、命令面板和全局任务托盘入口。
- 管理员功能开关、运行时健康状态和基础指标。

### 2.3 首期不做

- 不开放 Pi 的 `bash`、`read`、`write`、`edit`、`grep`、`find`、`ls`。
- 不加载用户目录或项目目录中的 Pi extension、skill、prompt、theme、`AGENTS.md`。
- 不支持任意第三方 Pi package。
- 不做局部重绘/mask、扩图、视频、Canvas、Storyboard、素材搜索和发布工具。
- 不做子 Agent、并行 Agent、计划模式和长时间后台自治。
- 不在 Web 暴露 Pi 的 `/model`、`/tree`、`/fork`、`/compact` 等终端命令。
- 不把 Pi JSONL session 当作产品数据源。
- 不让 Agent 在首个 run 中等待图片完成后自动评图或二次重绘。
- 不接入 Telegram；待 Web 链路稳定后再评估。

## 3. 现状与约束

### 3.1 可复用的 Lumen 基建

Lumen 已有：

- `Conversation`、`Message`、`Completion`、`Generation` 持久化。
- API 事务内写消息、任务和 Outbox，事务后入 arq 队列。
- Worker 执行 Provider 调用、状态机、重试、计费和图片落库。
- Redis Pub/Sub + Stream、SSE replay、任务快照和轮询降级。
- Provider Pool、代理、熔断、并发、BYOK 和钱包。
- 账号长期记忆、会话摘要和系统提示词。
- 全局任务托盘、图片卡片、灯箱、分享和资产流。

这些能力继续作为唯一生产执行层，Pi 不重复实现。

### 3.2 当前聊天工具边界

现有聊天通过 Responses API 开放：

```text
web_search
file_search
code_interpreter
image_generation
```

这些工具由上游托管。Pi 集成的新增价值是执行 Lumen 自有的结构化业务工具，并在一次用户请求中完成有限的「模型 -> 工具 -> 模型」循环。

### 3.3 运行时约束

Pi 是 Node/TypeScript SDK，Lumen API 和 Worker 是 Python，Web 是 Next.js。生产环境不应把有状态 Agent Runtime 放进 Next.js route handler，也不应在 Python Worker 镜像中临时安装 Node 并为每条消息启动 CLI 进程。

首期采用独立 Node 服务：

```text
apps/agent-runtime/
```

它只在后端网络中运行，使用 `@earendil-works/pi-coding-agent` SDK，不提供面向浏览器的公开端口。

## 4. 总体架构

```text
Browser
  |
  | POST /agent/sessions/{id}/messages
  v
FastAPI API
  | 事务：user message + assistant placeholder + agent_run + outbox
  v
PostgreSQL / Redis / arq
  |
  | run_agent(run_id)
  v
Python Worker
  | 选择 Lumen chat provider / BYOK credential
  | 组装系统提示词、记忆、完整 Pi retained tail 和会话图片资源
  | 调用内部 Agent Runtime，并消费 NDJSON 事件
  v
Node agent-runtime
  | Pi SDK AgentSession（in-memory）
  | 仅注册 Lumen 图片工具
  |
  +-- 文本模型调用 -> Lumen 选定的 chat provider
  |
  +-- lumen_create_image
        |
        | 受签名的内部 HTTP 请求
        v
      FastAPI Agent Tool Gateway
        | 无参考图 -> TEXT_TO_IMAGE
        | 有参考图 -> IMAGE_TO_IMAGE
        | 创建 Generation + billing hold + outbox
        v
      Existing Image Worker / Provider Pool / Storage

所有进度
  -> Worker/API 持久化快照
  -> Redis Stream/PubSub
  -> Existing SSE Hub
  -> Agent UI / Global Task Tray
```

## 5. 职责与安全边界

### 5.1 Pi 负责

- 根据用户文本和会话图片资源决定直接回答或调用图片工具。
- 通过原生 `SessionManager` 维护消息树和 compaction checkpoint。
- 在当前 prompt 前按原生 `shouldCompact`、`reserveTokens` 和 `keepRecentTokens` 规则自动调用一次 `session.compact()`。
- 执行模型与工具之间的有限轮次循环并产出标准事件。
- 原生管理 thinking level，并按模型能力对 `max` 做受控降级。
- 响应取消信号和宿主设置的轮次/工具上限。

### 5.2 Lumen 负责

- 用户身份、会话归属、CSRF、限流和权限。
- Agent 会话、消息、run、tool call 的持久化。
- Provider/BYOK 选择和凭据解密。
- 钱包预授权、结算、释放和用量审计。
- 参考图所有权、格式、角色和数量校验。
- 图片任务创建、幂等、队列、重试、存储和状态。
- SSE、断线恢复、任务托盘和跨设备同步。
- 账号记忆、系统提示词、隐私导出和账号删除。
- 功能开关、健康检查、部署和回滚。

### 5.3 禁止事项

- Agent Runtime 不直接访问 PostgreSQL、Redis、媒体存储或 Docker Socket。
- Agent Runtime 不接受浏览器 Cookie。
- Pi 不直接写 Lumen 数据库。
- Agent Runtime 不持久化 Provider API key、BYOK key、用户 prompt 或图片原文件。
- Web 不直接连接 Agent Runtime。
- 图片工具不能绕过现有 Generation、Provider Pool 和计费服务。
- 模型不能提交任意图片 ID；只能使用当前 run 获得授权的引用标签。

## 6. 一级导航与功能开关

### 6.1 路由契约

新增：

```text
/agent
/agent?session=<agent_session_id>
```

导航顺序：

```text
studio -> agent -> video -> projects -> assets -> me
```

需要同步的导航契约：

- `apps/web/src/lib/navigation.ts`
- `apps/web/src/components/ui/shell/MobileTabBar.tsx`
- `apps/web/src/components/ui/CommandPalette.tsx`
- `apps/web/src/components/IdleRouteWarmup.tsx`（通过共用导航列表自动纳入）
- `apps/web/src/components/ui/shell/PageTransitions.tsx`（通过 active nav key 自动纳入）

移动端图标使用 Lucide `Bot`；桌面沿用纯文字一级标签。移动端加入第六项后必须在 320px、375px 和横屏下验证标签、图标、44px 命中区及 safe area 不重叠。

### 6.2 两层开关

新增运行时设置：

```text
agent.enabled
ui.nav.agent_visible
```

规则：

- `agent.enabled=0`：Agent API fail closed，前端不允许进入 Agent 页面。
- `ui.nav.agent_visible=0`：隐藏一级入口，并把 `/agent` 重定向到第一个可见入口。
- 有效可见性为 `agent.enabled && ui.nav.agent_visible`。
- Agent Runtime 暂时故障时不动态隐藏标签，页面显示可恢复错误，避免导航抖动。
- 两项初始默认均为 `0`，本地和灰度环境显式打开；正式验收后再决定新安装默认值。

需要同步：

- Core `RuntimeDefaultsOut` / `NavigationVisibilityOut`。
- API auth runtime defaults 和 setting key map。
- Web `RuntimeDefaults`、Cookie warm cache、response validator、API types。
- 管理后台 UI 设置项。
- 环境变量 `AGENT_ENABLED`、`UI_NAV_AGENT_VISIBLE`。

## 7. 数据模型

首期复用 `Conversation` 和 `Message` 作为可见历史，但增加独立 Agent 领域表。不要只在 `Conversation.default_params` 中放松散的 `agent=true`，否则无法建立可靠的 run、tool call、幂等和恢复约束。

### 7.1 `agent_sessions`

```text
id                    uuid pk
user_id               uuid fk users(id) on delete cascade
conversation_id       uuid fk conversations(id) on delete cascade unique
runtime_version       text
created_at            timestamptz
updated_at            timestamptz

index (user_id, updated_at desc, id desc)
```

说明：

- 标题、归档、置顶、摘要、记忆 scope 继续来自关联 `Conversation`。
- `AgentSession` 是隔离 Studio 与 Agent 会话的正式标记。
- Studio 列表增加 `NOT EXISTS agent_sessions`；Agent 列表只查询已关联 conversation。
- Workflow/Canvas 隐藏会话继续沿用现有规则。

### 7.2 `agent_runs`

```text
id                    uuid pk
agent_session_id      uuid fk agent_sessions(id) on delete cascade
user_id               uuid fk users(id) on delete cascade
user_message_id       uuid fk messages(id) on delete restrict
assistant_message_id  uuid fk messages(id) on delete restrict
status                queued | running | succeeded | partial | failed | cancelled
execution_epoch       int default 0
idempotency_key       text
provider_name         text nullable
model                 text nullable
reasoning_effort      text nullable
turn_count            int default 0
tool_call_count       int default 0
usage_jsonb           jsonb nullable
error_code            text nullable
error_message         text nullable
started_at            timestamptz nullable
finished_at           timestamptz nullable
cancel_requested_at   timestamptz nullable
created_at            timestamptz
updated_at            timestamptz

unique (agent_session_id, idempotency_key)
index (user_id, status, created_at desc)
index (agent_session_id, created_at desc)
```

`partial` 表示至少一个有副作用的工具已经成功，但最终文本轮次失败。成功图片必须继续展示，且不能自动重放副作用。

### 7.3 `agent_tool_calls`

```text
id                    uuid pk
agent_run_id          uuid fk agent_runs(id) on delete cascade
pi_tool_call_id       text
ordinal               int
name                  text
status                queued | running | succeeded | failed | cancelled | timed_out
arguments_jsonb       jsonb
result_jsonb          jsonb nullable
semantic_key          text
started_at            timestamptz nullable
finished_at           timestamptz nullable
created_at            timestamptz
updated_at            timestamptz

unique (agent_run_id, pi_tool_call_id)
unique (agent_run_id, semantic_key)
```

约束：

- `arguments_jsonb` 只存 allowlist 业务参数，设尺寸和长度上限。
- `result_jsonb` 只存任务 ID、图片 ID、状态和安全摘要。
- 不存 base64、API key、完整 HTTP body 或签名 URL。
- `semantic_key` 由 `run_id + tool_name + ordinal + normalized_args_hash` 组成。

### 7.4 Message 内容

Agent assistant message 示例：

```json
{
  "text": "已经开始根据参考图生成 2 张图片。",
  "source": "agent",
  "agent_run_id": "...",
  "tool_calls": [
    {
      "id": "...",
      "name": "lumen_create_image",
      "label": "生成图片",
      "mode": "image_to_image",
      "status": "succeeded",
      "generation_ids": ["..."]
    }
  ],
  "generation_ids": ["..."]
}
```

内部错误、Provider 信息、凭据和原始工具响应必须由 `public_message_content()` 过滤。

## 8. API 设计

### 8.1 用户接口

```text
GET    /agent/sessions
POST   /agent/sessions
GET    /agent/sessions/{session_id}
PATCH  /agent/sessions/{session_id}
DELETE /agent/sessions/{session_id}

GET    /agent/sessions/{session_id}/messages
POST   /agent/sessions/{session_id}/messages

GET    /agent/runs/{run_id}
POST   /agent/runs/{run_id}/cancel
```

`POST /agent/sessions/{id}/messages`：

```json
{
  "idempotency_key": "client-generated-key",
  "text": "保留商品外观，把参考图改成极简杂志海报",
  "attachments": [
    {
      "image_id": "owned-image-id",
      "role": "product",
      "label": "商品图"
    }
  ],
  "image_defaults": {
    "count": 2,
    "aspect_ratio": "3:4",
    "quality": "2k",
    "render_quality": "high",
    "background": "auto",
    "output_format": "webp"
  }
}
```

响应：

```json
{
  "user_message": {},
  "assistant_message": {},
  "agent_run_id": "..."
}
```

请求成功只表示消息和 run 已可靠落库，不表示 Pi 或图片已经执行完成。

### 8.2 参考图处理

API 在创建 run 前：

1. 复用现有 attachment normalize、数量、格式、大小和所有权校验。
2. 固化引用顺序和角色。
3. 聚合整个 Agent 会话的历史上传图和已完成 Agent 生成图，去重后生成 run-scoped 标签：`ref_1`、`ref_2` 等。
4. Worker 生成有界 preview，作为 Pi 原生 prompt image content；会话资源最多保留 64 张。
5. Runtime 只获得标签、角色、显示名和 preview，不获得存储 key 或原始私有 URL。
6. 图片工具一次最多选择 16 个 `reference_labels`，对齐 GPT Image 2 edit 输入上限；Runtime 根据当前 run 映射为受控内部请求。
7. Tool Gateway 再次验证映射、用户所有权和 execution epoch。

历史图片只在所属 Agent 会话内自动延续，不扩展到整个账号素材库。业务数据库保留全部消息和资源；达到 64 张会话资源上限时明确拒绝新增，不静默淘汰旧图。

若当前 chat Provider 不支持 image input：

- 带参考图的 Agent 请求在 preflight 返回明确的 `agent_vision_model_unavailable`。
- 不允许静默丢弃图片后按纯文本执行。
- 纯文本和文生图仍可使用文本模型。

### 8.3 内部工具网关

```text
POST /internal/agent/runs/{run_id}/tools/create-image
```

内部请求携带短期、run-scoped 签名：

```text
run_id
user_id
agent_session_id
allowed_tools
allowed_reference_labels
execution_epoch
expires_at
nonce
signature
```

FastAPI 再次执行：

- Agent run 所有权和状态校验。
- execution epoch 校验。
- tool allowlist 校验。
- reference label 映射和图片所有权校验。
- 参数 schema 校验。
- 钱包/BYOK 和图片 Provider preflight。
- 工具语义幂等校验。
- Generation、hold、Outbox 的原子创建。

Runtime/模型提供的 user ID、价格、Provider、图片 ID、存储 key 或 URL 一律不可信。

### 8.4 健康接口

Agent Runtime 提供后端可见接口：

```text
GET /healthz
GET /readyz
```

`healthz` 只检查进程；`readyz` 检查 Pi SDK 初始化、工具注册、事件编码器和运行时配置，不调用收费模型。

## 9. Pi Runtime 设计

### 9.1 目录与依赖

新增：

```text
apps/agent-runtime/package.json
apps/agent-runtime/package-lock.json
apps/agent-runtime/tsconfig.json
apps/agent-runtime/Dockerfile
apps/agent-runtime/src/server.ts
apps/agent-runtime/src/runtime.ts
apps/agent-runtime/src/contracts.ts
apps/agent-runtime/src/providers/
apps/agent-runtime/src/tools/
apps/agent-runtime/tests/
```

要求：

- 精确锁定 `@earendil-works/pi-coding-agent` 版本和完整 lockfile。
- 构建时禁用不需要的 lifecycle script，并纳入依赖/SBOM 扫描。
- 使用非 root 用户和只读根文件系统。
- 不挂载 Lumen 仓库、媒体目录、Docker Socket、用户 home 或 Pi 配置目录。
- 只开放 Docker backend network。

### 9.2 Session 创建

每个 Agent run 从 Lumen 业务消息和最近的 Pi checkpoint 重建同一逻辑 Agent session：

```text
SessionManager.inMemory(agent_session_id)
SettingsManager.inMemory(...)
custom ResourceLoader
no builtin tools
customTools = [lumen_create_image]
```

必须关闭默认资源发现：

- 不扫描 `~/.pi/agent`。
- 不扫描 `.pi`、`.agents`、`AGENTS.md` 或 `CLAUDE.md`。
- 不加载 extension、skill、prompt template 和 theme。
- 不把 `cwd` 暴露为可操作文件空间。

Lumen Postgres 是业务会话事实源；Pi 原生 pre-prompt compaction 产生的 summary、`firstKeptEntryId` 对应业务 message ID、来源 run 的 user-message continuation boundary、usage 和 token 边界通过 epoch-fenced 事件写入来源 `AgentRun.dispatch_jsonb.pi_compaction`。来源 run 自 checkpoint 持久化起不可重放；后续 run 选择最新 ready checkpoint，在原消息树位置恢复 Pi summary + retained tail。容器本地不持久化 JSONL，也不在持有 Run 行锁时更新 Session 行。

### 9.3 上下文包

Worker 发送完整、净化且有传输安全上限的上下文：

```text
Lumen Agent 基础系统提示词
  > 用户/管理员系统提示词
  > 账号 profile / constraints
  > 相关账号记忆
  > Pi compaction checkpoint + retained tail（或首次运行的完整历史）
  > 当前用户文本 + 会话图片 previews
```

不允许 Agent Runtime 自己访问 Lumen memory API。Worker 不生成 Agent conversation summary，也不按自定义 token 预算裁剪历史；Pi 使用原生 `SessionManager`、`estimateTokens`、`shouldCompact` 和 `session.compact()` 管理上下文。为避免流式文本或付费工具副作用被重复执行，当前 prompt 开始前关闭 post-turn/overflow auto-compaction；超限时失败关闭，不自动重放。

历史转换保留：

- 用户和助手文本。
- 安全的图片任务摘要。
- 已完成工具调用的名称、参数摘要和结果摘要。
- 仍在运行的 generation ID 和状态。

不重放原始文件、不重放过期签名 URL、不注入内部错误堆栈。属于当前 Agent 会话且仍满足所有权/保留期校验的图片才转换为有界 preview，并通过 Pi 原生 image content 发送；Worker 读取 preview 和 Tool Gateway 兑换 label 时都会重新检查所有权、ready、软删除及 BYOK retention。会话 catalog 首次分配稳定 `ref_N`，后续重新附加只更新角色而不重编号；当前可见 ready 图片与 queued/running Generation reservation 共同受 64 张会话上限约束。

### 9.4 Provider 接入

Lumen 仍是 Provider 选择权威：

1. Worker 按 `purpose=chat`、账户模式、输入模态和健康状态选择 Provider。
2. 带参考图时只选择支持 image input 的 chat model。
3. Worker 解析模型、base URL、必要 headers、代理和 BYOK credential。
4. Runtime 只为当前 run 创建 in-memory provider/model，不写 `auth.json` 或 `models.json`。
5. Pi 每个 model turn 以及 pre-prompt compaction 的 usage、状态和错误回传 Worker；滚动升级中仅旧 Runtime 明确 HTTP 400/413 且尚未进入 NDJSON、完整上下文仍满足旧 256 条历史/4 张参考图/8 MiB 合同时，允许一次包含 Pi summary 的 legacy envelope 降级，否则失败关闭。
6. Worker 写入 Lumen Provider 健康统计和 Agent 账单。
7. 明确发现 GPT-5.6 model ID、但供应商未声明 metadata 时使用 `272000` context 家族档案；没有 model catalog 的 wildcard Provider 保持保守 `128000`。Agent reasoning 默认 `max`，Pi 再按具体模型支持的 thinking levels 原生 clamp。

正式实现前必须完成 Phase 0 兼容性验证：

- OpenAI Responses 流式文本。
- 文本输入和图片输入。
- tool call / tool result 循环。
- reasoning level 映射。
- usage 和缓存 token。
- HTTP/SOCKS/SSH proxy 路径。
- abort、429、5xx、断流和超时。

若 Pi 默认 transport 无法复用某类代理，先实现受测的自定义 provider transport；不得静默绕过代理，也不得回退到前端提供 API key。

### 9.5 运行上限

首期默认：

```text
agent.max_turns = 6
agent.max_tool_calls = 3
agent.max_image_tool_calls = 2
agent.max_images_per_run = 4
agent.max_reference_images = 16
agent.max_session_images = 64
agent.run_timeout_seconds = 600
agent.tool_timeout_seconds = 30
agent.capability_ttl_seconds = 900
```

`agent.run_timeout_seconds` 可配置范围为 10–1500 秒；ARQ 外层任务保持 1800 秒，为上下文构建、Runtime 交付、终态持久化和账单结算保留 300 秒包络。Worker 在请求中声明 `heartbeat-v1` 后，Runtime 每 15 秒发送一次内部心跳；未声明该能力的旧 Worker 不会收到新事件，保证滚动升级兼容。Worker 的默认事件空闲判定为 90 秒，并要求该值大于心跳间隔的两倍。心跳会更新运行检查点，避免慢首字、长推理和 5 分钟 stale-run 对账把健康任务误判为断流或失联。工具凭证有效期会自动提升到至少覆盖 `run timeout + tool timeout + clock skew`，且仍受 active run 与 execution epoch 双重约束。

达到上限后：

- 停止后续工具执行。
- 让 Pi 进行一次不带工具的受限收尾；失败则由宿主写明确错误。
- 已创建 Generation 不取消、不重复创建。
- 已产生文本或图片副作用时落为 `partial`，保留原始错误码并向用户显示具体恢复提示。
- 记录 `limit_reason` 指标和审计信息。

## 10. 图片工具设计

### 10.1 单一工具

Pi 只看到一个严格工具：

```text
lumen_create_image
```

参数：

```json
{
  "prompt": "string, 1..10000",
  "reference_labels": ["ref_1"],
  "count": "integer, 1..4",
  "aspect_ratio": "Lumen AspectRatio enum",
  "quality": "1k | 2k | 4k",
  "render_quality": "auto | low | medium | high",
  "background": "auto | opaque | transparent",
  "output_format": "png | jpeg | webp"
}
```

路由规则：

```text
reference_labels 为空  -> Intent.TEXT_TO_IMAGE
reference_labels 非空  -> Intent.IMAGE_TO_IMAGE
```

其他规则：

- 模型只能引用当前 run 提供的 labels。
- 用户未明确参数时采用请求中的 `image_defaults`。
- 用户明确要求的参数优先，但仍受 Lumen schema、Provider 能力和钱包约束。
- 多张参考图保留顺序和角色，第一张继续作为 primary reference。
- 透明背景与 JPEG 的归一化继续复用现有 Core schema。
- Tool description 明确说明：提交后图片会异步出现在 Lumen，返回 generation IDs，不要在同一 run 中忙轮询。
- Prompt、labels 和工具结果均有字符/字节上限。

### 10.2 执行流程

```text
Pi tool call
  -> Runtime 校验 reference labels 并生成 semantic_key
  -> 调用内部 Agent Tool Gateway
  -> API 锁定 agent_run + tool ordinal
  -> 解析 labels 为 owned image IDs
  -> 无引用走 TEXT_TO_IMAGE，有引用走 IMAGE_TO_IMAGE
  -> 复用 resolve_size / billing / Generation / Outbox
  -> 返回 generation_ids + mode + accepted params
  -> Pi 得到结构化 tool result
  -> Pi 输出简短确认或继续回答
  -> 图片 Worker 独立执行
  -> generation SSE 更新原 assistant message
```

工具不等待图片完成，原因：

- 避免 Agent run 长时间占用 Worker 和 Runtime。
- 避免低 Worker 并发下父 Agent 等待子 Generation 造成队列饥饿。
- 保留 Lumen「关窗不丢任务」语义。
- 图片失败、重试、取消继续使用已有独立任务控制。

后续若需要自动评图，应设计 generation terminal event 驱动的 continuation run，而不是在首个工具调用里阻塞。

### 10.3 幂等

三层幂等：

1. 用户消息：`agent_session_id + client idempotency_key`。
2. Agent 工具：`agent_run_id + semantic_key`。
3. 图片子任务：复用现有 generation child idempotency key。

Runtime/Worker 重试时：

- 同 semantic key 返回原 tool call 和 generation IDs。
- 同 ordinal 但参数 hash 不同返回冲突，不创建第二批图片。
- 只有新的 Agent run 才允许用户主动再次生成相同 prompt。

## 11. 状态、事件与恢复

### 11.1 Agent run 状态机

```text
queued
  -> running
  -> succeeded
  -> partial
  -> failed
  -> cancelled
```

终态不可逆；所有写入携带 `execution_epoch`，过期 Runtime/Worker 不能覆盖新执行者。

### 11.2 Tool call 状态机

```text
queued
  -> running
  -> succeeded
  -> failed
  -> cancelled
  -> timed_out
```

每个 running tool 必须得到终态。Runtime 断流时：

- 工具尚未提交：标记 failed，可安全重试 run。
- Generation 已创建：工具标记 succeeded，run 标记 partial 或从快照继续，禁止重新提交图片。
- 结果未知：标记 `tool_result_unknown`，不自动重复有成本动作。

### 11.3 SSE 事件

新增事件族：

```text
agent.run.queued
agent.run.started
agent.output.delta
agent.tool.started
agent.tool.updated
agent.tool.succeeded
agent.tool.failed
agent.run.succeeded
agent.run.partial
agent.run.failed
agent.run.cancelled
```

频道：

```text
agent:{agent_session_id}
task:{generation_id}
user:{user_id}
```

事件 envelope 至少包含：

```text
agent_session_id
agent_run_id
assistant_message_id
execution_epoch
event_seq
```

图片继续发送现有 `generation.*` 事件，不复制 Agent 图片状态协议。

### 11.4 快照恢复

页面启动或 SSE 重连：

1. 拉取 Agent session/messages 快照。
2. 拉取当前 session 的非终态 Agent run。
3. 通过现有 active tasks 快照恢复 Generation。
4. 再连接 SSE，用 replay cursor 增量补齐。
5. SSE 不可用时轮询 Agent run 和 active tasks。

Worker 流式文本继续周期性数据库 flush，避免长文本只存在于 Redis。

### 11.5 取消

- 取消 Agent run：终止 Pi 当前模型调用并禁止新的工具回调。
- 已可靠创建的 Generation 不随 Agent run 自动取消；它是独立、有成本且可恢复的任务。
- 用户可从图片任务卡或全局任务托盘单独取消 Generation。
- 取消与新工具提交竞争时，以 API 对 agent_run 行锁和 `cancel_requested_at` 为准。

## 12. 计费、Provider 与记忆

### 12.1 Agent 文本计费

- 创建 run 前按模型、输出上限和最大轮次预授权。
- Pi 回传每个 model turn usage，Worker 聚合到 `agent_runs.usage_jsonb`。
- 成功按实际 usage 结算。
- Provider 已收到请求但结果未知时沿用 unknown settlement，不按零成本释放。
- Runtime preflight 失败且未发上游时释放 hold。
- `WalletTransaction.ref_type = agent_run`，记录 provider、model、turn_count、tool_count 和 reasoning。

### 12.2 图片计费

每次 `lumen_create_image` 直接复用现有 Generation hold/settle/release，不并入 Agent 文本账单。文生图和图生图都使用现有图片价格、倍率、像素档位和 BYOK 路径。

用量页面区分：

```text
Agent 对话费用
Agent 文生图费用
Agent 图生图费用
```

### 12.3 BYOK

- Agent chat 按 `purpose=chat` 解析用户凭据。
- Agent image tool 按 `purpose=image` 解析用户凭据。
- Runtime credential 只存在于内存，不写日志、磁盘、Pi auth store 或错误 payload。
- BYOK retention、导出和删除策略覆盖 Agent message/run/tool call。

### 12.4 记忆

- Agent session 使用关联 Conversation 的 `memory_disabled` 和 `active_scope_id`。
- 复用账号记忆检索、注入和 `used_memory_summary` 展示。
- Agent 回复完成后可进入现有记忆抽取链，但工具参数、图片 prompt 和工具结果不作为用户事实来源。
- Agent 与 Studio 共享账号记忆；会话摘要各自独立。

## 13. 安全设计

### 13.1 最小工具权限

首期有效工具必须精确等于：

```text
[lumen_create_image]
```

用户关闭「允许生图」后应为：

```text
[]
```

测试必须证明即使模型请求 `bash`、`read`、`write`、任意 URL fetch、任意图片 ID 或未注册工具，也无法执行。

### 13.2 服务认证

- Worker -> Runtime 使用 `AGENT_RUNTIME_SHARED_SECRET` 签名请求。
- Runtime -> Tool Gateway 使用短期 run-scoped capability token。
- Token 绑定 run、user、session、epoch、allowed tools、allowed references 和过期时间。
- nonce 防重放；签名比较 constant-time。
- 内部端口只加入 `lumen_backend`，不映射宿主公网端口。
- 不以来源 IP 作为唯一认证手段。

### 13.3 Prompt injection 防线

- 用户 prompt 可以影响模型，但不能扩大工具集合或 reference allowlist。
- 工具 schema 和内部 API 双重校验。
- 系统提示词明确禁止泄露内部 prompt、credential、tool token 和 Provider 配置。
- Tool Gateway 忽略模型提供的 user_id、价格、Provider、存储 key 和 callback URL。
- 错误回给模型前做白名单映射，不发送堆栈和内部网络信息。

### 13.4 数据与日志

- 日志不记录完整 prompt、图片 base64、Cookie、capability token、Provider key 和 BYOK key。
- arguments/result 只保存有界、可公开字段。
- Sentry `send_default_pii=false`，增加 Agent scrubber 测试。
- Agent 数据纳入账号导出、账号删除、会话删除和 retention job。

## 14. 前端设计

### 14.1 页面结构

桌面：

```text
DesktopTopNav(active="agent")
  +-- Agent session sidebar
  +-- Agent context bar
  +-- Agent conversation turns
  +-- Agent composer
```

移动端：

```text
Agent top bar
  +-- scrollable conversation
  +-- task island
  +-- Agent composer
  +-- MobileTabBar
```

页面是实际工作区，不做 landing page，不使用营销式 hero。空状态只提供少量可直接提交的目标建议。

### 14.2 Store 与 Query 隔离

新增：

```text
apps/web/src/features/agent/
apps/web/src/store/agent/
```

建议 query key：

```text
["agent-sessions"]
["agent-session", sessionId]
["agent-messages", sessionId]
["agent-run", runId]
```

不能直接复用 `useChatStore.currentConvId`，否则切换 `创作` 与 `Agent` 会互相清空消息、草稿或错投请求。

可以复用/抽取的纯展示层：

- Markdown。
- 上传和附件预览 primitives。
- 图片卡片与灯箱。
- Generation 状态展示。
- TaskIsland / GlobalTaskTray。
- Composer 基础输入和图片参数 primitives。

不为了复用而让 Agent 组件依赖 Studio intent、Studio mode 或 Studio 当前会话。

### 14.3 Composer

首期 Composer：

- 核心层：参考图附件、文本输入、发送、停止。
- 摘要层：当前默认图片数量、比例、质量。
- 设置层：允许生图开关和完整 image defaults。
- 不显示 Studio 的聊天/生图 mode segmented control；是否调用工具由 Pi 决定。
- 用户关闭「允许生图」时，该 run 不注册 `lumen_create_image`。
- 参考图附件保留角色、顺序、移除和预览操作。
- 从 Agent 结果图点击「用作参考」时加入当前 Agent Composer，不切到 Studio。

参数几何保持稳定，设置展开不能导致输入框跳动。移动端设置使用现有 BottomSheet 和 safe-area primitives。

### 14.4 消息与工具展示

Agent turn 依次显示：

1. 用户消息和参考图。
2. Agent 流式文本。
3. 工具状态，例如「图生图 · 已提交 2 个任务」。
4. 现有 Generation 骨架和图片卡片。
5. partial/failure 的明确状态和可执行操作。

不展示原始 chain-of-thought。Provider 若提供可公开 reasoning summary，可沿用当前 thinking summary 规则；否则只显示普通运行状态。

### 14.5 视觉规范

- 使用 `apps/web/DESIGN.md` 的 semantic tokens、type classes、radius 和 surface primitives。
- 页面 section 不做浮动卡片，消息、工具和重复资产才用卡片。
- 不新增硬编码暗色；媒体舞台、图片覆盖控件和 scrim 例外。
- Desktop App Bar、Mobile Top Bar、Mobile Tab Bar 与现有 shell 一致。
- 所有状态有文本或 ARIA 表达，不能只靠颜色。
- 尊重 `prefers-reduced-motion`。

### 14.6 全局任务托盘

Agent 创建的 Generation 写入：

```text
source=agent
action_source=agent.create_image
agent_session_id
agent_run_id
```

任务卡点击跳转：

```text
/agent?session=<id>&scrollTo=<assistant_message_id>
```

需要扩展 TaskItem 输出和前端 route resolver，不能把 Agent 任务错误跳回 Studio。

## 15. 模块与文件规划

建议新增：

```text
packages/core/lumen_core/agent_models.py
packages/core/lumen_core/agent_schemas.py
packages/core/lumen_core/agent_events.py

apps/api/app/routes/agent_sessions.py
apps/api/app/routes/agent_runs.py
apps/api/app/routes/internal_agent_tools.py
apps/api/app/services/agent_sessions/
apps/api/app/services/agent_tools/

apps/worker/app/tasks/agent_run.py
apps/worker/app/agent_runtime_client.py
apps/worker/app/agent_context.py
apps/worker/app/agent_billing.py

apps/web/src/app/agent/page.tsx
apps/web/src/features/agent/
apps/web/src/store/agent/

apps/agent-runtime/
```

原则：

- Core 只放共享 entity/schema/event contract，不放 API/Worker runtime state。
- API 负责事务、权限和工具副作用入口。
- Worker 负责编排、Provider 选择、计费和事件持久化。
- Runtime 只负责 Pi session、模型循环和受控工具客户端。
- Web 只通过公共 API/SSE 工作，不感知 capability token。

## 16. 分阶段实施

### Phase 0：技术验证与 ADR

- 建最小 Runtime spike。
- 用 fake model 验证 text -> tool -> text 事件顺序。
- 用实际 chat Provider 验证文本、图片输入、tool calling、usage、reasoning、abort。
- 验证 HTTP/SOCKS/SSH proxy。
- 验证完全关闭 built-in tools 和资源发现。
- 验证 NDJSON framing、背压、断流和输出上限。
- 形成 ADR，确认 provider adapter 和 credential envelope。

退出条件：不存在未解决的 key 持久化、代理绕过、图片输入丢失或 usage 缺失。

### Phase 1：导航和闸门

- 新增 `agent.enabled`、`ui.nav.agent_visible`。
- 扩展 Core/API/Web runtime defaults。
- 加入共享 navigation policy。
- 更新 Desktop、Mobile、Command Palette、route guard 和测试。
- 新增 `/agent` disabled/loading shell。

退出条件：开关行为完整，320px 移动端六标签不溢出、不重叠。

### Phase 2：Agent 数据和文本对话

- 新增 migration、ORM、schemas 和 session/run API。
- 隔离 Agent/Studio conversation。
- Outbox + `run_agent` Worker。
- Runtime 正式服务和内部认证。
- Pi text streaming -> persistence -> SSE -> Web。
- Agent sidebar、conversation、composer 和取消。
- 记忆、系统提示词、auto title、摘要基础接入。

退出条件：文本对话可刷新恢复，Runtime/Worker 重启不产生重复消息，账户严格隔离。

### Phase 3：文生图和图生图

- Composer 支持上传/选择参考图、角色和顺序。
- 当前 run reference labels 和 vision preview。
- 注册唯一工具 `lumen_create_image`。
- 增加 Tool Gateway 和 run-scoped token。
- 无 refs 复用 text-to-image，有 refs 复用 image-to-image。
- Agent tool events 与 assistant message/generation 关联。
- 图片 skeleton、图片卡和任务托盘跳转。
- 工具幂等、partial success 和结果未知处理。

退出条件：自然语言可触发文生图和图生图；刷新、断流、重复投递不重复扣费或重复生图。

### Phase 4：计费、恢复与可观测性

- Agent run wallet hold/settle/release/unknown。
- BYOK credential 和 retention。
- Agent reconciler、stale epoch、DLQ。
- SSE replay + snapshot + polling fallback。
- 指标、trace、日志 scrubber、管理后台状态。
- 备份、隐私导出、账号删除和数据维护。

退出条件：计费守恒，崩溃/超时/重复回调/取消竞态均有测试。

### Phase 5：灰度与开放

- 默认关闭，在测试环境打开。
- fake Provider 完整 E2E。
- 小范围真实 Provider 灰度。
- 观察错误率、重复工具率、平均 turn、费用、图生图失败率和取消率。
- 通过发布门禁后决定新安装默认值。

## 17. 测试计划

### 17.1 Core/API

- Agent schema 严格拒绝额外字段。
- 状态终态不可逆。
- ORM、索引、唯一约束和 migration。
- session CRUD 所有权、分页、搜索和隔离。
- message idempotency 和并发重复提交。
- attachment 所有权、角色、顺序、格式和数量。
- capability token：篡改、过期、重放、错 user、错 epoch、错 tool、错 reference。
- 文生图/图生图路由和参数归一化。
- 余额不足、BYOK purpose、Provider/vision model 缺失。
- tool semantic idempotency 和 Generation 原子创建。
- cancel 与 tool submit 竞态。
- privacy export/delete/retention。

### 17.2 Worker/Runtime

- Pi NDJSON event 映射和 delta flush。
- agent/tool 终态保证。
- provider pre-dispatch failure 与 post-dispatch unknown。
- Runtime 断流、timeout、abort、stale epoch。
- 工具提交后断流不重复图片。
- Agent 文本计费守恒。
- memory/context packing token 预算。
- built-in tools 和资源发现关闭。
- 只有 allowlist 工具可调用。
- reference labels 无法越权。
- 文本模型不能接收带图 run。
- max turns/tools/images/references/timeout。
- secret 不进入日志、错误和 session state。

### 17.3 Web

- `AppNavKey`、可见性、active route、hidden redirect。
- Desktop Agent tab、Mobile Bot tab、Command Palette。
- Studio store 与 Agent store 隔离。
- Agent session 切换、草稿、分页和 optimistic message。
- 参考图上传、排序、角色、移除、预览和「用作参考」。
- text delta、tool status、Generation SSE reconciliation。
- refresh/reconnect snapshot。
- partial、failed、cancelled、runtime unavailable、insufficient balance。
- task tray 跳转 Agent 原消息。

### 17.4 E2E

至少覆盖：

1. 新建 Agent 会话并完成纯文本回复。
2. 无参考图时自然语言触发文生图。
3. 附加一张图片时触发图生图并保留 primary reference。
4. 附加多张不同角色图片时顺序和角色正确传递。
5. 图片仍在生成时刷新，状态恢复且无重复任务。
6. Runtime 在文本流中断开，run 正确失败或恢复。
7. Runtime 在图片工具提交后断开，图片只创建一次并作为 partial 显示。
8. 用户取消 run 后不能再提交工具。
9. 余额不足、BYOK 不可用、vision model 不可用时不创建图片。
10. 两个用户并发运行，消息、参考图、事件和 token 不串号。

视口：

```text
320 x 700
375 x 812
768 x 1024
1024 x 768
1440 x 900
```

检查导航不重叠、文字不溢出、Composer 不遮消息、键盘打开时底栏隐藏、参考图可辨识、最终图片非空且灯箱可用。

## 18. 可观测性

建议指标：

```text
agent_runs_total{status,provider,model}
agent_run_duration_seconds{status}
agent_turns_histogram
agent_tool_calls_total{name,mode,status}
agent_tool_duration_seconds{name,mode,status}
agent_limits_total{reason}
agent_runtime_requests_total{status}
agent_runtime_disconnects_total{phase}
agent_provider_usage_tokens_total{kind}
agent_generation_links_total{mode,status}
agent_partial_runs_total{reason}
agent_reference_images_histogram
```

Trace 字段：

```text
trace_id
user_id_hash
agent_session_id
agent_run_id
agent_tool_call_id
generation_id
provider_name
model
execution_epoch
```

禁止把 prompt、记忆原文、图片 ID、API key 和 capability token 作为 metric label 或普通日志字段。

## 19. 部署与发布

### 19.1 Docker

新增 `agent-runtime`：

- 独立 immutable image digest。
- 非 root、只读 rootfs、`no-new-privileges`、drop capabilities。
- CPU/内存上限和 stop grace period。
- 只加入 `lumen_backend`。
- 不发布宿主端口。
- Worker 调用 `http://agent-runtime:<port>`。
- `agent.enabled=1` 时 readiness/update preflight 要求 Runtime healthy。

### 19.2 Release

同步：

- GitHub Actions build/push Runtime image。
- release manifest、SBOM、签名和 immutable image checker。
- `docker-compose.yml`、blue/green compose、dev compose。
- installer/update/rollback/health check。
- `.env.example` 和 README 架构/配置说明。
- 版本同步脚本的新 package target。

正式发布遵循仓库 release 规则：版本 bump、sync/check、测试、推送 main、创建 `vX.Y.Z` tag，并等待 tag 触发的 Docker Release 成功。

## 20. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 两套会话事实源 | 历史漂移、恢复困难 | Pi session 只用 in-memory，Postgres 唯一权威 |
| Pi 工具权限过大 | 服务器文件或命令泄露 | 关闭 built-ins/资源发现，只注册图片工具 |
| Agent 循环失控 | token 和图片成本失控 | turn/tool/image 上限、预授权、timeout、无工具收尾 |
| Runtime 重试重复生图 | 重复扣费和资产 | run/tool/generation 三层幂等，unknown 不重放 |
| Agent 等待子任务 | Worker 队列饥饿 | 图片工具提交即返回，Generation 独立完成 |
| Provider/代理不兼容 | Agent 无法使用现有供应商 | Phase 0 验证，不通过先做自定义 transport |
| BYOK key 跨服务 | 凭据泄露 | backend-only、run-scoped in-memory credential、禁日志/持久化 |
| 模型伪造参考图 ID | 越权读取他人图片 | 只给 labels，API 双重所有权校验 |
| 文本模型忽略参考图 | 图生图意图失真 | 带图请求强制 vision-capable chat model preflight |
| Agent/Studio 会话串页 | 错投消息、状态污染 | `agent_sessions` 隔离 + 独立 API/query/store |
| 六个移动 Tab 过密 | 标签或命中区重叠 | 320px 起布局测试，不缩小到 44px 以下 |
| 图片成功但最终文本失败 | 已付费结果不可见 | `partial` 终态 + 图片继续显示 + fallback 文案 |
| Runtime 成为新单点 | Agent 不可用 | 健康检查、重启、超时、明确降级；不影响其他入口 |

## 21. 验收标准

### 21.1 导航

- 开关开启后，桌面和移动端存在唯一一级 `Agent` 标签。
- `/agent` 始终高亮 Agent。
- 命令面板可搜索并进入 Agent。
- 开关关闭后入口消失，直达路由安全处理。
- 所有业务导航关闭时仍回退到 `/me`。

### 21.2 对话

- Agent 会话不出现在 Studio，Studio 会话不出现在 Agent。
- 文本回复流式显示并定期持久化。
- 刷新、切页、重连后不丢消息、不重复回复。
- 停止、失败、重试和服务不可用有明确状态。

### 21.3 文生图与图生图

- 用户不切换模式即可通过自然语言触发图片工具。
- 无参考图创建 text-to-image Generation。
- 有参考图创建 image-to-image Generation，顺序和角色正确。
- Pi 只能使用当前 run 的 reference labels，不能访问任意资产。
- Generation 使用现有 Provider Pool、BYOK、计费、队列、存储和 SSE。
- 工具重试不会重复创建图片或重复扣费。
- 图片在原 Agent turn 和全局任务托盘中正确显示、跳转和恢复。

### 21.4 安全与账务

- 用户隔离、CSRF、限流、内部签名和 token replay 测试通过。
- Agent 文本、文生图和图生图费用分别守恒。
- API key、BYOK key、capability token 和内部错误不出现在 Web、SSE、日志或导出文件。
- 账号删除和隐私导出覆盖 Agent 数据。

### 21.5 工程门禁

至少执行：

```bash
git diff --check
bash scripts/test.sh -q
```

前端：

```bash
cd apps/web
npm test
npm run type-check
npm run lint
npm run build
```

Runtime：

```bash
cd apps/agent-runtime
npm test
npm run type-check
npm run lint
npm run build
```

并完成 Playwright 桌面/移动截图、控制台错误检查、SSE 重连、参考图呈现和生成图片像素非空验证。

## 22. 后续扩展

首期稳定后建议依次增加：

1. 局部重绘、扩图和 upscale 工具。
2. 图片完成事件触发的 Agent continuation，用于评图和有限重绘。
3. 素材检索与引用。
4. 视频生成。
5. Canvas/Storyboard 工具。
6. Agent 会话分支与创意方向比较。
7. Telegram Agent 入口。

每增加一个工具，都必须单独定义权限、参数 schema、成本预算、幂等键、取消语义、结果未知策略、公开结果字段和审计字段。不得因为 Pi 支持任意 extension，就把未经治理的工具直接暴露给生产用户。
