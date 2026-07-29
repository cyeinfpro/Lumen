# Lumen 架构、模块拆分与工程治理深度审计

- Status: audit completed; remediation not started
- Date: 2026-07-29
- Repository: `cyeinfpro/Lumen`
- Baseline commit: `702d445dbb5e3c8cd6c6a41e2f5b8207684d4690`
- Baseline tag: `v1.2.82`

Follow-up plan:
[`lumen-engineering-governance-9of10-plan-2026-07-29.md`](./lumen-engineering-governance-9of10-plan-2026-07-29.md)

## 1. 审计目的

本审计评估当前 Lumen 在以下方面的真实完成度：

- 模块拆分是否降低了业务耦合，而不是仅移动文件。
- Python、Web、Worker、image-job 的依赖方向是否合理。
- 运行时状态、资源和生命周期是否有明确所有者。
- CI、发布、升级和治理基线是否不可绕过。
- 是否存在可确认的业务缺陷、资金风险、数据隔离问题和死代码。
- 后续拆分应按什么顺序推进，以及每个阶段如何验收。

本次使用 6 个并行审计方向：

1. 架构、模块边界与工程治理。
2. API 和 Core。
3. Worker 和 image-job。
4. Web、跨标签实时状态与客户端数据隔离。
5. CI、发布、升级和回滚。
6. 死代码、重复实现和未使用依赖。

审计为只读检查。除本报告外，没有修改产品代码、测试、配置或发布状态。

## 2. 总体结论

当前工程已经具备较完整的静态门禁、影响测试规划、发布工作流和拆分账本，
但治理结果仍不能被视为完成。

总体成熟度评估：**5/10**。

| 维度 | 评价 | 结论 |
| --- | --- | --- |
| 静态依赖图 | 8/10 | Python/Web 显式循环和已登记边界违规为零 |
| CI 与测试基础设施 | 7/10 | 覆盖较广，但生产构建输入、迁移和治理文件存在绕过路径 |
| 大文件治理 | 5/10 | 建立了 1,500 行硬阈值，但大量文件贴线，职责仍然混杂 |
| 模块语义拆分 | 5/10 | 物理拆分明显，部分模块仍通过 facade、ContextVar 和回调重新耦合 |
| Runtime ownership | 3/10 | 扫描器漏报 Core 和多类全局状态，生命周期责任不完整 |
| 资金链路可靠性 | 3/10 | image-job 接单后失败路径存在重复生成和错误退款风险 |
| Web 数据隔离 | 4/10 | 跨标签恢复、会话撤销和账号切换仍可能保留旧私有状态 |
| 死代码治理 | 6/10 | 已能识别高可信候选，但缺少生产可达性门禁和清理闭环 |
| 发布与回滚 | 5/10 | 发布链较完整，但 tag 来源、alias 推广和 updater 提交点不够安全 |

核心判断：

- 当前状态是“已有治理系统”，不是“治理已经闭环”。
- 静态门禁为绿，不能证明运行时边界、资金语义和客户端隔离正确。
- 多处拆分降低了文件级循环，但没有同步降低动态依赖和状态所有权复杂度。
- 在修复 P0/P1 问题前，不应继续以大规模机械拆文件作为第一优先级。

## 3. 当前基线证据

审计期间确认：

- `main` 与 `origin/main` 均指向 `702d445`。
- 工作树在审计前保持干净。
- Python architecture gate：0 cycles，0 explicit boundary violations。
- Python architecture audit：3 个已登记 runtime-coupling findings。
- Complexity gate：0 hard violations，5 个多维复杂度 findings。
- Module runtime-state gate：报告 8 个实例，但本审计确认该数字存在漏报。
- Manifest lint：报告 1,252 个生产文件、0 unmatched，但 image-job 根路径统计错误。
- Web architecture：559 files，2,139 edges，3 features，0 cross-feature edges。
- Web complexity：0 findings。
- Web type-check：通过。
- Web tests：510 项通过。
- 治理相关定向测试：44 项通过。
- Worker/Core 资金路径定向测试：5 项通过。
- 当前 main CI run `30411381239` 成功。
- 当前 main Docker Release run `30411381238` 成功。

这些全绿证据与本报告中的真实缺陷同时存在，说明当前测试和门禁存在覆盖盲区。

## 4. P0：必须优先处理的资金风险

