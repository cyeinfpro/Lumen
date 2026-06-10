# AI 视频功能深度 Bug 审计（2026-06-10）

审查范围（全量通读）：

| 层 | 文件 |
|---|---|
| API | `apps/api/app/routes/videos.py`（2106 行）、`apps/api/app/video_reference_images.py`、`apps/api/app/routes/providers.py`（video 部分）、`apps/api/app/routes/events.py`（video 通道） |
| Worker | `apps/worker/app/tasks/video_generation.py`、`apps/worker/app/video_billing.py`、`apps/worker/app/video_upstream.py`、`apps/worker/app/tasks/outbox.py` |
| Core | `packages/core/lumen_core/video_billing.py`、`video_providers.py`、`billing.py`、`schemas.py`（Video*）、`models.py`（VideoGeneration/Video）、`runtime_settings.py`（video spec） |
| Web | `apps/web/src/app/video/page.tsx`（7094 行）、`apps/web/src/lib/apiClient.ts`（video 部分）、`apps/web/src/app/admin/_panels/VideoProvidersPanel.tsx` |

分级：**P0 资金/计费正确性**、**P1 功能正确性/资源安全**、**P2 体验/一致性/低危**。

计费基线原则（既定约束）：**平台绝不吸收上游成本——只要上游扣费，用户就必须付**。下面多条 P0 直接对照该原则。

---

## P0 — 资金 / 计费

### P0-1 deadline 过期：不向上游发取消，却全额退款用户
- 位置：`apps/worker/app/tasks/video_generation.py:791-801`（expired PollResult 构造）、`apps/worker/app/video_billing.py:147-155`（`terminal_not_billable_release`）
- `run_video_poll` 中只有 `cancel_requested_at` 非空才调用 `_try_provider_cancel`（L788）。本地 deadline（10 分钟）过期时直接构造 `PollResult(status="expired", upstream_billable=None)`，**完全不通知上游取消**，随后 billing 走 `upstream_billable is not True → release`，把 hold 全额退回用户。
- 上游（火山/第三方网关）任务并不会因为平台超时而停止，照常渲染完成并向平台收费。结果：**上游扣费、用户退款、平台吸收成本**，且视频结果也永久丢失（任务已 terminal，不再 poll）。
- 慢任务（4k / 高峰期 >10 分钟）每一单都触发。这是对计费原则最直接的违反。
- 建议：expired 时先对支持取消的 provider 调 `adapter.cancel`；对不支持取消的（dashscope/omni）**不应 release**，应延长轮询或按 `est_cost` 扣费；至少把 expired+release 改成挂起人工对账。

### P0-2 成功但缺 usage/billable 信号 → 免费送视频（官方火山适配器独缺兜底）
- 位置：`apps/worker/app/video_billing.py:129-137`（`missing_usage_release`）、`apps/worker/app/video_upstream.py:689-697`
- 决策矩阵：`succeeded + usage=None + billable=None → release`。用户**拿到了视频但一分钱不付**。
- 四个适配器里，`VolcanoThirdPartySeedanceAdapter`（L794-796）、`UnifiedVideoCreateAdapter`（L1072-1074）、`DashScopeHappyHorseAdapter`（L1214）在 succeeded 时都默认 `upstream_billable=True`，**唯独官方 `VolcanoSeedanceAdapter.poll` 没有这个兜底**——一旦官方响应缺 `usage`（网关裁剪、字段改名、`_usage_total_tokens` 解析失败），立即触发免单。
- 注意：`tests/test_video_billing.py::test_resolve_video_billing_releases_success_when_usage_missing` 把该行为固化成了预期。该测试与计费原则相悖，需要一并修订。
- 建议：成功必收费——`succeeded` 且 `billable is not False` 时至少按 `max(held, est_cost_micro)` 结算；官方适配器补 `upstream_billable=True if status=="succeeded" else _billable(raw)`。

### P0-3 失败 + 有 usage + billable=None → 退款（usage 即上游已扣费的强信号）
- 位置：`apps/worker/app/video_billing.py:147`（在 `usage_tokens is not None` 分支 L156 **之前**短路）
- 上游失败响应中带了 `usage`（明确报了消耗）但没带 billable 字段时，先命中 `upstream_billable is not True → release`，usage 被无视。上游大概率已按 usage 扣费 → 平台吸收。
- `test_resolve_video_billing_does_not_charge_failed_usage_without_billable` 同样固化了此行为，需产品层面重新确认。
- 建议：把 `usage_tokens is not None` 分支提到 `upstream_billable is not True` 之前（有用量就按用量结算）。

