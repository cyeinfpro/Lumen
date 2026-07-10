# 创作对话功能 端到端稳定性与鲁棒性加固方案

> 范围：首页 `/`（`DesktopStudio` / `MobileStudio`）→ 输入框 → POST `/conversations/{id}/messages` → Outbox → Worker `run_completion` / `run_generation` → 上游 Responses API（含 `web_search` / `file_search` / `code_interpreter` / `image_generation` 四个工具）→ Redis Stream → SSE → `useChatStore.applySSEEvent` → 消息渲染 / 工具状态卡 / 生图视图。
>
> 目标：所有用户能从 UI 触发的 tool 调用都可观察、可恢复、可取消、可计费；端到端无静默失败；断网/刷新/多 tab/重试不破坏会话一致性。
>
> 本文遵循仓库既有 P0/P1/P2 分级与 `docs/*-plan.md` 文档体例。所有引用都给出 `file:line`，便于实施者直接跳读源码。

---

## 0. 现状摘要

### 0.1 链路与归属

| 层 | 文件 | 关键入口 |
| -- | -- | -- |
| 入口 | `apps/web/src/app/page.tsx:14` | `<MobileStudio/> / <DesktopStudio/>` |
| 桌面 / 移动外壳 | `apps/web/src/components/ui/shell/{DesktopStudio,MobileStudio}.tsx` | 挂载 `ConversationCanvas` + `PromptComposer` |
| 画布 | `apps/web/src/components/ui/chat/desktop/DesktopConversationCanvas.tsx`、`apps/web/src/components/ui/chat/mobile/MobileConversationCanvas.tsx` | 渲染 `MessageRow` / `SceneDivider` / `DevelopingCard` |
| 消息构件 | `AssistantBubble.tsx`、`UserBubble.tsx`、`CompletionStatusLine.tsx`、`GenerationView.tsx`、`IntentBadge.tsx` | 工具状态徽章、生图卡片 |
| 状态 | `apps/web/src/store/useChatStore.ts`（4306 行 Zustand store） | `sendMessage`(L2196)、`applySSEEvent`(L3247)、`queueCompletionStreamPatch`(L1406) |
| 网络 | `apps/web/src/lib/apiClient.ts:1304` `postMessage`；`apps/web/src/lib/useSSE.ts` | EventSource + 指数退避；`apps/web/src/components/SSEProvider.tsx` 中央分发 |
| API | `apps/api/app/routes/messages.py:1504`（POST messages）；`apps/api/app/routes/conversations.py:1530`（list）/ 2104（compact）/ 2291（compact status） | 写 Message + Completion/Generation + OutboxEvent 单事务 |
| Outbox→Stream | `apps/api/app/sse_publish.py:24` `publish_event` Lua XADD | Redis Stream + per-user DLQ + PG DLQ |
| 计费/配额 | `apps/api/app/routes/messages.py:229` 预扣 `_ensure_chat_wallet_preflight`；`packages/core/lumen_core/billing.py:352` `hold`/`settle` |
| Worker | `apps/worker/app/tasks/completion.py:2514` `run_completion`；`apps/worker/app/tasks/generation.py` `run_generation` |
| 上游 | `apps/worker/app/upstream.py:94` `stream_completion`（Responses API SSE） |
| 工具 | Responses 端 4 个内建工具：`web_search` / `file_search` / `code_interpreter` / `image_generation`（`apps/worker/app/tasks/completion.py:137-143`、`319-371`、`390-426`） |

### 0.2 上游工具执行模型（关键事实）

工具不在本地 dispatcher 执行，而是由 OpenAI Responses API 服务端执行，本地仅做：

1. 在请求体加上 `tools=[{type:"web_search"},…]`、`tool_choice:"auto"`、`parallel_tool_calls:false`（`completion.py:374-379`）；
2. 在 SSE 中追踪 `response.web_search_call.*` / `response.file_search_call.*` / `response.code_interpreter_call.*` / `response.image_generation_call.*` 等事件，通过 `_CompletionToolTracker`（`completion.py:225+`、`_extract_tool_call_update` L493、`_merge_tool_call_state` L554）合并状态；
3. 通过 `EV_COMP_PROGRESS`（stage=`tool_call`）把 `tool_call`、`tool_calls` 数组推给前端（`completion.py:2802-2814`）；
4. 在 `response.completed` 时再次扫描 `resp` 中的最终态并补发（`completion.py:2961-2972`）；
5. 工具产出的图像通过 `_store_and_publish_completion_tool_image` 解码 + 存档 + `EV_COMP_IMAGE` 推送（`completion.py:2834-2861`、`2935-2960`）。