### P0-01 image-job 已接单后轮询超时仍会重新生成并退款

位置：

- `apps/worker/app/upstream_parts/image_jobs.py:559`
- `apps/worker/app/upstream_parts/image_jobs.py:608`
- `apps/worker/app/upstream_parts/image_job_failover.py:62`
- `apps/worker/app/retry.py:223`
- `packages/core/lumen_core/upstream_billing.py:223`

触发链：

1. Worker POST sidecar 成功并取得 `job_id`。
2. sidecar 任务保持 queued/running，Worker 轮询达到 deadline。
3. Worker 抛出普通 `upstream_timeout`。
4. 该错误被判定为可重试，并允许 endpoint/provider failover。
5. 新请求可能再次提交生成。
6. 最终普通失败计费路径将该错误判定为可 release。

主审探针结果：

```text
upstream_timeout:
  retriable = true
  failover = true
  billing = release
```

影响：

- 原 sidecar 任务可能稍后成功并产生费用。
- 系统同时提交第二次生成。
- 用户可能被退款，而平台承担第一笔或多笔上游成本。
- 当前没有持久化 sidecar execution handle，崩溃恢复也无法继续轮询原任务。

要求：

- POST 接单后 timeout 必须映射为成本未知终态。
- 接单后只允许恢复轮询，不允许重新 POST。
- 立即持久化 `job_id/provider/endpoint/idempotency_key`。
- 增加“接单 -> 轮询超时 -> 原任务稍后成功”的计费和重启测试。

### P0-02 image-job 已成功但结果下载失败仍会 failover 和退款

位置：

- `apps/worker/app/upstream_parts/image_jobs.py:499`
- `apps/worker/app/upstream_parts/image_jobs.py:526`
- `apps/worker/app/upstream_parts/image_jobs.py:547`
- `apps/worker/app/upstream_parts/direct_requests.py:100`
- `apps/worker/app/upstream_parts/image_job_failover.py:86`

触发链：

1. sidecar GET 返回 `status=succeeded`。
2. Worker 读取 `images[0].url`。
3. 下载结果发生 HTTP/network/OSError。
4. 错误映射为 `direct_image_request_failed`。
5. 该错误允许重新生成和 provider failover。
6. 最终失败计费路径仍会 release。

主审探针结果：

```text
direct_image_request_failed:
  retriable = true
  failover = true
  billing = release
```

影响：

- 上游成本已经确定发生。
- 系统仍可能再次生成。
- 用户可能获得退款，平台承担已经发生的成本。

要求：

- “生成失败”和“交付失败”必须使用不同错误类型。
- sidecar 成功后只能重试下载，禁止重跑生成。
- 持久化结果 URL、图片元数据和成本可知性。
- 增加 succeeded 后下载超时、404、空响应和重启恢复测试。

## 5. P1：高优先级确认缺陷

### P1-01 image-job 取消接口不存在

位置：

- `apps/worker/app/upstream_clients/image_job_client.py:201`
- `image-job/image_job/app_factory.py:76`
- `image-job/image_job/persistence.py:659`

Worker 会发送 `DELETE /v1/image-jobs/{job_id}`，但 sidecar 只注册 POST 和 GET。
客户端也不校验 DELETE 的响应状态。

影响：

- 用户取消后本地任务可能退款。
- sidecar 和上游任务继续执行。
- 产生孤儿任务、孤儿文件和平台成本。

整改要求：

- 实现认证 DELETE。
- 返回 `cancelled_before_dispatch`、`cancel_requested`、`already_terminal`
  等明确结果。
- Worker 必须根据取消结果决定 release、settle 或进入 uncertain。

### P1-02 retention 会清理 queued/running 任务

位置：

- `image-job/image_job/persistence.py:467`
- `image-job/image_job/persistence.py:1327`
- `image-job/image_job/persistence.py:1348`
- `image-job/image_job/application/job_service.py:402`

任务创建时即设置 `retention_expires_at`，清理查询没有限制
`finished_at/status`，因此可能清除活跃任务凭证和 artifacts。

影响：

- queued 任务失去凭证后无法被 reconciler 重新调度。
- running 任务的输入或输出可能在处理中被删除。
- 数据库中保留无法恢复、无法删除的永久卡死行。

整改要求：

- 普通 retention 仅处理终态任务。
- active stale policy 独立实现，并使用状态 CAS。
- 清理前先将异常活跃任务转换为 failed/uncertain。