### P0-4 submit 超时重试可在上游创建重复任务（上游双倍扣费）
- 位置：`apps/worker/app/tasks/video_generation.py:119-128`（timeout/transport 可重试）、`:531-558`（submit 无上游幂等键）、`apps/worker/app/video_upstream.py`（所有 adapter 的 submit body 均不带客户端去重 ID）
- `adapter.submit` 请求发出后响应丢失（读超时、连接断），平台视为可重试错误并再次 submit。第一个请求可能已被上游接受 → **上游存在两个计费任务**，平台只跟踪第二个的 task id，用户只付一份 → 平台吸收第一单成本。
- `_SUBMIT_RESULT_CACHE`（L181-192）只在**成功拿到响应后**写入，防不住"请求已达、响应丢失"。
- 放大器 1：`reconcile_video_tasks` L1364 把 stale 的 SUBMITTING 行重置回 QUEUED 并重新入队，lease（120s）过期后第二个 worker 可并行再 submit。
- 放大器 2：`uq_video_gen_provider_task` 唯一索引（`models.py:1069-1077`）挡不住"同一 generation 两次 submit 得到两个不同 provider_task_id"。
- 建议：seedance/网关支持透传 metadata 时带上 `task_id` 做上游侧去重；不支持时，超时后先 poll 按 `safety_identifier`/列表查询对账，确认无任务再重试。

### P0-5 retry 端点无状态校验 + 前端"重新生成"按钮裸奔 → 一键多倍扣费
- 位置：`apps/api/app/routes/videos.py:1758-1845`、`apps/web/src/app/video/page.tsx:7024-7031`
- 后端 `POST /generations/{id}/retry` **不检查原任务是否 terminal**：进行中的任务也能 retry，且 `idempotency_key=f"retry:{row.id}:{new_uuid7()}"` 每次全新 → 重复请求不去重。
- 前端 `TaskRow` 对**活跃任务也渲染"重新生成"按钮**（只有"取消"按 active 条件渲染），并且按钮没有用 `retryMut.isPending` 禁用——**连点 N 次 = N 个并行任务、N 份 hold、N 份扣费**。
- 另外 retry 路径绕过了 create 端点的 `account_mode == "wallet"` 检查（`videos.py:1570-1573` 只在 create 有，`_create_video_generation_record` 没有）：byok 用户若有历史 video 记录可绕过 wallet-only 限制。
- 建议：后端限制 retry 仅对 `failed/canceled/expired` 开放并补 account_mode 检查；前端按钮按 terminal 状态渲染并加 `isPending` 禁用。

### P0-6 settle 金额无上限——usage 解析错误会放大成巨额扣费
- 位置：`packages/core/lumen_core/video_billing.py:363-389`（`settle_video_cost` 无 cap）、`apps/worker/app/video_upstream.py:385-416`（`_duration_usage_total_tokens` 顶层 `("duration",)` 路径过宽）
- `_duration_usage_total_tokens` 会把响应顶层任意 `duration` 字段×1,000,000 当 tokens。若网关把 duration 以毫秒返回（10000），settle = 10000×1M tokens ≈ est 的 1000 倍。`billing.settle` 中 `allow_negative` 开启时直接扣成深度负数；关闭时 cap 到 0、超出部分平台吸收（`billing.py:523-528`）。
- 建议：settle 金额对 `est_cost_micro` 设倍数上限（如 3×），超限记审计并按 est 收费；`duration` 解析约束到 `usage` 命名空间内。

---

## P1 — 功能正确性 / 资源安全

### P1-1 deadline 过期且无 provider_task_id 的任务永不终结（hold 资金可被无限期占用）
- 位置：`apps/worker/app/tasks/video_generation.py:446-461`（run_video_generation 无 deadline 检查）、`:1358-1366`（reconcile 对无 task id 的行只会重置 QUEUED 再入队）
- 流程闭环里没有任何人对"deadline 已过 + 未提交上游"的任务做终结：reconcile 每 30s 重置 QUEUED → enqueue submit → worker 不查 deadline 照常提交。后果一：worker 宕机几小时后恢复，用户 10 分钟前的任务在数小时后突然提交上游执行。后果二：若 submit 持久化持续失败（如撞 `uq_video_gen_provider_task` 唯一索引——幂等网关对相同输入返回相同 task id 时必现），任务无限循环、**hold 资金永久占用**。
- 建议：`run_video_generation` 开头与 reconcile 中对 `deadline_at <= now` 且无 provider_task_id 的任务直接 fail + release。