→ 因此「工具调不动」的真因不在「dispatcher 缺一环」，而在 **状态机覆盖不全、断流补发缺失、UI 在状态机上漏一格就卡住**。本方案聚焦这一点。

### 0.3 已知缺陷的来源

本方案以下三处既有报告为基线，**不重复修补已记录但未完成的项**，仅整合到统一时间线：

- `docs/audits/archive/BUG_REVIEW.md` P1-43（composer 浅拷贝）、L481（SSE 跨 tab 双投）。
- `docs/audits/archive/DEEP_BUG_AUDIT.md` P1-1（钱包检查非原子）、P1-4（SSE 缺 event ID 重连重放）、P1-13（Redis cancel 静默吞错）。
- 本机历史审计 `docs/audits/local/ISSUES.md` L67-86、L1431-1465、L1604-1735、L1989-2076（前端 store / composer / hook race）、L1147-1216（upstream SSE 不关 / 字节计数 / failover 误计）。

---

## 1. 现存问题清单（按链路分组，全部带 file:line）

### A. 入站：POST `/conversations/{id}/messages`

| # | 文件:行 | 问题 | 用户可见症状 | 优先级 |
| - | -- | -- | -- | -- |
| A1 | `apps/api/app/routes/messages.py:1600-1606` vs `1090-1106` | 余额检查与 `billing_core.hold` 不在同一事务/同一锁，并发可超额扣 | 同一用户两个并发消息：用户消息已 flush 但 hold 失败回 402，会话出现「半条消息」 | P1 |
| A2 | `apps/api/app/routes/messages.py:1397` `_lookup_idempotent_post` | 仅按 (user_id, idempotency_key) 查，未校验 conversation_id 一致；同一 key 跨会话复用会被命中 | 用户在 A 会话发消息回到 B 会话发同步消息会拿到 A 的返回 | P1 |
| A3 | `apps/api/app/routes/messages.py:1639` 用户消息 flush 在 `hold` 前 | 上面 A1 的延伸：异常路径下回滚的是事务但事务里已经写入了 Message 行 | DB 留下 orphan user message | P1 |
| A4 | `_ensure_chat_wallet_preflight`（`messages.py:229-246`） | `estimate_completion_cost` 不含工具开销（`web_search` 单次约 30¢、`code_interpreter` 容器分钟计费） | 工具一开就可能超预算导致中途 `billing_core.settle` 透支 | P1 |

### B. Outbox → Redis Stream → SSE 重放

| # | 文件:行 | 问题 | 症状 | 优先级 |
| - | -- | -- | -- | -- |
| B1 | `apps/api/app/routes/events.py:307-388`（见本机归档 `docs/audits/local/ISSUES.md` L365） | SSE replay 不按 client 请求的 channel 过滤 | A 会话页面会收到 B 会话事件，触发误更新 | P1 |
| B2 | `apps/api/app/routes/events.py:532-558`（DEEP_BUG_AUDIT.md P1-4） | publisher 未给 `sse_id` 时 SSE 不下发 `id:` 字段，浏览器重连后无 `Last-Event-ID` 锚点 | 网络抖动后中间所有无 ID 事件被重放，文本翻倍 | P1 |
| B3 | `apps/api/app/sse_publish.py:56-79` | `_LAST_TS_MS` 无锁更新，并发 worker 可产出同 `ts_ms` | 重连时事件顺序错乱 | P2 |
| B4 | `apps/api/app/sse_publish.py:85-130` | 失败时 fallback Redis-DLQ + PG-DLQ，但都失败仅 log；下游消费者不知道有事件丢失 | 极端故障下静默丢消息 | P2 |
| B5 | `apps/api/app/routes/events.py:330-332` | `get_message(timeout=1.0)` + 断开检测最长 1s | 客户端断开后最多滞留 1s 才释放，影响 SSE 资源回收 | P3 |

### C. 上游 SSE / 工具状态机（Worker）