### P1-03 sidecar `n>1` 只交付第一张图片

位置：

- `apps/worker/app/upstream_parts/image_jobs.py:681`
- `apps/worker/app/upstream_parts/image_jobs.py:526`
- `image-job/image_job/artifacts.py:153`
- `apps/worker/app/tasks/generation_parts/runner_dispatch_phase.py:344`

请求允许 `n=2..10`，sidecar 会生成和保存全部图片，但 Worker 固定读取
`images[0]`。现有多图附加结果逻辑只覆盖 direct source。

影响：

- 其余图片产生上游成本和磁盘占用。
- 用户看不到这些图片。
- 结算数量与真实生成数量可能不一致。

短期应强制 sidecar `n=1`；长期应统一 direct 和 sidecar 的多图交付管线。

### P1-04 Canvas 视频节点调用契约已经失效

位置：

- `apps/api/app/services/task_submission.py:204`
- `apps/api/app/services/task_submission.py:214`
- `apps/api/app/services/video/submission.py:396`
- `apps/api/tests/test_canvas_execution_guards.py:1043`

Canvas 适配器仍传入：

- `request`
- `workflow_metadata`
- `defer_commit`
- `deferred_publish_payload`

新的 `create_video_generation_record()` 只接受 `context` 和 `services`。
主审签名绑定确认：

```text
TypeError: got an unexpected keyword argument 'request'
```

现有测试使用 `**kwargs` mock，反而掩盖了真实契约漂移。

整改要求：

- Canvas 适配器构造 `VideoSubmissionContext`。
- 使用真实函数签名或 autospec mock。
- 增加 Canvas `video_generate` 完整 service-level 测试。

### P1-05 跨标签 SSE 恢复错误标记 follower 已恢复

位置：

- `apps/web/src/features/realtime/model/runtime.ts:516`
- `apps/web/src/features/realtime/model/runtime.ts:525`
- `apps/web/src/features/realtime/model/runtime.test.ts:444`

只有 leader 执行 snapshot adapter。follower 没有刷新自己的 Zustand/React Query，
但收到 `recovery_complete` 后直接进入 `open`。

现有测试明确断言：

- leader snapshot 被调用一次。
- follower snapshot 调用次数为零。
- follower 状态仍然变为 open。

影响：

- replay gap 期间丢失的任务、余额、会话和资源状态会长期残留。
- UI 显示连接正常，但本地状态并未恢复。

整改要求：

- 每个 tab 必须执行本地 snapshot。
- leader 只负责连接和恢复协调，不应替代 follower 的本地状态重建。

### P1-06 `auth_invalidated` 没有真正撤销客户端会话

位置：

- `apps/web/src/features/realtime/model/runtime.ts:353`
- `apps/web/src/features/realtime/model/runtime.ts:516`
- `apps/web/src/features/realtime/model/useLumenRealtime.ts:327`
- `apps/web/src/components/RuntimeResilienceStatus.tsx:12`

leader 只把连接状态映射为 realtime `error`，没有将 session 状态设置为
`unauthorized`。follower 仅收到 control event，但当前订阅未提供有效的会话撤销处理。

影响：

- UI 继续展示旧私有数据。
- 提示是“实时连接中断”，而不是“会话已失效”。
- 角色变化、登录撤销和 cookie 失效不能及时清理客户端状态。

### P1-07 视频任务本地状态没有用户作用域

位置：

- `apps/web/src/app/video/use-video-generation-feed.ts:81`
- `apps/web/src/app/video/use-video-generation-feed.ts:96`
- `apps/web/src/app/video/use-video-generation-feed.ts:199`
- `apps/web/src/components/useIdentityRevalidation.ts:221`

账号切换时 React Query 会清理旧缓存，但以下状态不会按 user id 重建：

- `items`
- `selectedVideoId`
- refresh request map
- refresh timer
- task SSE channels

影响：

- 旧账号视频任务仍可能显示。
- 新账号页面继续订阅或轮询旧账号 task id。
- 构成客户端跨账号私有数据泄露和错误请求。

### P1-08 PR 可绕过生产构建输入测试

位置：

- `.github/workflows/ci.yml:118`
- `scripts/test-manifest.toml:4`

以下文件可产生 `commands=0`、`full_mandatory=false` 和 unmatched 结果：