### P1-2 取video 结果失败跨过 deadline → 视频丢失 + 走 P0-1 退款路径
- 位置：`apps/worker/app/tasks/video_generation.py:1005-1053`（`_finish_success`）、`:791-801`
- `fetch_result` / `_store_video_asset`（磁盘满、网络抖动）失败会走 poll 重试；一旦重试拖过 deadline，下一轮直接判 expired（不再询问上游）→ release。上游已成功且已扣费，视频再也取不回来。
- 建议：对"上游已 succeeded 仅取回失败"的任务单独标记，超 deadline 后仍允许有限次取回，且 billing 按成功处理。

### P1-3 `fetch_result` 无大小上限、无内容校验，整段进内存
- 位置：`apps/worker/app/video_upstream.py:699-716`、`:1218-1235`
- `response.content` 一次性读入，无 max-bytes（对照 `_fetch_image_url_as_data_url` 有 64MB 限制）。被劫持/恶意的第三方网关返回超大响应可直接 OOM worker。也不校验 content-type/magic bytes，任意字节都会被存成 `.mp4` 并向用户收费。
- 关联：`UnifiedVideoCreateAdapter.poll` L1063-1064 `status=="running" 且有 video_url 即判 succeeded`，而 `_video_url` 匹配路径极宽（含顶层 `url`、`metadata.url`）——网关响应若回显某个无关 URL，会把非视频文件当结果落盘并扣费。
- 建议：流式下载 + 上限（如 2GB）；校验 content-type 或 ftyp 魔数；omni 的提前 succeeded 判定限定在明确的结果字段。

### P1-4 `ensure_video_reference_image_variant` 的 `db.rollback()` 污染外层请求事务
- 位置：`apps/api/app/video_reference_images.py:276-291`
- 并发创建同一 variant 撞 IntegrityError 时执行**整 session rollback**（而不是 `begin_nested`），调用链位于 create-generation 的请求事务中。多参考图场景下，前面参考图刚生成的 `video_reference_access_token` 赋值被回滚丢失，而 URL（含该 token）已写入 snapshot → 上游访问 404 → 任务必败（hold 会释放，但用户体验是莫名失败）。
- 建议：仿照 `billing._insert_tx` 用 `begin_nested` 包住 flush。

### P1-5 第三方 Seedance 网关 cancel 路径与 submit/poll 不一致（疑似笔误）
- 位置：`apps/worker/app/video_upstream.py:800-808`
- submit/poll 用 `v1/video/generations[/...]`，cancel 却 DELETE `v1/videos/{id}` → 大概率 404 → `CancelResult(accepted=False)`，**用户取消永远不生效**，上游照常渲染并扣费（随后按 poll 终态收用户钱，原则上没亏钱，但取消功能形同虚设）。
- 建议：对齐为 `video/generations/{id}`，或按网关文档修正。

### P1-6 poll 收到 404 立即终态失败 + 退款（新任务读写延迟即被误杀）
- 位置：`apps/worker/app/video_upstream.py:1305-1327`（404→`invalid_input`）、`apps/worker/app/tasks/video_generation.py:76-83`（`invalid_input` 不在可重试列表）
- 第三方网关 submit 后查询接口短暂 404（读写分离/最终一致）很常见。当前 404 → `invalid_input` → 不可重试 → 立刻 FAILED；billing 因 `billable=None` 走 release。而上游任务实际在跑、最终扣费 → 平台吸收（又一条 P0-1 家族路径）。
- 建议：poll 阶段对 404 至少容忍 N 次/N 秒再判终态。