| # | 文件:行 | 问题 | 症状 | 优先级 |
| - | -- | -- | -- | -- |
| C1 | `apps/worker/app/tasks/completion.py:1047-1075` | `_lease_renewer` 连续 3 次失败抛 RuntimeError，但主循环每 16 个 delta 才查 `lease_lost` | lease 丢失后最多再推 16 token；可能与新 worker 双写 | P1 |
| C2 | `apps/worker/app/tasks/completion.py:2793-2875` | `_CANCEL_CHECK_EVERY_DELTAS=16`；用户点取消最多迟 2-3s 才中断 | 「停止」按钮明显延迟；用户继续被扣费 | P1 |
| C3 | `apps/worker/app/tasks/completion.py:347-352`（DEEP_BUG_AUDIT P1-13） | `_is_cancelled` Redis 异常时返回 True（fail-closed 反了） | 短时 Redis 抖动会误把所有任务标为取消 | P1 |
| C4 | `apps/worker/app/tasks/completion.py:2870-2879`（ISSUES.md L1436） | retry 路径在 `set comp.text=""` 前没保留 `tool_calls`/`tool_images` 引用 | 重试时工具卡片闪烁清空 | P1 |
| C5 | `apps/worker/app/tasks/completion.py:2299-2381`（ISSUES.md L1439） | `pg_advisory_xact_lock` 在 commit 后释放，到 lease renewer 启动前的窗口可被并发 worker 抢占 | 同一 completion 可能被两个 worker 同时跑 | P1 |
| C6 | `apps/worker/app/tasks/completion.py:319-371` | `file_search` 在 `vector_store_ids` 缺失时仅 warn 后静默跳过；UI 仍显示「检索文件」开关已开 | 用户以为生效，实际从未调到 | P1 |
| C7 | `apps/worker/app/tasks/completion.py:493-550` | `_extract_tool_call_update` 对未列出的 `tool_type` 走 fallback `"tool"`；`_TOOL_RUNNING_STATUSES` / `_TOOL_FAILED_STATUSES` 为硬编码字符串 | 上游字段微调（如 `in_progress` → `running`）会让状态卡永远停在 queued | P1 |
| C8 | `apps/worker/app/tasks/completion.py:2961-2972` | `update_from_response` 仅在 `response.completed` 触发，不在 `response.failed` / `response.incomplete` 时收尾 | 失败终态下最后一个工具卡片状态可能停在 `running` | P1 |
| C9 | `apps/worker/app/tasks/completion.py:2552-2572`（ISSUES.md L1454） | 工具图像去重 set 只对带 `item.id` 的事件生效；同次 `partial_image` 不带 id 会重复存档 | 同张图被存多次，图片库被污染 | P2 |
| C10 | `apps/worker/app/upstream.py:6306-6512`（ISSUES.md L1147） | `_iter_sse_with_runtime` 在 SSE 循环内抛 `UpstreamError` 时未 `aclose` httpx response | 上游 socket 半连接堆积，弱网时雪崩 | P1 |
| C11 | `apps/worker/app/upstream.py:6440-6450`（ISSUES.md L1199） | `_SSE_MAX_LINE_BYTES` 用 `len(str)` 而非 `encode('utf-8')` 长度 | 大量 CJK 内容时上限放宽 ~2× | P2 |
| C12 | `apps/worker/app/tasks/completion.py:1083-1115` | `_attachment_to_data_url` 用 `asyncio.to_thread` 但每张图都重新加载 PIL | 多图附件 P95 延迟显著 | P2 |
| C13 | `apps/worker/app/tasks/completion.py:2905-2914` | `tokens_in/out` 默认 0：若 `response.completed` 未到（上游早断），settle 用 0 计费但已有部分文本 | 异常路径下「白送」 | P1 |
| C14 | `apps/worker/app/tasks/completion.py` 全局 | 无 `max_tool_invocations` / `max_assistant_iterations` 上限 | 模型刷工具调用可能耗光 token 不收敛 | P1 |
| C15 | `apps/worker/app/tasks/completion.py:2843-2844`（ISSUES.md L1448） | `RETRY_BACKOFF_SECONDS[attempt - 1]` 索引越界回退到固定值，未告警 | 第 N 次重试退避失真 | P2 |

### D. 前端 store / SSE 接入