- `Dockerfile.python`
- 根目录 `docker-compose*.yml`
- `.dockerignore`
- `.env.example`
- `apps/web/Dockerfile`

CI 的 empty-plan 拒绝逻辑允许存在 unmatched evidence，因此这些生产构建输入可以在
PR 中不运行 Docker build 或 compose config。

### P1-09 Alembic breaking lint 和发布 tag 来源可绕过

位置：

- `.github/workflows/alembic-expand.yml:3`
- `.github/workflows/docker-release.yml:7`
- `.github/workflows/docker-release.yml:115`

问题：

- Alembic breaking lint 只在 pull request 触发。
- main 直推和 tag 发布不执行同等 migration lint。
- 任意 `v*` tag 都能触发正式发布。
- workflow 只校验 tag commit 等于 checkout HEAD，不要求其是 `origin/main` 的祖先。

影响：

- 破坏性 migration 可通过直推或直接打 tag 进入发布。
- 未合并、未经过 main required checks 的 commit 可成为正式 Release。

### P1-10 updater 在最终健康检查前提交状态

位置：

- `scripts/update/services/restart.sh:283`
- `scripts/update/runner.sh:101`
- `scripts/update/recovery/state.sh:240`

`restart_services` 成功后立即执行 `mark_update_committed`，但最终 HTTP health check
在下一阶段才运行。健康检查失败后，committed 状态会阻止自动恢复并进入
`manual_required`。

影响：

- API/Web 已不可用时，current 仍指向新 release。
- 即使本次没有 migration，也不会自动恢复旧版本。

提交点应移动到最终 health check 成功之后，或至少引入
`switch_committed` 与 `health_verified` 两阶段状态。

### P1-11 首个新 major 的 stable alias 推广会失败

位置：

- `scripts/promote_release_images.py:430`
- `.github/workflows/docker-release.yml:777`
- `.github/workflows/docker-release.yml:847`

新 major 的 `v2`、`v2.0` 等 alias 在第一次发布前没有旧 digest。当前实现要求所有
mutable alias 必须已经存在 rollback baseline，因此会在写入前拒绝推广。

同时 workflow 先创建 GitHub Release，再执行 stable alias promotion，可能形成：

- GitHub Release 已存在。
- 精确版本镜像已存在。
- `latest/v2/v2.0` 仍指向旧版本或不存在。

## 6. P2：架构和工程治理问题

### P2-01 Billing 拆分形成隐藏运行时循环

位置：

- `apps/api/app/routes/billing.py:181`
- `apps/api/app/routes/billing_parts/compat.py:9`
- `apps/api/app/routes/billing_parts/overview.py:59`

当前依赖链为：

```text
billing facade
  -> billing_parts
  -> ContextVar runtime provider
  -> _BillingRuntime.__getattr__
  -> billing facade globals
```

静态依赖图看不到该循环，但资金域仍通过 `Any` 和 `globals()` 访问旧模块私有实现。

目标结构应为：

```text
BillingRoutes
  -> BillingQueries / BillingCommands
  -> BillingRepository / WalletService / PricingService
```

依赖由 application runtime 或 FastAPI dependency 显式构造。

### P2-02 runtime-state scanner 严重漏报

位置：

- `scripts/module_runtime_state_audit.py:27`
- `packages/core/lumen_core/providers.py:834`
- `packages/core/lumen_core/volcano_asset_media.py:79`
- `packages/core/lumen_core/context_window.py:226`

扫描根没有覆盖 Core 和 TgBot，也无法识别多种普通顶层可变实例。

确认漏报：

- `_SSH_TUNNEL_RUNTIME`：子进程字典和 `asyncio.Lock`。
- `_VIDEO_TRANSCODE_RUNTIME`：事件循环到 Semaphore 的弱引用表。
- `_TOKEN_COUNTER_RUNTIME`：线程、锁和 tokenizer cache。
- `_ADAPTER_RUNTIME_PORT`：可变工厂。
- `_BILLING_RUNTIME`：动态 service locator。

当前“8 个实例”只能表示 ledger 已登记实例数，不能表示实际 runtime state 数量。

### P2-03 治理基线可在同一 PR 中自我扩容

涉及：

- architecture baseline
- complexity baseline
- runtime coupling inventory
- module runtime-state ledger
- facade retirement ledger
- Web architecture/complexity baseline