### P1-7 前端：删除视频后该任务被永久判为"活跃" → 无限 2.5s 轮询 + SSE 订阅
- 位置：`apps/web/src/app/video/page.tsx:924-934`（`isActiveVideo`: `succeeded && !video → true`）、`:2866-2879`（deleteMut 置 `video=null`）、`:1659-1677`（active 轮询 effect）、`:1479-1482`（SSE channels）
- 用户每删除一个成功视频的资产，对应 generation 变成 `succeeded + video=null` → 进入 `activeItems` → **每 2.5 秒一次 GET 详情、永不停止**（刷新页面后 history 拉回同样数据继续轮询），同时占用一个 SSE task 通道。删 N 个就是 N 路永久轮询，对服务端是稳定的自我 DDoS。
- 建议：`isActiveVideo` 对 `succeeded` 不应回 true（可单独渲染"结果已删除"态）；或后端 `_generation_out` 对已删资产返回明确标记。

### P1-8 上传参考视频：无配额、无内容校验、无去重
- 位置：`apps/api/app/routes/videos.py:482-540`
- 64MB×无限次数，全量进内存（bytearray），不校验 magic bytes（content_type 由客户端任填 mp4/mov 即过），同 sha 重复落盘。普通用户可无成本撑爆磁盘；任意文件可借平台公开 URL（`/api/videos/reference/{id}/binary?token=...`，无需登录）对上游或第三方分发。
- 建议：按用户配额（个数/总字节）限制；校验 ftyp 魔数；按 sha256 去重；考虑给 reference URL 加过期时间。

### P1-9 reference URL 中的 access token 长期有效且通过 API 回显
- 位置：`apps/api/app/routes/videos.py:195-236`（token 生成无过期）、`:355-366`（`_generation_reference_media` 把含 token 的 URL 原样返回给前端）
- 给上游用的免认证 URL（图片/视频参考）带的 token 永不过期，且会随 generation 详情回显。用户复制分享链接 = 永久公开该资产。前端 `loadAsDraft` 还会把这种 URL 当 `url` 引用重新提交。
- 建议：token 加 TTL（任务生命周期内有效即可）或在任务 terminal 后轮换。

### P1-10 通用设置端点保存 `video.providers` 时校验器不识别共享 proxy（与运行时不对称）
- 位置：`packages/core/lumen_core/runtime_settings.py:1166-1167`（`validate_video_providers(raw)` 不传 `shared_provider_raw`）、对照 `apps/api/app/routes/providers.py:644-650`（专用端点传了）
- 运行时解析（API/worker）都带共享配置，引用共享 proxy 合法；但经由通用 settings 校验路径保存时报 "proxy not found" 被拒。配置导入/导出、脚本化修改会踩坑。
- 建议：校验器签名支持可选 shared raw，或校验时仅警告未知 proxy。

---

## P2 — 体验 / 一致性 / 低危