| # | 文件:行 | 问题 | 症状 | 优先级 |
| - | -- | -- | -- | -- |
| D1 | `apps/web/src/store/useChatStore.ts:3771-3786`（ISSUES.md L1604） | `useChatStoreBound` 条件调用 hook + 模块级 `getChatStore()` getter | 多组件订阅可能拿到不同 store 实例；事件 handler 看到的状态 ≠ 渲染时状态 | P0 |
| D2 | `apps/web/src/store/useChatStore.ts:3517-3578`（ISSUES.md L2057） | completion_id 没有稳定反查到 assistant message；delta 早到时只能靠当前消息扫描 | 工具输出 / 文本 delta 早于 `conv.message.appended` 时可能丢失 | P1 |
| D3 | `apps/web/src/store/useChatStore.ts:1406-1440` | `queueCompletionStreamPatch` 50ms 批处理无 per-completion 隔离 | 并发会话 / 多消息流时 delta 被合到错误消息 | P1 |
| D4 | `apps/web/src/store/useChatStore.ts:3613-3638` | delta 应用按 `msgId or compId`；二级 fallback `completionMessageLookupId` 找不到时静默丢弃 | 极少数情况文本一直不显示 | P1 |
| D5 | `apps/web/src/store/useChatStore.ts:902-916`（ISSUES.md L2128） | 模块级 Map 在 SSR 进程内被跨请求复用 | SSR 路径下偶发用户态串流 | P2 |
| D6 | `apps/web/src/store/useChatStore.ts:338-345`（BUG_REVIEW P1-43） | `cloneComposerState` 浅拷贝 attachments | 失败回滚后 attachments 共享引用，状态错位 | P1 |
| D7 | `apps/web/src/lib/useSSE.ts:101-108, 150-376` | useEffect refs 同步无依赖；EventSource 在 StrictMode 双挂载时 cleanup 顺序不稳 | 偶发僵尸连接 / handler 注册到已废弃连接 | P1 |
| D8 | `apps/web/src/components/SSEProvider.tsx:292-305`（ISSUES.md L68） | 5s self-heal + onOpen 触发 hydrate + poll 不限并发 | 弱网下请求风暴；用户掉电恢复时一片白屏 | P1 |
| D9 | `apps/web/src/store/useChatStore.ts:3654-3688` | 工具图像 payload 无 schema 校验；恶意/异常字段会 throw 进而中断当前 setState | 一次脏事件让整个 store 进入异常 | P1 |
| D10 | `apps/web/src/components/ui/chat/CompletionStatusLine.tsx:47-76` | 仅识别 `failed/running/queued/succeeded`；上游若推 `cancelled` 或 `timed_out` 落入默认分支 | 卡片永远转圈 | P2 |
| D11 | `apps/web/src/components/ui/PromptComposer.tsx:202-230`（ISSUES.md L1989） | `enhancePrompt` abort 后 composer 状态未清；流仍在写到已清空的 composer | UI 偶发出现「自填入残段」 | P1 |

### E. 工具特定

| # | 文件:行 | 问题 | 症状 | 优先级 |
| - | -- | -- | -- | -- |
| E1 | `web_search` — `completion.py:324-325` | 仅塞 `{type:"web_search"}`，未挂 `user_location`、`search_context_size`，无法跨 provider 一致行为 | 同一问题不同 provider 返回差异大 | P2 |
| E2 | `file_search` — `completion.py:327-349` | `vector_store_ids` 缺失静默 skip + UI 仍显示开 | 用户感知不到该工具未启用 | P1 |
| E3 | `code_interpreter` — `completion.py:351-357` | `container:{type:"auto"}` 无容器复用 / 无 stdout 上限 | 长输出可能击穿 SSE 行字节限制（与 C11 复合） | P2 |
| E4 | `image_generation` — `completion.py:359-369` + `_store_and_publish_completion_tool_image` | size/quality 硬编码、写图前无 user 配额二次校验 | 用户余额刚好够文本但够不到图，整轮失败回滚体验差 | P1 |
| E5 | 工具计费 — `messages.py:229-246` 预估只覆盖文本 token | 工具调用产生的额外费用直到 settle 才计入 | 大额超支风险 | P1 |
| E6 | UI 工具开关 — `apps/web/src/components/ui/PromptComposer.tsx`（搜「web_search」） | 多工具同时开时无前端互斥/提示，且未把禁用条件反馈给用户（如未配置 vector_store） | 用户开了无效工具仍以为生效 | P2 |

---

## 2. 目标架构与不变式

完成本方案后，下列不变式必须在测试中可验证：

1. **单次提交 = 单条 user message + 单条 assistant message + ≤ N tool 调用**。N 默认 8（C14），可配。
2. **任何工具调用一定有终态事件**：`tool_call.completed | failed | cancelled | timed_out`，前端按 4 种状态各对应一种 UI；不存在「永远转圈」。
3. **断流可恢复**：SSE 重连依赖 `Last-Event-ID`；事件 100% 含 `sse_id`（B2）；replay 按 channel 过滤（B1）。
4. **取消 ≤ 500ms**：用户点击「停止」到 worker 中断 ≤ 500ms（C2）；并对未消费的工具调用回款。
5. **计费幂等 + 工具感知**：`hold` 预算覆盖文本 + 工具估值（A4/E5）；`settle` 失败有补偿/告警；不可双扣（DEEP_BUG_AUDIT P0-1 已在另一计划修，本计划不重复）。
6. **前端 store 唯一**：`useChatStore` 是单例；事件 handler 与渲染共享同一 store 实例（D1）。
7. **多 tab 一致**：同一会话两个 tab 看到的 delta 序列一致、不双投（B2 + 现有 BroadcastChannel）。
8. **工具未启用要告诉用户**：vector_store 未配置则 `file_search` 在 UI 灰显并 tooltip 说明（E2/E6）。
9. **错误必有出口**：所有 `error_code` 都映射到中文用户文案；失败可重试或重生成。
10. **观测**：每条 completion 落地 traceId，关联 `outbox→stream→SSE` 三段日志，便于事后回放。