当前缺少 merge-base 单调性验证。代码新增违规后，同一 PR 更新 baseline/ledger，
修改后的 gate 仍可能通过。

要求：

- CI 从 merge-base 读取旧基线。
- 集合类基线只允许删除，不允许新增。
- 数值类预算只允许下降。
- 治理文件变更强制 full suite 和 CODEOWNERS。

### P2-04 facade retirement 指标覆盖不完整

`docs/refactors/compatibility-facade-retirement.json` 只登记：

- `packages/core/lumen_core/models.py`
- `packages/core/lumen_core/schemas.py`

但源码仍存在 billing、video upstream、poster、admin 等兼容 surface。
“facade 只剩 2 个”实际是“ledger 只登记 2 个”。

需要自动发现：

- re-export facade
- `compat.py`
- service locator
- 动态 `__getattr__`
- 同时承担业务逻辑的兼容入口

### P2-05 1,500 行门禁形成贴线行为

当前生产代码统计：

- 78 个文件不少于 1,000 行。
- 46 个文件不少于 1,200 行。
- 26 个文件不少于 1,400 行。
- 18 个文件不少于 1,450 行。
- 4 个文件不少于 1,490 行。

典型文件：

- `apps/web/src/app/video/page.tsx`：1,499 行。
- `apps/api/app/routes/storyboards.py`：1,499 行。
- `apps/api/app/images/adapters/filesystem_store.py`：1,499 行。
- `apps/api/app/routes/volcano_assets.py`：1,490 行。
- `apps/web/src/components/ui/canvas/CanvasInspector.tsx`：1,411 行。
- `apps/web/src/components/ui/canvas/CanvasViewport.tsx`：1,331 行。

Shell 文件：

- `scripts/install.sh`：2,503 行。
- `scripts/lumenctl.sh`：2,426 行。
- `scripts/lib.sh`：1,956 行。

但 `scripts/check_complexity.py` 的 shell line gate 只扫描 `scripts/update.sh` 和
`scripts/update/**`。

建议改为每文件 ratchet，并设置角色目标：

- route/controller：500-800 行。
- service/adapter：800-1,000 行。
- shell entrypoint：300-500 行。
- React page：仅保留 composition 和 view wiring。

### P2-06 Web architecture gate 未覆盖主要产品域

位置：

- `apps/web/scripts/check-architecture.mjs:318`
- `apps/web/scripts/check-architecture.mjs:365`

当前只把 `features/*` 识别为 feature，只识别 `store/*` 和
`features/*/store` 的状态所有权。

以下主要产品域没有进入真实 domain 边界检查：

- `app/video`
- `app/admin`
- `components/ui/canvas`
- `lib/canvas`
- `shared/realtime`

因此“3 features、0 cross-feature edges”不能代表 Web 主要业务域已经解耦。

### P2-07 Web complexity gate 可被 ESLint disable 绕过

位置：

- `apps/web/scripts/check-complexity.mjs:144`

该 gate 只消费 ESLint complexity 结果，没有禁止文件级 disable，也没有组件、
hook、state、effect 或文件长度预算。

结果是 1,499 行页面和多个千行 controller 仍显示 0 violations。

### P2-08 image-job production coverage 统计路径错误

位置：

- `scripts/test_manifest_lint.py:37`
- `scripts/test_manifest_lint.py:41`

当前模式为：

```text
image-job/app/**/*.py
```

实际生产根为：

```text
image-job/image_job/**/*.py
```

因此 35 个 image-job Python 文件没有进入 production coverage 和 critical-gap
统计。宽泛的测试规则目前仍会触发测试，但 linter 报告的生产文件总数和零缺口结论
不准确。

### P2-09 API SSE 批量回退会生成重复事件

位置：

- `apps/api/app/sse_publish.py:229`
- `apps/api/app/sse_publish.py:278`
- `apps/api/app/sse_publish.py:333`

`transaction=False` pipeline 可能部分执行成功。如果后续命令返回 reservation 或
兼容错误，代码会把整个批次退回逐条发布。

批量准备阶段生成的 event id 没有写回原事件，逐条回退会重新生成 UUID。
已经成功写入的事件无法被 dedupe，形成永久重复 durable event。

### P2-10 每个 SSE 连接都会持久化同一 compaction event

位置：

- `apps/api/app/realtime/connection_hub.py:346`
- `apps/api/app/realtime/replay.py:89`