| # | 位置 | 问题 |
|---|---|---|
| P2-1 | `video_upstream.py:136-158` | `_status` 对未知状态一律映射 `running`：上游新增终态（如 `moderating`/`rejected`）会拖满 deadline 再走 expired-release（连带 P0-1）。 |
| P2-2 | `tasks/video_generation.py:1321-1324` | `_looks_faststart` 用 `bytes.find(b"moov")` 全文件搜索，mdat 数据流中出现 `moov` 字节序列会误判已 faststart、跳过转码（首播体验差）。 |
| P2-3 | `routes/videos.py:1588-1595` | `_decode_cursor` 接受任意 isoformat：传入 naive datetime 与 timestamptz 列比较时 asyncpg 抛错 → 500（应 422）。 |
| P2-4 | `routes/videos.py:824-851` | 火山 `reference` 动作要求 `reference_image` 与 `reference_video` 两种 variant **都**配价才显示该模式；只配图片参考价时整个参考生成不可见，管理员难以排查。 |
| P2-5 | `lumen_core/video_billing.py:93-106` | `split_video_resolution_pricing_variant` 比较用小写但返回原值：管理员把 variant 配成 `t2v_1080P` 时 options 价格匹配不上（前端按 `1080p` 对比）。 |
| P2-6 | `tasks/video_generation.py:1358-1362` | reconcile 中 `if row.deadline_at <= now and row.provider_task_id` 与 `elif row.provider_task_id` 两个分支完全相同（死代码）。 |
| P2-7 | `page.tsx:986-991` | `parseSeed` 只验证整数，不验证 [-1, 4294967295] 范围 → 后端 422，且 DashScope 上限更小（2147483647）在前端无提示。 |
| P2-8 | `page.tsx:1120` | `estimateHoldMicro` 找不到价格时返回 `micro: 0` → UI 显示"预计 ¥0"，实际后端按真实价格 hold，误导用户。 |
| P2-9 | `page.tsx:783-799` | `waitForStoryboardCompletion/ImageTask` 轮询循环（最长 108s/210s）无 AbortSignal，组件卸载后继续打 API 并 setState。 |
| P2-10 | `page.tsx:181` | `SETTLING_VIDEO_STAGES` 含后端不存在的 `storing`/`billing` stage（前后端 stage 枚举漂移，死代码）。 |
| P2-11 | `routes/videos.py:1990` | `_media_response` 的 If-None-Match 只做单值精确比较，不支持 `*`、多 etag、弱比较（W/）。 |
| P2-12 | `routes/videos.py:415` | `diagnostics` 原样全量回显给用户，含上游错误消息、cancel 原始响应等内部细节（轻微信息暴露）。 |
| P2-13 | `video_upstream.py:556-561` | 官方火山 i2v 强制 base64 data URL（不允许公网 URL），大图（数十 MB）会生成超大 JSON body，易被上游网关拒绝；无前置大小预算。 |
| P2-14 | `routes/videos.py:1338-1347` | omni_flash 的 reference 视频引用在 API 层拒绝 ✓，但 t2v/i2v 的 `aspect_ratio=21:9` 等未按 provider 白名单预校验，submit 阶段才报错（hold→release 一轮空转）。 |
| P2-15 | `page.tsx:1885-1931` | 参考素材数量上限（9 图/3 视频）在 mutationFn 闭包里读旧 state，快速连续上传可超限（后端 schema 会兜底 422）。 |
| P2-16 | `routes/videos.py` cancel + `video_upstream.py`（dashscope/omni `cancel→None`） | 用户取消后 toast"已请求取消"，但 dashscope/omni 不支持上游取消，任务照常成功并**全额扣费**（符合转嫁原则，但 UI 完全没有"取消可能失败仍计费"的预期管理，易引发计费争议）。 |
| P2-17 | `lumen_core/billing.py:516-517` | settle 中 `held<=0 and raw_actual<=0: return None` 不落账——零额结算无审计痕迹（对账时少一条 ledger）。 |
| P2-18 | `routes/videos.py:854-1014` | `/options` 把全部 `hold_estimates` 原样返回（含未上架模型的估算表），轻微配置信息暴露。 |

---

## 已验证无问题（重点排查过）

- **钱包核心**：`hold/settle/release` 双重幂等（idempotency_key + ref 消费检查）+ 行锁，settle/release 互斥正确（`billing.py:425-585`）。
- **arq 入队去重**：API fast-path 与 outbox 补漏用同一 `arq_job_id(kind, task_id, outbox_id)`，不会双跑（`routes/videos.py:1522-1533` vs `tasks/outbox.py:204-208`）。
- **取消竞态**：API cancel 与 worker submit 用行锁 + `skip_locked` 协调，pre-submit cancel 与已提交推进两条路径自洽。
- **媒体服务**：`_fs_path` 防穿越、O_NOFOLLOW 防符号链接、Range/ETag 处理正确；reference binary 用 `secrets.compare_digest`。
- **SSE 通道授权**：`events.py` 对 video task 通道做了所有权校验。
- **会话配置**：`expire_on_commit=False`，poll 中 session 关闭后访问 ORM 属性安全。
- **幂等创建**：create 的 IntegrityError 回退 + `request_fingerprint` 防 key 重用，正确。
- **事件名**：前端 `VIDEO_EVENTS` 与 `constants.py` 的 `EV_VIDEO_*` 完全一致。

---

## 修复优先级建议

1. **P0-1 / P0-2 / P0-3（计费决策矩阵）**：一次性重写 `resolve_video_billing` 的 release 条件——只有上游**明确** `billable=False` 或确证未提交上游时才退款；其余终态按 usage 或 `max(held, est)` 收费。同步修订两条已固化错误行为的测试。
2. **P0-5（retry 裸奔）**：后端状态校验 + 前端按钮条件渲染/防抖，半天内可完成。
3. **P1-7（删除视频后无限轮询）**：一行条件修复。
4. **P0-4 / P1-1 / P1-2（submit 幂等与 deadline 终结）**：需要设计上游对账逻辑，工作量最大。
5. 其余 P1/P2 按表逐项处理。