---

## 3. 阶段化实施计划

实施分四阶段，每阶段独立可发布；后阶段不依赖前阶段未合并代码。

### 阶段 1 — P0 / P1 阻断项（第 1 周）

目标：把「会话明显坏掉」「白用 / 多扣」「工具静默不生效」全部修掉。

1. **D1**：`useChatStore` 改为模块级单例（移除条件 hook、`Object.defineProperties` getter 切换），SSR 路径用 `Map<requestId, store>`。验收：单元 + RTL 集成。
2. **A1 / A3 / A4**：把余额检查、`hold`、`Message.flush` 合到 `_create_assistant_task` 内的同一事务、同一 wallet 锁；`estimate_completion_cost` 增加工具系数（`web_search`/`code_interpreter`/`image_generation` 单价从 `settings.chat.tool_*_micro` 读取）。
3. **A2**：`_lookup_idempotent_post` 增加 `conversation_id` 过滤。
4. **B1 / B2**：`events.py` replay 按 channels 过滤；`publish_event` 所有路径强制写入 `sse_id`（用 Redis Stream entry ID 或自增 ULID）。
5. **C2 / C3**：把 `_CANCEL_CHECK_EVERY_DELTAS` 调到 4 且加上 100ms 软轮询任务并行；`_is_cancelled` Redis 异常时返回 `False`（fail-open，配合 lease 与终态 CAS）。
6. **C6 / E2**：`file_search` 在请求构造阶段若无 `vector_store_ids` 应抛 `EC.FILE_SEARCH_NOT_CONFIGURED`，前端把该开关置为 disabled + tooltip。
7. **C7 / C8 / D10**：用 `TOOL_STATUS_MAP`（dict）做 status 规范化，未识别状态归到 `unknown` 并在 worker 日志告警，UI 在 30s 内无更新自动降级到 `timed_out`；`response.failed/incomplete` 时强制 `tool_tracker.finalize()` 把 running → cancelled。
8. **D2 / D3 / D4**：新增 `_completionMessageIds: Map<completion_id, message_id>`；`queueCompletionStreamPatch` key 改成 `completion_id` 级隔离；缺 lookup 时把 delta 暂存 `pendingDeltasByCompletionId` 直到 message 出现再合并（10s TTL + max entries + 丢弃 + 告警）。
9. **D6 / D9**：composer 深拷贝；所有 SSE payload 用 `zod` 校验，失败 log + 丢弃单事件而不是 throw。
10. **C13**：worker 在 finalize 时若 `response.completed` 缺失则用累计 `accumulated_text` 长度 + provider 估值兜底，避免 0-token 计费。
11. **C14**：在 `run_completion` 内加 `MAX_TOOL_INVOCATIONS = settings.chat.max_tool_invocations or 8`；超过即 `tool_choice="none"` 并写 `EV_COMP_PROGRESS{stage:"tool_loop_truncated"}`。
12. **C10**：`_iter_sse_with_runtime` 改为 `async with httpx.stream(...)` 的同范围，确保 `aclose` 总会执行（用 `contextlib.aclosing` 包装）。

### 阶段 2 — P1 加固（第 2 周）

目标：消除「偶发抖动」「不可观测」。