同一用户打开多个 SSE 连接时，每个连接收到 legacy compaction PubSub 后都会执行
一次 `XADD`。消息没有 event id 时，每个连接还会生成不同 UUID。

影响：

- 重连时重复 replay。
- 多连接用户成倍消耗 stream 容量。
- 其他事件更早被淘汰。

### P2-11 `--rerun-failed` 可产生假成功

位置：

- `scripts/run_test_plan.py:155`
- `scripts/run_test_plan.py:267`

results 没有绑定当前 plan hash、base、head 或 command set。
如果 plan 已变化且旧 results 中没有失败 command，新加入但从未执行的命令会被跳过，
程序输出 `No failed command groups to rerun.` 并返回 0。

### P2-12 Release alias 解析只读取前 100 条 Release

位置：

- `scripts/release_manifest_guard.py:216`

GitHub API 请求固定使用 `releases?per_page=100`，没有分页。
当前仓库已经超过 100 个 GitHub Releases，旧 major/minor channel 可能无法解析。

### P2-13 image-job 终态写入缺少 fencing

位置：

- `image-job/image_job/persistence.py:529`
- `image-job/image_job/persistence.py:549`
- `image-job/image_job/persistence.py:599`

`mark_running` 使用 `status='queued'` CAS，但 `mark_succeeded/mark_failed` 只按
`job_id` 更新，也不检查影响行数。

旧 worker 可能覆盖：

- cancelled 状态。
- uncertain 状态。
- 恢复流程写入的新 attempt 状态。

应引入 execution token/attempt，并对所有状态转换执行 allowed-state CAS。

## 7. P3 和较低优先级问题

### P3-01 全部非法 SSE channel 会先返回 200 再断流

位置：

- `apps/api/app/realtime/channel_policy.py:51`
- `apps/api/app/realtime/channel_policy.py:199`
- `apps/api/app/routes/events.py:213`
- `apps/api/app/realtime/connection_hub.py:493`

`channels=bogus` 会被静默过滤为空列表，但不会回退到 user channel，最终调用无参数
Redis `SUBSCRIBE`。HTTP 200 已经发送，客户端只能看到流异常中断。

### P3-02 Canvas 节点上传竞态会泄漏服务端资源

位置：

- `apps/web/src/components/ui/canvas/nodes/CanvasImageAssetDropZone.tsx:86`
- `apps/web/src/components/ui/canvas/CanvasInspector.tsx:503`

DropZone 上传完成后如果节点或配置已经变化，会直接 return，不清理刚创建的图片。
Inspector 的同类流程已经实现 stale asset cleanup，说明两个入口没有共享相同的资源
所有权协议。

### P3-03 部分治理测试只检查源码字符串

涉及：

- `tests/test_lumenctl_scripts.py`
- `tests/test_update_state_machine.py`

注释、未使用常量或死代码也可能满足字符串断言，导致执行路径已经失效但测试仍然通过。

### P3-04 文档执行账本没有闭环

`docs/refactors/lumen-deep-optimization-execution-2026-07-28.md` 仍保留
`[ ] GitHub Actions passed`，同时其他段落记录相关波次完成。

治理文档应区分：

- code complete
- locally verified
- CI verified
- released
- production verified

指标应尽量由脚本生成，不应依赖手工同步。

## 8. 死代码和重复实现清单

### 8.1 确认可删除候选

| 路径 | 原因 | 风险 |
| --- | --- | --- |
| `apps/api/app/routes/_video_reference_media.py` | 仅测试导入，生产已使用 `services/video/reference_snapshots.py` | 低 |
| `apps/api/app/routes/_poster_library.py` | 仅测试导入，生产路由直接导入正式 service | 低 |
| `apps/api/app/services/upload_pipeline.py` | 无生产入口，上传已迁移到 `UploadCommandService` | 中 |
| `apps/api/app/images/adapters/variant_locks.py` | 无生产或测试导入 | 低至中 |
| `apps/api/app/workflows/adapters/model_library_tagging.py` | 无生产或测试导入，已有正式实现 | 低 |
| `apps/api/app/workflows/domain/planning.py` | 无生产或测试导入 | 低 |
| `apps/worker/app/tasks/completion_parts/decisions.py` | 只被状态机原型测试引用 | 中 |
| `apps/worker/app/tasks/generation_parts/decisions.py` | 只被状态机原型测试引用 | 中 |
| `apps/worker/app/tasks/video_generation_parts/decisions.py` | 只被状态机原型测试引用 | 中 |
| `image-job/image_job/api/__init__.py` | 不可达空包 | 低 |
| `image-job/image_job/ports/artifacts.py` | `ArtifactStore` 无引用 | 低 |
| `apps/web/src/app/(chat)/_hooks/useCompactConversation.ts` | 单行旧路径 re-export，无生产调用 | 低 |
| `packages/core/lumen_core/alembic_expand.py` | 无迁移、测试或生产调用 | 低 |

### 8.2 未使用依赖

- `apps/web/package.json` 中的 `gsap`。
- `apps/web/package.json` 中的 `@gsap/react`。
- TgBot 直接声明的 `pydantic` 可能仅为 `pydantic-settings` 的传递依赖。

删除依赖后必须重新生成 lockfile，并运行完整 Web/TgBot 测试。

### 8.3 不能直接删除的兼容 facade

- `packages/core/lumen_core/models.py`
- `packages/core/lumen_core/schemas.py`

两者仍被大量生产模块导入。应先迁移内部调用者，再删除 facade。

### 8.4 需要生产运行时证据后才能删除

- image-job legacy Bearer auth。
- `UPSTREAM_BASE_URL/UPSTREAM_API_KEY` provider fallback。
- image-job plaintext credential migration compatibility。

删除前需要线上指标、配置和数据库迁移标记证明旧路径已经不再使用。

## 9. 建议的目标架构

### 9.1 image-job

引入持久化 `SidecarExecution` 聚合：

```text
SidecarExecution
  - generation_id
  - sidecar_job_id
  - provider_id
  - endpoint
  - idempotency_key
  - dispatch_state
  - result_state
  - cost_knowledge
  - result_artifacts
```

拆分为：

```text
SidecarSubmitter
SidecarPoller
SidecarCanceller
SidecarResultDownloader
SidecarRecoveryService
ImageBillingDecision
```

关键规则：接单前可以 failover，接单后只能恢复和交付，不能重新生成。

### 9.2 Billing

删除 `ContextVar[Any]` 和 `globals()` service locator：

```text
BillingRoutes
  -> BillingQueries
  -> BillingCommands
  -> WalletRepository
  -> LedgerRepository
  -> PricingPolicy
  -> BillingCache
```

由 FastAPI application runtime 显式构造和关闭。

### 9.3 Realtime

分离：

```text
RealtimeTransport
CrossTabLeaderElection
ReplayCoordinator
LocalSnapshotRecovery
SessionInvalidation
DomainEventDelivery
```

每个 tab 对自己的 store/query 状态负责。leader 只能共享 transport 和 cursor，
不能替 follower 声明本地恢复完成。

### 9.4 Video Web

`app/video/page.tsx` 只保留页面 composition。状态按 user id 作用域化：

```text
useVideoIdentityScope
useVideoHistory
useVideoActiveTasks
useVideoRealtimeChannels
useVideoSelection
useVideoSubmission
useVideoPlayback
```

账号变化时统一 abort、清空 timers、清空 local items、重建 query keys 和 channels。

### 9.5 Storyboard 和 Filesystem Store

`storyboards.py` 建议拆为：

```text
storyboard_routes.py
storyboard_queries.py
storyboard_commands.py
storyboard_submission.py
storyboard_assembly.py
storyboard_publish.py
```

`filesystem_store.py` 建议拆为：

```text
filesystem_objects.py
filesystem_staging.py
filesystem_publish.py
filesystem_variants.py
filesystem_reconcile.py
filesystem_retention.py
```

## 10. 整改波次

### Wave 0：资金风险止血

范围：

- P0-01、P0-02。
- sidecar execution handle 持久化。
- 接单后禁止重新 POST。
- 成功后的下载失败只重试下载。

验收：

- POST accepted + poll timeout 不产生第二个 POST。
- succeeded + download timeout 不产生第二次生成。
- 两种场景都不能 release。
- Worker 崩溃后恢复轮询原 `job_id`。

### Wave 1：取消、retention、Canvas 和用户隔离

范围：

- sidecar DELETE。
- active retention policy。
- Canvas 视频签名修复。
- Web follower 本地恢复。
- `auth_invalidated`。
- 视频状态 user scope。

验收：