1. **C1 / C5**：lease 丢失事件改为 `asyncio.Event`，在 SSE 循环每次 `await` 后通过 `wait_for(stream.__anext__(), …)` + `cancel_on(lease_lost)` 立即跳出；`pg_advisory_xact_lock` 改为 `pg_advisory_lock`（session 范围）并在 finally `pg_advisory_unlock`。
2. **C4**：`_pack_recent_history` 与 retry 路径都把 `tool_calls`/`tool_images` 写入 `completion.upstream_request->tool_state`，重启 worker 时载入而非清零。
3. **D7 / D8**：`useSSE.ts` 改为以 ref-counted `EventSourceSingleton`，所有 hydrate / poll 调用合并到 `coalescePromise(key, fn)`；StrictMode 双挂载下保持单连接。
4. **D11**：`PromptComposer.enhancePrompt` 在 abort 时同步置 `enhancingStreamId=null`，store 端检查 `streamId !== state.activeEnhanceStreamId` 直接丢弃 chunk。
5. **B3 / B4**：`_LAST_TS_MS` 改成 Redis `INCR` 计数器作单调 id；DLQ 失败上报 Prometheus + Sentry tag 而非纯日志。
6. **E1 / E3**：`web_search` / `code_interpreter` 暴露 `search_context_size` / `container.type` 等参数到 settings；`code_interpreter` 输出累计字节超过 256KB 时主动截断并提示用户「输出过长已截断」。
7. **E4 / E5**：发图前再 `wallet_check(estimate_image_micro)` 二次预扣，失败则把 `image_generation` 从 tools 中剥离继续文本对话，并 SSE 推 `tool_skipped:{reason:"insufficient_credits"}`。
8. **观测**：每条 SSE 事件带 `trace_id`；API/Worker 日志 join 字段为 `trace_id`；新增 `docs/chat-trace-runbook.md`。

### 阶段 3 — P2 精修 + 体验（第 3 周）

1. C9（图像去重 fallback 用 b64 sha1）、C11（按 bytes 计算 SSE 行长）、C12（PIL 缓存 by image_id）、C15（退避索引边界 + 告警）。
2. D5（模块级 Map 改 `AsyncLocalStorage` 等价物 / 显式 request scope）、E6（PromptComposer 工具开关接 `useToolAvailability()`）。
3. UI：`CompletionStatusLine` 增加「取消 / 重试此工具」入口（仅对支持的工具：image_generation / code_interpreter 直接 reattempt）。
4. `ConversationCanvas` 在网络断开 > 5s 显示「连接已断开，正在重连」横幅，重连成功后自动 hydrate。

### 阶段 4 — 验证与上线（第 4 周）

1. 离线回放：抓取 5 类既有客诉的 SSE 录像（公司内 staging），用 `tools/sse-replay/` 重放确认全部修复。
2. 灰度：先对 5% 用户打开 `settings.chat.hardened_loop=true`（行为差异在阶段 1 的工具循环 + 取消窗口），观察 1d。
3. 全量：根据 `MEMORY.md` 「Lumen Release Workflow」走 `python3 scripts/version.py sync` + 版本标签发布。

---

## 4. 详细设计要点

### 4.1 工具状态机（C7/C8/D10）

新建 `packages/core/lumen_core/chat_tools.py`：

```python
class ToolStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"

UPSTREAM_STATUS_MAP: dict[str, ToolStatus] = {
    "in_progress": ToolStatus.RUNNING,
    "running": ToolStatus.RUNNING,
    "searching": ToolStatus.RUNNING,
    "interpreting": ToolStatus.RUNNING,
    "completed": ToolStatus.SUCCEEDED,
    "failed": ToolStatus.FAILED,
    "incomplete": ToolStatus.FAILED,
    "cancelled": ToolStatus.CANCELLED,
    "canceled": ToolStatus.CANCELLED,
}
```

Worker 端用 `normalize_tool_status(raw) -> ToolStatus`；前端 `CompletionStatusLine` 完全按枚举渲染。每个 tool_call 记录 `last_update_ts`，若 30s 无更新且未到终态，worker 主动补发 `timed_out` 终态（避免 UI 卡死）。

### 4.2 取消链路（C2/C3）

```
用户点击停止
  → POST /tasks/:id/cancel
  → API SETEX task:{id}:cancel 1 EX 3600
  → Worker:
       (a) SSE 主循环：把 cancel 监听换成 `asyncio.create_task(_watch_cancel(redis, task_id))` 持续轮询 200ms
       (b) cancel 触发后立刻 abort httpx response（用 AbortController via response.aclose()）
       (c) 终态写 status=cancelled，billing_core.release(hold_id)（如还在 hold 阶段）/ settle(actual_tokens, refund=True)
  → 前端 store 收到 EV_COMP_FAILED{code:"cancelled"} 渲染「已取消」+ 重试按钮
```

`_is_cancelled` 改为 fail-open（Redis 异常返回 `False`），让上层 lease 与终态 CAS 兜底，避免 Redis 抖动误杀。

### 4.3 工具计费预算（A4/E4/E5）

`estimate_completion_cost(model, messages, *, tools)` 增加：

```python
extra = 0
if "web_search" in tools:
    extra += settings.chat.web_search_cost_micro
if "code_interpreter" in tools:
    extra += settings.chat.code_interpreter_cost_micro * estimated_minutes
if "image_generation" in tools:
    extra += settings.chat.image_tool_cost_micro * max_images
```