- queued/running/succeeded 取消竞态测试。
- retention 不读取或清理活跃任务。
- Canvas video service-level 测试不使用宽松 `**kwargs` mock。
- 两标签 recovery 两边都执行本地 snapshot。
- 切换账号后旧任务、timer、channel 和 selection 全部清空。

### Wave 2：CI 与发布治理

范围：

- Docker/Compose/env/build 文件加入 full mandatory。
- Alembic lint 进入 main 和 Docker Release quality gate。
- release tag 必须来自 main。
- updater 提交点移动到最终 health 后。
- first-major alias rollback 语义。
- Release API 分页。

验收：

- 每个生产构建输入都能生成非空 plan。
- 破坏性 migration 在 PR、main、tag 三条路径都失败。
- 非 main 祖先 tag 无法发布。
- health failure 在无 migration 场景自动恢复旧 release。
- 首个新 major stable aliases 可原子推广或完整回滚。

### Wave 3：治理指标可信化

范围：

- runtime scanner 覆盖 Core/TgBot。
- baseline merge-base 单调性。
- facade 自动发现。
- image-job manifest 根修正。
- Web domain 边界。
- complexity per-file ratchet。

验收：

- 所有顶层可变 runtime 要么登记，要么失败。
- baseline/ledger 新增债务会在 CI 失败。
- `app/video`、`app/admin`、Canvas 和 realtime 被纳入 Web domain graph。
- shell 和前端 controller 进入文件级预算。

### Wave 4：语义拆分

顺序：

1. Billing。
2. image-job execution lifecycle。
3. `storyboards.py`。
4. `filesystem_store.py`。
5. `video/page.tsx`。
6. `CanvasInspector.tsx` 和 `CanvasViewport.tsx`。
7. install/lumenctl/lib shell 入口。

每次拆分必须证明：

- 依赖方向更清晰。
- runtime owner 更少。
- facade API 更薄。
- 文件减少不是通过新增同等规模聚合模块实现。

### Wave 5：死代码和 facade 退休

范围：

- 删除高可信不可达模块。
- 迁移仍有价值的旧测试。
- 删除未使用依赖。
- 批量迁移 `lumen_core.models/schemas` 调用者。
- 线上指标确认后移除 legacy compatibility。

## 11. 最终验收门禁

后续整改完成后至少运行：

```bash
uv run ruff check .
uv run python scripts/check_architecture.py
uv run python scripts/architecture_audit.py
uv run python scripts/check_complexity.py
uv run python scripts/module_runtime_state_audit.py
uv run python scripts/test_manifest_lint.py
uv run python scripts/lint_alembic_breaking.py
```

```bash
uv run pytest -q
```

```bash
cd apps/web
npm ci
npm test
npm run lint
npm run type-check
npm run check:architecture
npm run check:complexity
npm run build
```

```bash
docker compose --env-file .env.example config
docker build -f Dockerfile.python --target api-runtime .
docker build -f apps/web/Dockerfile apps/web
bash -n scripts/*.sh scripts/update/*.sh scripts/update/*/*.sh
```

资金和恢复路径必须增加以下故障注入测试：

- sidecar POST 成功后 Worker 被终止。
- poll timeout 后 sidecar 稍后成功。
- sidecar succeeded 后结果下载失败。
- DELETE 返回 404、405、409、500 和 network timeout。
- retention 与 queued/running worker 并发。
- updater 服务启动成功但最终 HTTP health 失败。
- SSE pipeline 部分执行成功后失败。
- 两个 tab 同时发生 replay gap 和账号切换。

## 12. 完成定义

不能以以下条件单独宣称治理完成：

- architecture gate 为绿。
- complexity gate 为绿。
- 文件均少于 1,500 行。
- ledger 数字下降。
- main CI 成功。

完整完成定义是：

1. P0/P1 缺陷全部有回归测试并修复。
2. 接单后生成、交付、计费和恢复语义一致。
3. 每个进程级 runtime 都有明确 owner 和关闭路径。
4. Web 私有状态严格按用户和 tab 生命周期隔离。
5. CI 对产品代码、构建输入、迁移、治理基线和发布配置均不可绕过。
6. 大文件按业务职责拆分，而不是贴着行数阈值移动。
7. facade、compatibility 和 dead code 均有可验证的退休条件。
8. 本地测试、GitHub CI、tag release 和稳定更新通道使用同一提交证据。