`hold` 含此预算；`settle` 时按上游真实 `usage.tool_calls`（如有）扣回未用部分；如无字段，按已发生事件数兜底。

### 4.4 SSE 事件 ID 与重放（B1/B2）

- `publish_event` 始终通过 Lua 脚本拿到 Redis Stream entry id 写入 `sse_id`；
- HTTP SSE 响应在 `event:` 前总是带 `id:<entry_id>`；
- `Last-Event-ID` 给到 `XREAD STREAMS <user_stream> <entry_id>`；
- replay 输出前对每条 event 二次校验 `event.channel in requested_channels`，否则丢弃；
- 新增 `events.replay_dropped_total` 指标以便回归监控。

### 4.5 前端 store 单例（D1/D5）

- 把 `useChatStoreBound` 删掉，直接 `export const useChatStore = create<ChatState>(…)`；
- SSR 入口在 Next 的 `next.config.ts` 已使用 RSC；store 文件加 `"use client"` 强制只在客户端实例化；
- 模块级 Map（`_completionStreamTimer` 等）改为 `useRef` 或 store 内字段。

### 4.6 弱网恢复（D8 + 阶段 3 横幅）

```
SSEProvider 内：
  visibility => visible & 上次 hydrate > 30s 前 => hydrateActiveTasks()
  online 事件 => 同上
  open 事件 => 触发 dedup-coalesced refresh：同一 key 600ms 内只跑一次
```

`ConversationCanvas`：当 `lastEventAt > 5s` 且 `streaming === true` 时渲染顶部细横幅「正在重连…」。

### 4.7 可观测性

- `trace_id`：API 入口生成 ULID，写入 `Message.metadata->trace_id`、`Completion.metadata->trace_id`、`OutboxEvent.payload.trace_id`、SSE event。
- 新指标：`chat_tool_invocations_total{tool, status}`、`chat_completion_cancel_latency_ms`、`sse_replay_dropped_total`、`completion_lease_lost_total`、`tool_status_unknown_total`。
- 日志统一加 `extra={"trace_id": ..., "completion_id": ..., "user_id": ...}`，在 `apps/api/app/logging.py` 与 worker 同步。

---

## 5. 数据库 / 配置变更

- 不新增表。
- `Completion.upstream_request` JSONB 新增字段 `tool_state`（用于阶段 2 C4 重启恢复）。
- `system_settings` 新增 key（默认值括号内）：
  - `chat.max_tool_invocations`（8）
  - `chat.web_search_cost_micro`（按上游成本配置）
  - `chat.code_interpreter_cost_micro_per_minute`（按上游成本配置）
  - `chat.image_tool_cost_micro_per_image`
  - `chat.tool_status_idle_timeout_s`（30）
  - `chat.cancel_poll_interval_ms`（200）
  - `chat.hardened_loop`（false → true after rollout）

---

## 6. 测试方案

### 6.1 单元

| 用例 | 文件 |
| -- | -- |
| `normalize_tool_status` 覆盖所有上游已知字符串 + 未知降级 | `tests/core/test_chat_tools.py` |
| `estimate_completion_cost` 工具系数 | `tests/core/test_pricing.py` |
| `_is_cancelled` Redis 异常 fail-open | `tests/worker/test_completion_cancel.py` |
| `markEventSeen` 跨 tab 不重投 | `apps/web/__tests__/SSEProvider.test.tsx` |
| `queueCompletionStreamPatch` 按 completion_id 隔离 | `apps/web/__tests__/useChatStore.delta.test.ts` |

### 6.2 集成（pytest + 实 Redis + 实 PG）

| 用例 | 期望 |
| -- | -- |
| 余额刚好够一次，并发两次 send | 一次 402，另一次成功；DB 无 orphan user message |
| 文本流到一半 worker 被 kill | arq 重启，attempt 自增，`tool_state` 恢复，最终 settle 正确 |
| 用户点取消后 1s 内 | worker 已终止 stream，余额已 refund，前端显示「已取消」 |
| 多 tab 同会话 | delta 不双投，文本一致 |
| `file_search` 未配 vector_store | API 返回 400 `FILE_SEARCH_NOT_CONFIGURED`，前端开关 disabled |
| `code_interpreter` 输出 1MB | SSE 不超 line 限制，UI 显示「输出已截断」 |
| 网络断 10s 再恢复 | SSE 续传，无文本翻倍 |
| 工具死循环（mock 上游永远返回 tool_call） | 第 8 次后 `tool_loop_truncated`，文本/失败终态写入 |

### 6.3 端到端（Playwright）

放到 `apps/web/scripts/e2e/chat-*.spec.ts`：

- 发文本 → 收 delta → 完成；
- 发文本带 `web_search` → 状态卡 4 态切换；
- 发文本带 `image_generation` → 图像出现在对话中并可点开；
- 中途断网 5s → 重连后无重复 → 完成；
- 点停止 → 1s 内出现「已取消」；
- 两个 tab 同会话 → 内容一致。

### 6.4 回归

`apps/web` 提交前必须通过：`npm run type-check && npm run lint && npm run build`（参见 `MEMORY.md` UI 标准）。

后端：`uv run pytest tests/api tests/worker tests/core -q`。

---

## 7. 风险与回滚

| 风险 | 缓解 |
| -- | -- |
| `useChatStore` 单例改动可能引起 SSR hydration 不一致 | 加 `"use client"` + 独立 PR + e2e 全量 |
| 取消窗口从 16-delta 缩到 200ms 轮询会增加 Redis QPS | 仅在 `status in {streaming, queued}` 时轮询；配置可调 |
| `tool_loop_truncated` 改变交付内容 | 在 UI 显式提示「为防止失控已限制工具次数」并允许「继续」按钮触发新一轮 |
| 计费预算调整 | 上线前先 dry-run：计算「老规则 vs 新规则」差额，>5% 用户的工具支出差异 < 10% 才发布 |
| Outbox `sse_id` 全量改造引入新依赖 | 兼容旧客户端：缺 `id:` 回退到「最近 5s replay」窗口 |

回滚：所有改动通过 `chat.hardened_loop` 配置开关包裹；关闭即回到旧行为，无需代码回退。

---

## 8. 验收清单

发布前在 staging 必须勾完：

- [ ] 单条用户消息 → 单条 assistant 终态，DB 无 dangling row。
- [ ] 四个内建工具任意组合均可见状态卡 4 态切换，从不卡 `running`。
- [ ] 点击停止：最大延迟 500ms，refund 入账。
- [ ] 断网 10s 重连：文本无重复，无丢失。
- [ ] 两个 tab：delta 一致，无双投。
- [ ] 余额刚好 / 余额不足 / Redis 闪断 / PG 闪断：分别走对应错误码并可恢复。
- [ ] 全部 P0/P1 项在 `docs/audits/archive/BUG_REVIEW.md`、`docs/audits/archive/DEEP_BUG_AUDIT.md` 和本机 `docs/audits/local/ISSUES.md` 中已复核。
- [ ] `npm run build` 与 `uv run pytest` 通过。

---

## 9. 实施时间表

| 周 | 阶段 | 产出 |
| -- | -- | -- |
| 1 | 阶段 1：P0/P1 阻断项 | 12 项修复 PR + 单元/集成测试 |
| 2 | 阶段 2：P1 加固 | 8 项修复 PR + Playwright e2e |
| 3 | 阶段 3：P2 精修 + 体验 | UX 增强 + observability |
| 4 | 阶段 4：灰度 + 上线 | 5% → 100%，按 release workflow 发版 |

---

## 10. 附录：文件 / 行号速查

```
入口          apps/web/src/app/page.tsx:14
壳            apps/web/src/components/ui/shell/{DesktopStudio,MobileStudio}.tsx
画布          apps/web/src/components/ui/chat/desktop/DesktopConversationCanvas.tsx
              apps/web/src/components/ui/chat/mobile/MobileConversationCanvas.tsx
工具卡        apps/web/src/components/ui/chat/CompletionStatusLine.tsx:47
生图卡        apps/web/src/components/ui/chat/GenerationView.tsx
Composer      apps/web/src/components/ui/PromptComposer.tsx:202
Store         apps/web/src/store/useChatStore.ts:2196 / 3247 / 3771
SSE           apps/web/src/lib/useSSE.ts
              apps/web/src/components/SSEProvider.tsx:249
HTTP API      apps/web/src/lib/apiClient.ts:1304
后端入口      apps/api/app/routes/messages.py:1504 / 229 / 1397
后端列表      apps/api/app/routes/conversations.py:1530 / 2104 / 2291
SSE 发布      apps/api/app/sse_publish.py:24
SSE 重放      apps/api/app/routes/events.py:307
Worker        apps/worker/app/tasks/completion.py:2514 / 137 / 319 / 493 / 554 / 2802 / 2961
上游          apps/worker/app/upstream.py:94 / 6306 / 6440
计费          packages/core/lumen_core/billing.py:352 / 424
事件常量      packages/core/lumen_core/constants.py:249
模型          packages/core/lumen_core/models.py:399 / 435 / 607 / 741 / 1049
```
