# Lumen 深度 Bug 审计报告

- 日期: 2026-07-26
- 分支: `main`（含 workflow 路由重构与 worker 域端口重构未提交改动）
- 范围: 全仓源码 —— `apps/api`、`apps/worker`、`apps/web`、`apps/tgbot`、`packages/core`、`image-job`、`scripts`、`tools`、`deploy`

> 本文档包含**两轮审计**。
> - **第二轮（本轮）**：重构语义验证 + 模块拆分与耦合评估 + 工程治理有效性 + 持续 bug 深挖 → 见下方「第二轮审计」。
> - **第一轮**：全量 bug 清单 126 项 → 见后文「第一轮审计」，其修复状态已在本轮复核。

## 修复后复核（2026-07-26）

本报告记录的是修复前的审计快照。对全部未提交修改再次交叉复核后，新增确认的阻断项已全部修复，当前未发现仍可复现的 P0/P1 缺陷：

| 复核项 | 修复结果 |
|--------|----------|
| 奖励图成功但独立计费失败后无人补偿 | 新增幂等 `BonusBillingReconciler`，扫描成功奖励图并补齐结算 |
| 仅领取、未实际请求上游的超时任务被扣费 | 持久化上游派发/响应回执；无响应回执的超时任务统一释放 hold |
| 上游成功但图片交付失败仍向用户扣费 | 未交付图片不结算，释放 hold 并记录人工复核元数据 |
| `settle()` 可突破 hold、把钱包扣成负数 | hold 改为授权上限，超额写入 `unauthorized_micro`，不再超授权扣款 |
| 超时终态写入时吞掉计费异常 | 计费先行且异常回滚，不再留下“任务已终态、资金未处理”的半提交状态 |
| Redis 锁续期失败后持锁任务继续运行 | 续期结果非真即取消 holder，阻止失锁后的并发执行 |
| 空金额被显示为 `0.00` | 空白和非法金额统一显示 `--`，并新增前端单测 |
| 确认弹窗关闭重开后误触发防重复保护 | 弹窗关闭时清空确认时间戳，保留同一次打开期间的双击保护 |

同时完成背景移除 provider 异常后继续回退、严格 `xfail`、测试模块缓存清理，以及默认前端生产构建切换到已验证的 `next build --webpack`。

验证结果：Core 555、Worker 1498、API 1392、image-job 118、tgbot 83、Web 461 个测试通过；Web 类型检查、lint、生产构建、Ruff、compileall、架构 gate、复杂度 gate、运行时状态 gate、shell 语法检查通过。当前环境没有 Docker，因此未执行 `docker compose config`。

---

# 第二轮审计（本轮新增）

- 方法: 9 路并行 agent 深挖 + lead 独立交叉验证
- 新增确认问题: **46 个**（P0×3 · P1×15 · P2×17 · P3×11）
- 关键动作: 复核第一轮 23 个 P0 的实际修复情况

## ⚠️ 本轮最重要的发现：治理体系名义存在、实际未生效

三条独立证据指向同一个系统性问题——**治理 gate 写得很好，但 CI 从不运行它们**：

| 证据 | 位置 | 后果 |
|------|------|------|
| CI 只跑 `scripts/test.sh`，从不调用 `check_architecture.py` / `check_complexity.py` | `.github/workflows/ci.yml:69-70` | 架构分层违规、复杂度超标可畅通合入 main |
| `image-job` 完全不在任何扫描路径列表中 | `scripts/check_complexity.py:19-23` | 该服务 981/909 行文件不受任何行数约束 |
| baseline 是只增不减的棘轮，`--update-baseline` 可一键豁免 | `scripts/complexity-baseline.json` | 已豁免最高 29 参数的函数，无人审查豁免膨胀 |

**这解释了第一轮审计结论为何未能落地**：本轮逐条代码复核第一轮的 23 个 P0，结果是——

| 状态 | 数量 | 说明 |
|------|------|------|
| ✅ 已修复 | 2 | F-2（取整方向）、F-3（负成本拦截） |
| 🟡 部分修复 | 2 | F-1（actual 仍可超 held）、F-6（hash 正确但比较用 `==`） |
| ⚠️ 仍存在 | **19** | **几乎全部资金对称性问题原样保留** |

**P0 修复率 = 2/23 = 8.7%**。修复率低不是执行力问题，而是**没有任何自动化机制阻止问题存续或复发**。因此本轮的首要建议不是"再修一批 bug"，而是**先让 CI 真正执行治理 gate**——否则第二轮的 46 个问题会以同样的方式沉积。

## 本轮新增 P0

### 新-1. [P0] `settle_video_cost` 3 倍封顶，平台主动吸收上游超额成本 【双轮独立确认】
- 位置: `packages/core/lumen_core/video_billing.py:427-432`
- 触发: 用户提交视频任务，预估 hold 100 元；Seedance 实际返回 4× tokens（应收 400 元）。因 `actual_micro > estimate * 3`，函数 `return estimate`，只收 100 元。
- 影响: **直接违反「视频生成计费纯转嫁——平台绝不吸收上游成本」的核心业务规则**，每单亏损 300 元，且静默发生、无告警、无差额登记。
- 证据:
  ```python
  if estimated_micro is not None:
      estimate = int(estimated_micro)
      if estimate > 0 and actual_micro > estimate * max(1, int(max_estimate_multiplier)):
          return estimate   # ← 静默返回预估额，超出部分永久丢失
  return actual_micro
  ```
- 修复: 删除封顶逻辑直接返回 `actual_micro`；若担心上游异常值，改为 `raise VideoBillingError` 交由上层决策（登记差额 + 告警运营），**绝不静默吸收**。
- 备注: D 区与 H 区两路 agent 独立命中同一处，可信度最高。

### 新-2. [P0] API images 层存在 6 包循环依赖，且治理 gate 未捕获
- 位置: `apps/api/app/images/application/http_routes.py:77` → `routes/_image_delivery`
- 触发: application 层反向 import routes 层，形成跨 6 个包的引用环。
- 影响: 分层被击穿，application 无法独立测试；`check_architecture.py` 报告"0 循环依赖"却漏掉此环，说明**检测口径与真实依赖图存在偏差**（该脚本本身也未在 CI 运行）。
- 修复: 将 `_image_delivery` 中被复用的逻辑下沉到 application/domain 层，routes 仅作 HTTP 适配；同步修正 `check_architecture.py` 的包解析口径使其能捕获该环。

### 新-3. [P0] `formatRmb` 把非法金额显示为 `"0.00"`（第一轮 C-P0，复核确认未修复）
- 位置: `apps/web/src/lib/money.ts:1-5` + `src/app/me/wallet/useWalletPageModel.ts:189`
- 影响: 后端返回 `null`/`NaN` 时，用户看到"余额 0.00"而非错误态，无法区分真实零余额与数据异常，直接损害计费透明度。
- 修复: 非法输入返回 `"--"`/`"N/A"` 或抛错由调用方处理，钱包页显式渲染错误态。

## 本轮新增 P1

### 新-4. [P1] CI 未执行架构与复杂度治理脚本
- 位置: `.github/workflows/ci.yml:69-70`
- 证据: backend job 仅 `run: bash scripts/test.sh`，两个 gate 脚本均不在任何 workflow 中。
- 影响: 所有架构治理均为"手动运行才生效"，PR 无法被阻断（新-2 的循环依赖即由此进入主干）。
- 修复: backend job 增加 `python3 scripts/check_architecture.py` 与 `python3 scripts/check_complexity.py` 两步，并设为 required check。

### 新-5. [P1] `image-job` 游离于全部治理扫描之外
- 位置: `scripts/check_complexity.py:19-23`（`DEFAULT_PATHS` / `DEFAULT_LINE_PATHS` 均缺 image-job）；`check_architecture.py` 的 `DEFAULT_PACKAGES` 同样缺失
- 影响: `image-job/job_persistence.py`(981 行)、`image_candidates.py`(909 行) 不受任何约束地增长；该服务同时是第一轮 4 个 P0 的所在地。
- 修复: 将 `image-job/image_job` 加入两个脚本的路径列表。

### 新-6. [P1] Worker 连接池仅 15 连接，而 `max_jobs=64` 【lead + E 区双重命中】
- 位置: `apps/worker/app/db.py:12-22` + `apps/worker/app/main.py:181`
- 证据: `create_async_engine(settings.database_url, pool_pre_ping=True, future=True)` —— 未配置任何池参数，落到 SQLAlchemy 默认 `pool_size=5 + max_overflow=10 = 15`；而 arq 并发为 64，缺口 49。
- 对比: `apps/api/app/db.py` 正确配置了 `pool_size/max_overflow/pool_timeout/pool_recycle`（`config.py:134-135`），**worker 与 api 配置严重不对称**。
- 影响: 高峰期 49 个任务排队等 `pool_timeout`(默认 30s) → 任务链超时 → arq 标记失败。而任务失败时上游可能已扣费，会把一个容量问题放大成资金问题。
- 修复: worker 引擎补齐 `pool_size=32, max_overflow=16, pool_timeout=10, pool_recycle=1800`，并统一走 settings 而非硬编码。

### 新-7. [P1] Redis 异常时锁续期静默继续，互斥锁实质失效
- 位置: `apps/worker/app/locks/owned_redis.py:84-109`
- 证据: `if renewed is False: return` —— `renewed is None`（Redis 异常）分支被忽略，续期循环继续，持锁协程继续执行。
- 触发: Redis 秒级抖动超过 ttl（outbox 10s / reconciler 50s）→ 锁真实过期 → 另一 worker 抢到同一把锁 → 两个 worker 并行跑对账/投递，并发写同一批 outbox 行。
- 修复: 改为 `if renewed is not True: return`，将 `None` 视同失去 ownership。

### 新-8. [P1] Redis lease key 被驱逐 → 运行中任务误判超时 → release 退费，违反纯转嫁
- 位置: `apps/worker/app/reconciliation/task_domains.py:162-208`、`reconciliation/lease.py:41-66`
- 证据: `state = LeaseState.EXPIRED if value is None else LeaseState.ACTIVE` —— `value=None` 不区分「正常过期」与「被 LRU 驱逐」。
- 触发: Redis 内存满且策略为 `allkeys-lru` → 驱逐进行中任务的 `task:{id}:lease` → 对账读到 EXPIRED → `_apply_timeout` → `release_generation`，**但上游已扣费，平台吸收成本**。
- 修复: 生产 Redis 强制 `maxmemory-policy noeviction`（或 `volatile-*` 且 lease 不设 volatile）；应用层改为在 lease value 内嵌过期时间戳自行校验，不依赖 key 存在性。

### 新-9. [P1] tgbot enhance-choice 双击竞态导致重复提交与重复扣费
- 位置: `apps/tgbot/app/handlers/generation.py:280-288`
- 证据: 先 `await msg.edit_reply_markup(...)`（网络 IO，不阻塞第二次回调），再 `_submit_generation()`，最后 `state.clear()`。
- 触发: 用户连点两次「使用优化版」→ 两协程并发进入提交；第一个已 `state.clear()`，第二个取不到原 `idempotency_key`，回退为 `make_idempotency_key("enhance", chat.id, cb.id)`——`cb.id` 不同 → **幂等键不同 → 重复提交、重复扣费**。
- 修复: 回调入口用 Redis `SET NX` 或 FSM 一次性锁，确保同一 idempotency_key 仅提交一次；进入即固化 key 再清 state。

### 新-10~12. [P1] 兑换码与 hold 相关的三处资金漏洞（D 区）
- **TTL 过期码可重复兑换**：过期判定与兑换写入非同一临界区，过期码在特定时序下仍可再次入账。
- **并发兑换非原子，可超上限**：并发请求下累计兑换额可突破单用户上限。
- **重试 hold 零成本漏洞**：重试路径下 hold 可被反复建立而不产生实际成本约束。
- 修复: 三者统一收敛到「`SELECT ... FOR UPDATE` 内完成过期判定 + 上限校验 + 写入」的单一原子块，并对兑换建立唯一约束兜底。

### 新-13. [P1] Workflow 路由重构导致 logger 名称断裂
- 位置: `apps/api/app/routes/workflows.py`
- 触发: 原 `workflow_routes.*` 分层 logger 名在合并为单文件后统一为 `workflows`。
- 影响: 依赖 logger 名的日志过滤/告警规则静默失效，排障时无法按子域切分。
- 修复: 按子域显式 `logging.getLogger("workflow_routes.<domain>")`，或同步更新日志规则配置。

### 新-14. [P1] 60+ 个 mutation 完全缺少 `onError`，含计费操作静默失败
- 位置: `apps/web` 工作流、海报、对话、Canvas、管理操作全线（与第一轮 I-1 同类，范围更广）
- 影响: 会扣费的操作失败后前端零提示，用户以为未执行而重复点击 → **二次扣费**。
- 修复: 建立 mutation 默认 `onError` 兜底（QueryClient 级），计费类操作强制显式错误提示。

### 新-15~17. [P1] 前端资源与状态泄漏三处
- `AdminUpdatePanel` EventSource 5 个监听器未清理 → 连接与回调泄漏。
- `DesktopAccountMenu` 把钱包**错误态**当作"无余额"隐藏 → 用户实际有余额却看不到入口。
- `StoragePanel` 轮询 `useEffect` 无 cleanup → 卸载后仍持续请求。

### 新-18. [P1] 3 处层次违规未被门禁捕获（C 区）
- 与新-2 同源：`check_architecture.py` 的检测口径存在盲区，除 images 环外另有 3 处跨层引用未被识别。
- 修复: 修正脚本口径后重跑，并把结果纳入 CI required check。

## 本轮新增 P2（17 项，按模块归类）

**计费与资金**
- `packages/core/lumen_core/billing.py:767-778` — `charge()` 默认 `cap_overdraw=True`，余额不足时静默清零并只在 meta 记 `overdraw_micro`；调用方不检查即视为扣费成功 → **用户可免费获取服务**。建议高单价调用显式传 `cap_overdraw=False`，并审计全部调用方。
- 视频计费使用 `ROUND_HALF_UP`，应为 `ROUND_UP` —— 向下取整的那半分钱由平台承担，与纯转嫁原则不符。
- 金额换算路径存在 float 中转，应全程 `Decimal` + 整数 micro。
- wallet→byok 切换后 `hold_micro` 未释放。

**Worker 运行时**
- `image_artifacts.py:143-165, 305-332` — Pillow 变体生成峰值内存 ~200MB/任务，4 并发即 ~800MB，容器 OOM 风险（OOM 时上游已扣费）。
- `account_limiter.py:802-838` — `release_quota(reserved_at=None)` 跨 UTC 日边界时 DECR 次日计数器，当日配额永久多计。
- lead 补充: worker 引擎缺 `pool_recycle`（长连接失效）；API/worker 数据库配置接口不一致。

**API 安全**
- `ratelimit.py:289` — X-Forwarded-For 信任边界依赖 `TRUSTED_PROXIES` 配置正确性，配错即可伪造 XFF 获得独立限流桶，绕过全部 IP 限流。
- `admin_backups.py:304, 856` — subprocess 已参数化（非 `shell=True`）且时间戳有正则校验，但**备份脚本路径未做 allowlist**，仍存注入面。

**前端**
- `MobileConversationDrawer` 等 3 处异步操作无竞态保护。
- memory / api-key / admin 页面多处 query 不检查 `isError`。
- `MarkdownPreview` 的 `dangerouslySetInnerHTML` 未净化 HTML。
- `download_token` 暴露在 URL 中（易进日志/Referer/历史记录）。
- logger 的 `ctx.extra` 可能携带敏感数据。

**部署与重构残留**
- `docker-compose.yml:100, 180` — `LUMEN_IMAGE_TAG:-latest` 默认可变 tag，部署不可复现、回滚不可预期，且可能与 migration 状态错配。建议改为 `:-` 强制 fail-fast。
- A 区: workflow 重构后存在重复实现；facade 私有符号消失导致部分测试改为断言"符号已清理"（语义弱化，见下）。

## 本轮新增 P3（11 项，摘要）

- **Worker**: 2GB 视频经 `read_bytes()` 全量入内存（`video_upstream.py:377-385`）；`background_removal/pipeline.py:36-44` 首个 provider 异常即终止整条链，后续 provider 无法兜底；`http_retry.py:185-239` 超时无条件重试，无幂等键的写路径可致上游双重计费；`RECON_STUCK_AFTER=5min` 与任务实际 25min 窗口不匹配，扩大误判面。
- **治理**: `packages/core/lumen_core/providers.py` 已 1486 行，逼近 1500 阈值（因 CI 不跑而静默越界）；baseline 棘轮机制无法区分"欠债已修复"与"新违规被豁免"。
- **安全**: SECRET_KEY 泄露即可伪造任意用户（含管理员）会话 —— 属密钥管理范畴，建议密钥轮换机制 + 会话绑定更多因子。
- **前端**: 19 个文件超 1000 行；localStorage 无 userId 隔离（同浏览器多账号串数据）；ErrorBoundary 未走统一 `logError`；URL 净化函数缺单元测试。
- **测试基础设施**: conftest 新增的 `_app_module_cache()` 无清理逻辑；`xfail_strict` 未配置（xpass 不会失败）。

## 重构语义验证结论

本轮专项验证两个未提交的大规模重构：

| 重构 | 规模 | 结论 | 架构评分 |
|------|------|------|---------|
| **Workflow 路由合并** | 删除 16 个路由文件，消除 ~2,200 行 facade | 154/154 测试通过；仅 P1×1（logger 名断裂）+ P2×2 | 8/10 |
| **Worker 域端口拆分** | 扁平 `g.xxx` → `g.{domain,persistence,events,billing,provider,queue,lease}.xxx` | **零 P0/P1/P2**；计费对称性全路径逐一核对通过；`frozen+slots` dataclass 提供强边界（访问未声明属性立即 `AttributeError`） | 8/10 |

**Worker 端口重构可直接合并**——这是本轮少见的完全干净的结论。Workflow 重构建议先修 logger 名称问题再合并。

## 模块拆分与耦合评估

**总体架构评分 ~8.4/10**。静态依赖方向、文件规模治理良好；主要缺口在运行时所有权与领域边界。

**耦合热点**
| 指标 | 模块 | 数值 | 判断 |
|------|------|------|------|
| 扇入最高 | `packages/core/lumen_core/models.py` | 154 个引用方 | 正常（数据契约本应被广泛依赖） |
| 扇出最危险 | `default_runtime.py` | 37 个依赖 | **God module 倾向**，需按域拆分 |
| 配置绕过 | 散落 `os.environ` | ~30 处 | 绕过 settings 层，配置不可审计 |

**`packages/core` 已从共享内核退化为杂物间**：`billing`/`video_billing`/`pricing`/`schema_models`/`model_entities` 是真正的跨服务契约，理应共享；但 `vision_tagging.py`(604)、`canvas.py`(940)、`canvas_models.py`(714)、`volcano_assets.py`(1241) 是业务领域逻辑，放在 core 会迫使所有服务承担无关依赖。建议下沉到各自服务或独立为 `lumen_canvas` / `lumen_vision` 包。

**分布式协议层是亮点**：`locks/` 是纯 Lua+asyncio 原语无业务泄漏；`outbox/` 完整封装 at-least-once 状态机，`contracts.py` 的 FailureMatrix 是可执行文档；`reconciliation/` 与 `outbox/` 正确解耦（对账只改 DB 行并 stage 事件，投递由外部完成）。

**仍待收敛的耦合**
1. `provider_pool.py` 运行时 `from . import account_limiter` 并直接调用其函数，非接口注入 → 测试必须 mock 模块而非替换实现。
2. `reconciliation/coordinator.py` 通过注入的 **module 对象** 调用 `billing.release_generation`，是 duck-typing 而非 Protocol → 类型检查无法捕获签名不匹配。
3. `tasks/outbox.py` 仍保留大量私有名别名（`_stage_outbox_event`、`_owned_redis_lock` 等），阻碍 `outbox/` 接口收敛。
4. `provider_pool.ProviderPool` 同时负责配置热加载、断路器、inflight 计数、quota 选号，`_select_for_image` 超 100 行 → 建议拆出 `ImageCandidateSelector`。

## 测试可信度评估：4/10

- **规模**: 3,082 个测试函数 / 117,763 行 / 251 文件 —— 体量充足。
- **质量**: 计费层测试可信（`packages/core/tests/test_billing.py` 无 mock 直接断言 micro 金额；worker 测试虽 mock 底层但校验了传入的 `actual_micro` 值）。**未发现 mock 自证循环、`assert True` 占位或异常 skip 堆积**（仅 8 处 skip，多数合理）。
- **致命盲区**: `image-job` 整个服务**仅 5 个测试文件**，第一轮该区 4 个 P0（不退款 / 重复扣费 / 幂等缺失 / 计费误报）在 CI 中**完全无法被检测**。「settle 成功后 commit 失败」的窗口期（第一轮 D-1/D-2/D-3）也无任何故障注入测试。
- **重构测试变更评估**: git diff 中的测试改动整体是适配端口拆分的合理重构，未见削弱断言或删除用例；但 `test_workflow_service_facades.py` 有一处语义反转——从"断言 facade 是正确 alias"改为"断言符号已被清理"，需确认这是有意的迁移终态。

评分被拉低的唯一原因是**覆盖分布失衡**：资金风险最高的模块测试最薄。

## 修复优先级

### 🔴 立即（本周）
1. **让 CI 真正跑治理 gate**（新-4、新-5）— 30 分钟，是后续一切修复不再回退的前提。
2. **删除 `settle_video_cost` 的 3 倍封顶**（新-1）— 双轮确认的资金泄漏，改动仅数行。
3. **补齐 worker 连接池配置**（新-6）— 10 分钟，防止容量问题升级为资金问题。
4. **Redis 生产配置改 `noeviction` + 锁续期 `is not True`**（新-7、新-8）— 消除两处纯转嫁破坏路径。

### 🟡 本迭代（2 周）
5. 制定**统一的「上游计费可知性 → 本地计费动作」决策表**并落到 `packages/core`，一次性收敛第一轮 E-1/E-2/E-3/F-1/F-2/F-3/G-1 与本轮新-1、新-10~12。这是 P0 集中区的根因，逐个修补无法收敛。
6. 补 image-job 的 settle/release 状态机与幂等键（第一轮 H-1~H-4、H-3/H-6），同步补测试——当前该区零覆盖。
7. 修复 images 层循环依赖并校正 `check_architecture.py` 口径（新-2、新-18）。
8. 前端 mutation 全局 `onError` 兜底 + `formatRmb` 错误态（新-3、新-14）。

### 🟢 排期
9. 拆分 `default_runtime.py`(37 依赖) 与 `provider_pool.py`(1333 行)；`packages/core` 剥离业务领域模块。
10. 收敛 ~30 处 `os.environ` 到 settings 层。
11. 本轮 P2/P3 按模块批量处理。

### 建议新增的 CI gate
本轮手动发现的问题中，有 4 类可以固化为自动检查：
1. **连接池与并发度匹配**（`max_jobs` vs `pool_size+max_overflow`）
2. **计费调用对称性**（settle 必对应 charge，release 必配对）
3. **事务边界**（settle/charge 必须在 commit 之后或有补偿路径）
4. **幂等键维度完整性**（须含 provider 与 job 维度）

---

# 第一轮审计

## 执行摘要

**共确认 126 个问题：P0×23 · P1×40 · P2×36 · P3×27**（另有 A 区 2 项 P3 观察记录未计入）。

> **修复状态复核（本轮补充）**：23 个 P0 中仅 2 个已修复、2 个部分修复、**19 个仍存在**。下方清单仍然有效，不是历史记录。

| 分区 | 模块 | P0 | P1 | P2 | P3 | 小计 |
|------|------|----|----|----|----|------|
| A | API 安全与路由 | 0 | 0 | 0 | 0 | **0** ✅ |
| B | API 路由与中间件 | 0 | 0 | 1 | 2 | 3 |
| C | Web 数据流与 hooks | 1 | 7 | 4 | 1 | 13 |
| D | Worker 热点重构区 | 3 | 5 | 4 | 3 | 15 |
| E | Worker 其余运行时 | 7 | 12 | 11 | 8 | **38** |
| F | Core 计费与安全 | 6 | 6 | 6 | 3 | **21** |
| G | API 计费与生成端点 | 1 | 1 | 0 | 0 | 2 |
| H | image-job 服务 | 4 | 5 | 5 | 5 | **19** |
| I | Web 交互组件 | 1 | 2 | 3 | 2 | 8 |
| J | tgbot 与脚本/部署 | 0 | 1 | 1 | 5 | 7 |
| | **合计** | **23** | **40** | **36** | **27** | **126** |

### 核心结论

代码库整体工程质量很高（多重 CI gate 使低级 bug 基本绝迹），**但风险高度集中在一处：计费对称性**。23 个 P0 中有 **15 个直接涉及资金**，且呈现出四个互相矛盾的失衡方向——说明系统缺少一张统一的「上游计费可知性 → 本地计费动作」决策表，各模块各自为政：

| 失衡方向 | 后果 | 代表问题 |
|---------|------|---------|
| **平台吸收上游成本** | 违反「纯转嫁」原则，平台亏损 | E-1 · E-2 · F-2 · G-1 · D-1 · D-2 · D-3 |
| **多扣用户** | 超额扣费，引发纠纷 | E-3 · F-1 |
| **重复扣费** | 同一任务扣多次 | H-3 · H-6 · E-11 · E-12 |
| **收费未交付 / 免费获取** | 用户付费无结果，或反向刷余额 | H-1 · H-2 · H-9 · F-3 · F-4 · F-5 |

> **判定基准**：业务规则为「视频生成计费纯转嫁——平台绝不吸收上游成本，只要上游扣费用户就必须付」。据此，**在上游是否已扣费不可知时，默认动作必须是 settle 而非 release**。当前 `submit_unknown` 默认 release（E-2）与 `billable_hint=None` 默认按最大值扣费（E-3）在同一模块内做出了方向相反的默认选择，是最需优先统一的矛盾点。

### 其余风险主题

1. **事务边界与异常窗口**（D-1/D-2/D-3、E-6/E-7）：`settle`/`charge` 与 DB commit 之间存在异常窗口，上游已扣费而本地事务回滚。worker 重构把 `g.xxx` 拆分为分组端口后，settle/release 调用分散到多模块，原「先 persist 再 settle」的时序保证出现断裂。
2. **基础设施容量**（E-4/E-5）：连接池按默认值运行（`pool_size=5`）却配 `max_jobs=64`，且 shutdown 不 dispose——属于会放大其他所有故障的全局性隐患。
3. **安全加固**（F-6/F-13 时序攻击、F-12 DNS rebinding、E-22 redirect SSRF、F-16 固定盐、H-13 Pillow 绕过）：均非当前可直接利用的严重漏洞，但构成纵深防御缺口。
4. **前端静默失败**（I-1、C 区 `formatRmb`）：mutation 缺 `onError` 导致含计费操作在内的失败无任何提示；余额异常显示为 `0.00` 而非错误态。

### 建议修复顺序

1. **立即**：制定统一的计费决策表并落到 `packages/core`，收敛 E-1/E-2/E-3/F-1/F-2/F-3/G-1；补齐 image-job 的 settle/release 状态机（H-1~H-4）。
2. **本周**：修复整数溢出与取整方向（F-4/F-5/F-2）；重排 D-1/D-2/D-3 的 settle 与 commit 时序；配置连接池与 dispose（E-4/E-5）。
3. **本迭代**：事务边界统一 `session.begin()`（E-6/E-7）；幂等键纳入 provider 与 job 维度（E-11/H-3/H-6）。
4. **排期**：安全加固与健壮性项（P2/P3）。

---

## 审计方法

- **9 路并行深挖 agent**，分区：worker 热点重构区 / worker 其余运行时 / core 计费与安全 / api 鉴权与路由 / api 计费与生成端点 / image-job 服务 / web 数据流与 hooks / web 交互组件 / tgbot 与脚本部署。
- **lead 独立全局交叉检查**：危险模式扫描、未提交重构语义核对、代码卫生、DB 迁移安全、测试可信度。
- 每项发现要求带 `文件:行号`、触发场景、影响与修复方向；仅收录可确认触发路径的真实问题。

严重程度定义：**P0** 资金损失/数据损坏/安全绕过 · **P1** 功能崩溃/明确逻辑错误 · **P2** 边界条件/次要错误 · **P3** 健壮性隐患。

---

## 整体工程健康评估（lead 独立检查）

该代码库经过严格治理（ruff、architecture check、complexity check、module runtime state audit 等多重 CI gate），低级 bug 已基本绝迹：

- **危险模式**：无 `shell=True`、无危险 `eval/exec/pickle.loads/yaml.load`、无硬编码密钥、无吞异常的裸 `except`（全部 `eval` 均为正常 Redis Lua 脚本）。
- **代码卫生**：全库仅 1 处 `TODO`（`packages/core/lumen_core/schema_models/messaging.py:53`，有意的兼容保留）、无可变默认参数、无 `== None` 反模式、金额一律用整数 micro 单位。
- **未提交 worker 重构**：证实为把扁平 `g.xxx` 改为按域分组 `g.{domain,persistence,events,billing,provider,queue,lease}.xxx` 的机械搬迁；退款 `release_generation`、锁释放 `_release_lease` 等关键逻辑均保留；`runtime.py` 用 `frozen+slots` dataclass 提供强边界（访问未声明属性会立即 `AttributeError`）。
- **DB 迁移**：46 个迁移，破坏性操作基本位于 `downgrade()` 回滚路径；有 `lint_alembic_breaking` + `test_migration_lock_safety` 把关。
- **测试可信度**：5875 个测试（3249 Python + 2626 TS/TSX），跳过项全为合理的环境依赖，无 `assert True` 占位。

**结论**：真正的风险集中在并发/竞态、状态机、计费精度与时序、跨模块契约，而非低级错误。

---

## 发现汇总

> 说明：下列各区按并行 agent 分区归类，已全部完成。E、F、H 三区因首轮 agent 遭上游 502 中断而重启，其中 F、H 两区获得两轮独立审计结果，已交叉合并去重（标注 **[双轮确认]** 的条目为两轮独立命中，可信度最高）。

### A. API 安全与路由层 — 未发现可确认漏洞 ✅

对全部 API 入口层（路由、认证/会话、CSRF、授权/IDOR、SSRF、注入、文件路径、限流、webhook、admin、SSE）做逐端点数据流追踪 + 三路并行深挖（IDOR / SSRF / 注入路径穿越），七大类均未发现可利用 bug。已验证的关键防护：

- **认证/授权**：会话 cookie 为 HMAC-SHA256 签名引用，`require_active_session_user` 校验 `revoked_at`/`expires_at`/`user.deleted_at`；登出真正置 `revoked_at`，密码重置吊销该用户全部会话；IDOR 全面核验，嵌套子对象（Message/Share/ImageVariant/Canvas run/Storyboard step）均通过 join 父对象 `user_id` 强制 owner 过滤；admin 端点均带 `AdminUser` 依赖。
- **CSRF**：全部状态改变端点覆盖完整（双提交 + session 绑定 HMAC），"无 CSRF" 者均为正当情形（登录前 auth 路由带 IP 限流、telegram 走 `X-Bot-Token` 服务间鉴权）。
- **SSRF**：`packages/core/lumen_core/url_security.py` 防护全面（DNS 解析后拉黑私网/环回/链路本地/保留网段、IPv4-mapped IPv6、八进制/十六进制/十进制 IPv4、连接级 DNS pin、逐跳重定向重校验、`follow_redirects=False`）；BYOK `base_url` 非用户可控（仅 admin supplier 模板）。
- **注入**：无 SQL 拼接（全参数化，`text()` 仅静态串）；路径穿越有 `resolve_storage_path` + `open_regular_file_no_symlink` 双重防护；无 `shell=True`/`os.system`。
- **敏感信息**：BYOK key 存储前 AES-GCM 加密，响应仅回脱敏 `key_hint`；统一异常处理器不外泄内部细节。
- **竞态/TOCTOU**：钱包全部变更走 `SELECT ... FOR UPDATE` 行锁并在锁内复检幂等键+余额；兑换码 `with_for_update` + 唯一约束防重复兑换。

#### 低危观察（非漏洞，记录备查）

##### [P3] worker 侧辅助 LLM 调用未使用 DNS-pin transport
- 文件: `apps/worker/app/tasks/auto_title.py`、`memory_extraction.py`、`context_image_caption.py`
- 问题: 这些辅助 LLM 调用未使用 `pinned_async_http_transport`，与主生成路径的 SSRF 加固不一致。
- 触发场景: 需要 `base_url` 可被污染才能利用，但其来源为 admin provider pool / supplier 模板，非请求级用户可控。
- 影响: 当前不可利用；属加固一致性差异。
- 建议: 统一为所有出站 LLM 调用套用 pinned transport，消除未来引入用户可控 `base_url` 时的隐患。

##### [P3] ffprobe stderr 回显可能暴露内部存储路径
- 文件: `apps/api/app/routes/video_reference_videos.py`
- 问题: 无效视频时把 ffprobe stderr 末 500 字回给用户，可能含内部存储路径。
- 影响: 极低危（回显的是用户自己上传文件的路径）。
- 建议: 对回显的 stderr 做路径脱敏或改为通用错误信息。

---

### B. API 路由与中间件层 — 发现 3 个问题（P2×1, P3×2）

对 API 路由、鉴权基础设施、中间件、SSE 的完整审计。已排除的高危类别：IDOR/越权（所有资源访问都校验 owner）、CSRF（全覆盖）、路径穿越（双重防护）、SSRF（用户侧 URL 校验完整）、限流（昂贵端点已保护）、竞态（关键写操作用行锁+幂等键）。

#### [P2] NavFeatureGuard 中间件在每个特性路径请求上无缓存地查询数据库
- 文件: `apps/api/app/main.py:292-305` + `apps/api/app/runtime_settings.py:71-83`
- 问题: `_NavFeatureGuardMiddleware` 拦截特性路径（`/workflows`、`/canvases`、`/storyboards` 等）时，**每个请求都打开新 DB session**（`async with db_sessionmaker() as db`）并调用 `get_setting(db, spec)` 读 `SystemSetting` 表。`get_setting` 无任何缓存，每次都查数据库。
- 触发场景: 用户高频访问 workflows/canvases 路径（正常使用），中间件在**鉴权前**执行，未认证请求也会触发 DB 查询。
- 影响:
  - **数据库连接池压力**：每请求一个 session，高并发下可耗尽连接池。
  - **查询开销**：`SystemSetting` 表虽小但乘以请求量后显著。
  - **DoS 风险**：攻击者可向特性路径发送大量未认证请求（OPTIONS 之外的任何方法），强制 DB 查询，绕过应用层限流（限流在路由层，中间件先于限流执行）。
- 建议:
  1. **应用内缓存**：用 TTL 缓存（如 Redis 或内存 + 定时刷新）缓存 `get_setting` 结果，TTL 30-60 秒。
  2. **惰性加载**：仅在路由处理函数内（认证后）检查特性开关，而非中间件。
  3. **请求级缓存**：若必须保留中间件，在 `request.state` 缓存当前请求的查询结果，避免重复查库。

#### [P3] SSE 事件流 Last-Event-ID 解析未处理超大输入
- 文件: `apps/api/app/routes/events.py:135-149`
- 问题: `_parse_last_event_id` 解析 `Last-Event-ID` header（逗号分隔的多个 ID）时，用 `value.split(",")` 无长度限制。恶意客户端可发送巨型 header（如 100MB 逗号分隔的垃圾 ID），导致内存分配 + CPU 消耗。虽然 Starlette 默认限制 header 总大小（通常 8-16KB），但若部署环境（如 nginx）提升了 `client_header_buffer_size`，攻击面就打开。
- 触发场景: 攻击者构造 `Last-Event-ID: id1,id2,...,idN`（N 巨大），向 `/events?channels=...` 发起 SSE 连接。
- 影响:
  - 单请求可消耗数十 MB 内存（split 生成大列表）。
  - 后续 `_decode_event_id_token` 遍历所有 token，CPU 开销线性增长。
  - 并发攻击可耗尽进程内存或 CPU，影响其他用户。
- 建议:
  1. 限制 split 后的 token 数量：`tokens = value.split(",")[:10]`（只取前 10 个）。
  2. 或提前检查 header 长度：`if len(value) > 1024: raise HTTPException(400, "Last-Event-ID too large")`。

#### [P3] 视频 reference token 过期时间可被用户控制为极远的未来（潜在风险）
- 文件: `apps/api/app/services/video/reference_media.py:59-65`
- 问题: `reference_token_expiry()` 生成 24 小时后的过期时间，但 `now` 参数可由调用方传入。追溯调用链，**当前代码无直接用户控制点**（调用方都用 `datetime.now()`），但函数签名允许任意 `now`，若未来某处误用（如接受用户提供的时间戳），可生成永不过期的 token。
- 触发场景: 当前**无直接触发路径**，但存在潜在风险。
- 影响: 若误用，用户可生成永久有效的 reference token，绕过 24 小时 TTL，长期访问本应过期的引用媒体。
- 建议:
  1. 移除 `now` 参数，强制函数内部用 `datetime.now(timezone.utc)`。
  2. 或增加防御：`if now is not None and (now - datetime.now(timezone.utc)).total_seconds() > 60: raise ValueError("now cannot be in the future")`。

---

### C. Web 数据流与 hooks — 发现 13 个问题（P0×1, P1×7, P2×4, P3×1）

对 Next.js 前端的业务逻辑层深度审计，聚焦 API 客户端、状态管理、自定义 hooks、SSE/流处理、计费余额展示。

#### [P0] formatRmb 对 NaN/Infinity 的处理导致余额显示为 "0.00" 而非错误提示
- 文件: `src/lib/money.ts:1-5` + `src/app/me/wallet/useWalletPageModel.ts:189`
- 问题: `formatRmb` 在 `!Number.isFinite(amount)` 时返回 `(0).toFixed(fractionDigits)`，将非法输入（后端返回 `null`、字符串 `"abc"` 或计算错误的 `NaN`）显示为 `"0.00"`。用户无法区分真实余额为零还是数据异常。
- 触发场景: 后端 API 返回格式错误的余额数据，或前端计算错误（如除以零）。
- 影响: **资金显示错误**——用户误以为余额为零而无法充值/生成，或在实际有余额时被误导，可能导致客服投诉。根据 memory 的「视频计费纯转嫁」策略，余额显示错误直接影响计费透明度。
- 建议: 返回明确的错误标识（如 `"--"` 或 `"N/A"`），或抛出错误由调用方处理；在钱包页面增加数据校验并显示错误提示。

#### [P1] useSSE hook 的 handlers 依赖处理导致闭包陈旧
- 文件: `src/lib/useSSE.ts:58-61, 115-120`
- 问题: `useEffect` 使用 `handlersRef.current` 和 `optionsRef.current` 避免依赖，但父组件更新 handlers 函数引用时（`Object.keys(handlers)` 不变），SSE 事件处理器仍使用旧闭包。
- 触发场景: 父组件更新 handlers 函数但 eventKey 不变。
- 影响: SSE 事件处理可能读取陈旧状态，导致生成/完成任务状态更新丢失或应用到错误的会话。
- 建议: 将 ref 赋值移到独立的 `useEffect` 或添加警告要求调用者用 `useCallback` 稳定 handlers。

#### [P1] SSE Runtime 的 openSource 在 recoveryFlight 中可能重复打开连接
- 文件: `src/lib/sse/runtime.ts:247-255`
- 问题: `applyEffect` 中 `openSource` 在 `recoveryFlight` 异步恢复进行中时可能被重复触发（如 `manual_reconnect` 事件），导致短时间内连续打开/关闭连接。
- 触发场景: 用户在快照恢复期间多次点击重连，或网络状态快速变化。
- 影响: EventSource 连接泄漏、服务器连接计数异常、客户端 SSE 事件乱序或重复。
- 建议: 在 `recoveryFlight` 进行中时忽略 `openSource` 效果，或阻止 `manual_reconnect` 事件传播。

#### [P1] useLumenRealtime 的 completionIds 未过滤空字符串
- 文件: `src/lib/sse/useLumenRealtime.ts:124-137`
- 问题: `completionIds` 检查 `assistant.completion_id && !assistant.completion_id.startsWith("opt-")` 但不排除空字符串。后端返回 `completion_id: ""` 会生成错误的 channel `task:`。
- 触发场景: 后端边界情况或迁移遗留的空 completion_id。
- 影响: SSE 无法接收该任务更新，任务状态永久卡在 pending/streaming。
- 建议: 添加 `.length > 0` 检查。同样适用于 `assistantTaskIds` 和 `generationTaskIds`。

#### [P1] completionStreamPatches 合并逻辑在终端状态可能重复追加文本
- 文件: `src/store/chat/completionStreamPatches.ts:89-90, 102-107`
- 问题: `appendPatchValue` 在终端状态下仍可能追加文本。如果 SSE 事件延迟到达，`completion.succeeded` 已设置完整 `text`，但缓冲的 `completion.delta` 仍在队列中。
- 触发场景: SSE 事件延迟，终端事件先于 delta 被处理。
- 影响: 终端消息文本尾部重复（如 "你好你好"），用户体验差，且无法通过刷新修复（已持久化）。
- 建议: 在 `applyPatchesToMessage` 中，如果 `isTerminalMessage(next)` 且 `next.text` 已存在且较长，跳过所有 patch 应用。

#### [P1] eventSourceTransport 的 sequence 机制在快速 open/close 时可能导致事件丢失
- 文件: `src/lib/sse/eventSourceTransport.ts:51-55, 75-76`
- 问题: `sequence` 递增区分新旧连接，但 `active()` 的 `sequence === this.sequence` 检查在并发下有竞态。连接 A 的 `source.close()` 在连接 B 创建中调用，可能在 A 收到关键事件前关闭。
- 触发场景: 用户快速切换会话或网络抖动导致 SSE 快速重连。
- 影响: 生成任务状态更新丢失，UI 永久卡在 "生成中"。
- 建议: 在 `close()` 中立即标记连接为 inactive（独立 `closed` 标志），不依赖 `sequence` 比较；或增加短暂宽限期（100ms）允许旧连接事件通过。

#### [P1] runtime.ts 的 rememberCompletionMessage 在 ID 为 undefined 时静默跳过
- 文件: `src/store/chat/runtime.ts:622-632` + `src/store/chat/sendMessageAction.ts:556`
- 问题: `rememberCompletionMessage` 在参数为 `undefined/null` 时提前返回，但调用方可能假设已注册。后续 `completion.delta` 到达时 messageId 映射缺失，patch 无法应用。
- 触发场景: 聊天模式下后端未返回 `completion_id`（边界情况），但后续 SSE 仍发送 `completion.delta`。
- 影响: 文本流式更新失败，用户看到空白消息。
- 建议: 在参数无效时记录警告，并在 `sseEventActions.ts:521` 增加回退逻辑。

#### [P1] sendMessageAction 失败回滚后未清理 _generationIdAliases
- 文件: `src/store/chat/sendMessageAction.ts:646-647, 467`
- 问题: `removeOptimisticSend` 删除 `_generationConvIds`，但 `registerResponseAliases` 注册的 `_generationIdAliases` 映射未清理。
- 触发场景: 网络分区导致请求部分成功（后端创建任务但前端未收到响应，然后重试）。
- 影响: 内存泄漏（`_generationIdAliases` 持续增长），后续 SSE 事件可能匹配到已删除的 optimistic ID。
- 建议: 在 `removeOptimisticSend` 中遍历 `optimistic.generationIds` 并从 `_generationIdAliases` 删除所有相关条目。

#### [P2] useIdentityRevalidation 的定时器未在 retryAttempt 更新时清理
- 文件: `src/components/useIdentityRevalidation.ts:260-262`
- 问题: `scheduleRetry` 在创建定时器前更新 `state.retryAttempt`，但多次调用时因 `state.retryTimer !== null` 守卫提前返回，`retryAttempt` 已递增导致延迟计算错误。
- 触发场景: 快速连续的网络失败（在线/离线快速切换）。
- 影响: 重试延迟比预期更长，降低恢复速度。
- 建议: 在 `state.retryTimer !== null` 时不递增 `retryAttempt`，或在清除定时器时重置。

#### [P2] useWalletPageModel 的 redeemMutation 成功后 invalidateQueries 范围过大
- 文件: `src/app/me/wallet/useWalletPageModel.ts:152-154`
- 问题: `invalidateQueries({ queryKey: queryKeys.all })` 使所有钱包查询失效，触发不必要的重新获取。
- 触发场景: 用户兑换充值码。
- 影响: UX 轻微下降（加载状态闪烁），额外的 API 请求。
- 建议: 用 `setQueryData` 直接更新 `walletQuery` 余额（乐观更新），只 invalidate `snapshotQuery` 和 `transactionsQuery`。

#### [P2] connectionMachine 的 maxRetryCount 检查多执行一次
- 文件: `src/lib/sse/connectionMachine.ts:148-149, 159`
- 问题: `if (priorAttempt >= max)` 在 attempt 达到 max 后才关闭，但 `attempt` 在 backoff 时已递增。`maxRetryCount: 3` 实际执行 4 次连接。
- 触发场景: 设置了 `maxRetryCount` 的 SSE 连接持续失败。
- 影响: 比预期多一次重试，违反用户配置语义。
- 建议: 修改为 `if (priorAttempt + 1 > max)` 或在 backoff 前检查。

#### [P2] completionStreamPatches 终端状态 patch 应用逻辑（补充说明）
- 文件: `src/store/chat/completionStreamPatches.ts:98`
- 问题: 同 P1 completionStreamPatches 问题的补充——需在应用 patch 前检查 `isTerminalMessage`。
- 建议: 见上文 P1 问题。

#### [P3] csrfService 的 waitForCaller 监听器清理（已修复）
- 文件: `src/lib/api/csrf.ts:119-132`
- 问题: 理论上存在监听器泄漏，但第 128 行已使用 `{ once: true }` 选项，实际不会发生。
- 影响: 无。
- 建议: 可忽略。

---

### D. Worker 热点重构区 — 发现 15 个问题（P0×3, P1×5, P2×4, P3×3）

#### D-1. [P0] bonus image settle 缺少异常回滚机制
- 位置: `apps/worker/app/tasks/generation_parts/persistence.py:699-706`
- 触发: bonus image（dual_race loser 或 batch extra）处理时在 settle_generation 之后、commit 之前发生异常（网络中断、DB 连接断开、lease_lost 检查触发）。
- 影响: 上游 gateway 已扣费（settle_generation 完成），但事务回滚导致 generation 记录未写入，**用户被扣费但无图片记录，资金永久丢失**。
- 修复: 将 settle_generation 调用移到 commit 之后，或在异常处理中补偿性调用 release_generation。

#### D-2. [P0] success 路径 settle 前的 lease_lost 检查窗口期资金不对称
- 位置: `apps/worker/app/tasks/generation_parts/success.py:349-366`
- 触发: `_attach_image_to_message` 完成后、`_record_success_hooks` 执行期间（L361-L366 的 3 个 hook 可能耗时数百毫秒），lease 过期或续期失败被标记为 lost。
- 影响: L362 的 `_raise_if_generation_interrupted` 抛出 LeaseLost；上游已生成图片（已扣费），Image/ImageVariant/Generation 行已添加到 session；事务回滚但上游扣费无法追回；runner.py 的 failure handler 会调用 release，但 generation 行在 DB 中已回滚为 RUNNING 状态，billing 状态不匹配。
- 修复: 将所有 3 个 hook 调用移到 settle_generation 之后，或去掉 L349-L366 之间的所有 lease_lost 检查（已进入 commit 关键路径）。

#### D-3. [P0] completion settle 前的 cancel 检查导致资金泄漏
- 位置: `apps/worker/app/tasks/completion_parts/outcomes.py:161-174`
- 触发: charge_completion(L166) 之前的 2 个 cancel 检查（L161、L170）抛出 TaskCancelled。
- 影响: L161 检查通过，L166 charge_completion 完成（用户已被扣费）；L170 检查发现 cancel，抛出 TaskCancelled；session 事务回滚，Completion 记录未标记为 SUCCEEDED；runner.py 的 _settle_cancelled 会因 usage_is_finalized=True 重复扣费或因状态不匹配无法退款。
- 修复: 将 L170 的 cancel 检查移到 charge_completion 之后，或在 charge_completion 之前彻底禁止 cancel 检查。

#### D-4. [P1] dual_race bonus settle_billing 标志与 billing_free 冲突
- 位置: `apps/worker/app/tasks/generation_parts/persistence.py:549-556`
- 触发: billing_meta 指定 `billing_free=True` 但 `settle_billing=False`。
- 影响: L549 的 warning 警告缺少 settle_billing，但 L543 默认生成的 billing_meta 会设置 `billing_free=False`，逻辑矛盾——免费图片不应要求 settle。
- 修复: 在 L549 判断中增加 `and not billing_free` 条件，或重构逻辑使 billing_free 图片自动跳过 settle 要求。

#### D-5. [P1] existing image settlement 未验证 image_count 参数
- 位置: `apps/worker/app/tasks/generation_parts/lifecycle.py:122-127`
- 触发: existing_image 是 batch 或 dual_race 场景遗留的非主图片，width/height 正常但实际是多图之一。
- 影响: settle_generation 默认 image_count=1（未显式传参），但 generation.upstream_request 可能记录了 n>1，导致计费金额不匹配。
- 修复: 从 existing_image.metadata_jsonb 或 generation.upstream_request 中提取正确的 image_count 传入。

#### D-6. [P1] retry 失败后 avoid_provider 未清理
- 位置: `apps/worker/app/tasks/generation_parts/failure.py:344-358`
- 触发: retry 流程中 _persist_retry_state 或 _enqueue_retry 失败（L367-L445）。
- 影响: L361-L364 已调用 `_avoid_provider_for_task` 设置 Redis avoid key，但后续失败流转为 terminal failure 时未清理，该 generation_id 的 avoid set 会残留到 TTL 过期。
- 修复: 在 L430-L445 的 terminal failure 分支中调用 `_clear_avoided_providers`。

#### D-7. [P1] lease renewer 连续失败阈值窗口期未释放资源
- 位置: `apps/worker/app/tasks/generation_parts/lease.py:157-173`
- 触发: 租约续期连续失败 2 次后进入第 3 次失败。
- 影响: L165 判断 `>= 3` 才设置 lease_lost 并返回，但前 2 次失败时 generation 仍在执行，直到第 3 次失败才中断，期间 provider slot 未释放导致队列阻塞。
- 修复: 将阈值降为 2，或在第 2 次失败时预警并提前标记 lease_lost。

#### D-8. [P1] queue_lock 心跳失败未提前中断流水线
- 位置: `apps/worker/app/tasks/generation_parts/queue_lock.py:283-300`
- 触发: _heartbeat 协程在 L290 续期失败但未抛出异常，只调用 _mark_lost 并返回。
- 影响: 主流程（reserve_image_queue_slot）持有的 lock 对象 lost 事件已 set，但如果主流程未检查 lost 状态就继续执行 eval_fenced，会在 lost_result=-1 时才发现锁已丢失，期间可能已完成部分写操作。
- 修复: 在 L294 返回前抛出 ImageQueueLockLost 异常而非静默返回。

#### D-9. [P2] bonus image sha256 echo 检测未处理非 EDIT action
- 位置: `apps/worker/app/tasks/generation_parts/persistence.py:497-512`
- 触发: action != EDIT 但 references 非空（理论上不应发生，但系统演进可能引入新 action）。
- 影响: L501 检查 action == EDIT.value 才进行 sha echo 检测，其他 action 即使返回原图也会被接受。
- 修复: 对所有有 references 的 action 都执行 sha echo 检测，或显式断言非 EDIT action 的 references 必为空。

#### D-10. [P2] completion 工具图片预留预算未归还
- 位置: `apps/worker/app/tasks/completion_parts/runner.py:1254-1261` 和 `outcomes.py:74-90`
- 触发: 流式处理中 reserved_tool_image_budget_micro > 0 但最终未生成工具图片（如工具调用被 truncate）。
- 影响: 预留的 budget_micro 未在失败路径中退还，用户余额被冻结但未消费（虽然 fallback token 计算会补偿，但预留机制失效）。
- 修复: 在 _handle_failure 和 _settle_cancelled 中检查 reserved_tool_image_budget_micro，未消费的部分应退还或记录。

#### D-11. [P2] image queue reservation token 泄漏
- 位置: `apps/worker/app/tasks/generation_parts/queue_claim.py:646-675`
- 触发: release_image_queue_slot 使用 lease_token 成功释放资源，但 reservation_token 在 ContextVar 中未被清理（L672 只在 reservation_token 分支清理）。
- 影响: ContextVar 中残留 token 映射，长期运行的 worker 进程内存泄漏（虽然单个 token 很小，但高并发下累积）。
- 修复: L656 成功释放后无论使用哪个 token 都应调用 _forget_image_queue_reservation_token。

#### D-12. [P2] provider_attempt_log 截断不一致
- 位置: `apps/worker/app/tasks/generation_parts/failure.py:246` 和 `success.py:463`
- 触发: provider_attempt_log 长度超过 12。
- 影响: failure.py L246 截取前 12 条，success.py L463 也截取前 12 条，但若 provider 切换频繁，后续重试的 attempts 会被截断，diagnostics 数据不完整。
- 修复: 统一截断逻辑，优先保留最近的 attempts（从尾部截取）或提高上限到 20。

#### D-13. [P3] storage cleanup 异常被静默吞没
- 位置: `apps/worker/app/tasks/generation_parts/persistence.py:313-317`
- 触发: _delete_storage_keys 在 cleanup_storage_on_error 的 except 块中被调用，但其内部异常被 _wait_for_storage_task 捕获后只记录日志。
- 影响: 存储清理失败导致孤立文件残留，长期累积浪费存储空间。
- 修复: 在清理失败后将 keys 写入持久化的清理队列，由后台任务定期重试。

#### D-14. [P3] lease 释放失败日志级别过低
- 位置: `apps/worker/app/tasks/generation_parts/lease.py:89-95`
- 触发: release_lease 的 RELEASE_LEASE_LUA 脚本执行失败。
- 影响: L93 使用 debug 级别记录，生产环境默认不打印，导致租约泄漏难以排查。
- 修复: 将日志级别提升为 warning 并增加 task_id 上下文。

#### D-15. [P3] queue_lock WATCH 事务重试上限过低
- 位置: `apps/worker/app/tasks/generation_parts/queue_lock.py:85-121` 和 `130-162`
- 触发: 高并发下 WATCH 冲突导致 WatchError。
- 影响: FALLBACK_RETRIES=3 在高负载下容易耗尽，直接抛出 LOCAL_QUEUE_FULL 拒绝任务。
- 修复: 将重试上限提升到 5-10 次，并在重试间加入指数退避。

> **关键风险总结**：重构将 `g.xxx` 迁移到 `g.{billing,persistence,lease}.xxx` 后，settle/release 调用分散在多个模块，原有的"先 persist 再 settle"模式在异常处理中出现断裂。3 个 P0 均涉及 **settle/charge 与事务边界的异常处理窗口期**，违反"上游扣费即用户必被扣"的对称性原则。
---

### E. Worker 其余运行时 — 发现 38 个问题（P0×7, P1×12, P2×11, P3×8）

本区由 3 个子 agent 并行覆盖：**上游适配层**（`upstream_parts`、视频/图片 provider 交互与计费判定）、**杂项任务**（auto_title、storyboard、reconciliation、限流、可观测性）、**基础设施**（DB/Redis 连接、事务边界、锁、存储、生命周期）。已合并去重：`video_billing.py` 的 `upstream_billable` 信任问题由两个子 agent 独立发现，合并为 E-1。

#### E-1. [P0] 视频计费完全信任上游 `upstream_billable` 标志，误判即平台吸收成本
- 位置: `upstream_parts/video_billing.py:140-204`（判定分支）、`:196-201`（billable=True 但本地失败时无防御分支）
- 触发: 上游返回 `upstream_billable=False` 但实际已扣费；或标记 `True` 而本地判定为失败时逻辑未覆盖。当前仅检查少数 `safe_reasons` 白名单，其余失败原因一律走 release。
- 影响: **直接违反「上游扣费用户必付」原则**——上游已计费而平台执行 release，成本由平台吸收。两个独立子 agent 交叉确认同一缺陷。
- 修复: 反转信任模型为**白名单 release**：仅在能证明上游未扣费的枚举原因下 release，其余一律 settle；`upstream_billable=True` 无条件 settle。

#### E-2. [P0] 视频 `SUBMIT_UNKNOWN` 默认 release 而非 settle
- 位置: `upstream_parts/video_billing.py:140-152`
- 触发: submit 结果不可知（连接中断、网关超时），上游可能已受理并扣费。
- 影响: release hold 后平台承担已发生的上游成本，同样违反纯转嫁原则。
- 修复: `submit_unknown` 默认 settle（按最小可信金额），或转入人工/对账队列，禁止直接 release。

#### E-3. [P0] submit 失败后依据不确定的 `billable_hint` 按最大值扣费
- 位置: `upstream_parts/submission.py:845-863`
- 触发: `_submit_failure_billable_hint(exc)` 返回 `None` 时传入 `upstream_billable=None`，落入 `unknown_default_charge` 分支按上限扣费。
- 影响: 与 E-2 反向的风险——上游实际未扣费时**多扣用户**，损害用户信任且可能引发退款纠纷。
- 修复: `None` 时只释放 hold 并写入人工审核队列，不得按最大值扣费。

#### E-4. [P0] 数据库连接池未配置，高并发下耗尽
- 位置: `db.py:13`
- 触发: 默认 `pool_size=5 / max_overflow=10`，而 worker `max_jobs=64`。
- 影响: 连接等待超时，任务大面积失败；与 B 段中间件每请求开 session 叠加后风险放大。
- 修复: 显式配置 `pool_size=20 / max_overflow=30 / pool_timeout / pool_recycle=3600`。

#### E-5. [P0] 引擎 shutdown 未 dispose，连接泄漏
- 位置: `main.py:102-104`
- 触发: worker 关闭时 `_cleanup_resources` 未释放引擎连接。
- 影响: 数据库端堆积大量 IDLE 连接，反复重启后耗尽 PG 连接上限。
- 修复: `_cleanup_resources` 中添加 `await engine.dispose()`。

#### E-6. [P0] 多处事务未使用 `session.begin()`，事务边界依赖 autobegin
- 位置: `byok_runtime.py:257-269`、`outbox/staging.py:43-51`、`reconciliation/coordinator.py:59-71`
- 触发: 未显式开启事务，行为依赖 SQLAlchemy autobegin 配置。
- 影响: 版本升级或配置变更时事务边界静默改变，可能造成部分提交/数据不一致。
- 修复: 统一改为 `async with session.begin():`。

#### E-7. [P0] 事务 rollback 后未抛异常，错误被吞没
- 位置: `storyboard_assembly.py:205-206, 249-250`
- 触发: rollback 后直接 `return None`。
- 影响: 调用方无法区分「未抢到锁」与「数据库错误」，任务静默失败且不重试，用户侧表现为永久卡住。
- 修复: rollback 后抛 `ClaimFailedError` 或至少记录 warning 并区分返回值。

#### E-8. [P1] 视频 submit 超时未区分结果可知性
- 位置: `upstream_parts/adapters.py:82-127`
- 触发: httpx timeout 直接上抛，非幂等 provider 标记 `SUBMIT_UNKNOWN`。
- 影响: 若上游已受理并扣费，reconciliation 无法凭 task_id 恢复，形成计费黑洞。
- 修复: 区分连接超时（未送达，可安全 release）与读取超时（已送达，需 settle/对账）。

#### E-9. [P1] 图片 direct request 超时标记 `RESULT_UNKNOWN` 但不重试也不释放
- 位置: `upstream_parts/direct_requests.py:304-322`
- 触发: 超时抛 `DIRECT_IMAGE_RESULT_UNKNOWN`，hold 未释放。
- 影响: 上游已出图并扣费时，**用户既被扣费又拿不到结果**。
- 修复: 增加结果轮询补偿，或在确认未扣费后释放 hold。

#### E-10. [P1] provider 切换后 slot 未释放导致队列死锁
- 位置: `upstream_parts/submission.py:378-386, 869-877`
- 触发: 获取 slot 后、submit 前异常；`slot_provider_name` 已设但 `generation.provider_name` 未持久化。
- 影响: finally 释放时 provider 名不一致导致 slot 泄漏，该 provider 并发额度被永久占用，队列阻塞。
- 修复: 获取 slot 后立即持久化 provider 绑定，释放时以 slot 自身记录为准。

#### E-11. [P1] 图片 retry 未检查 provider 是否已更换，幂等键失效
- 位置: `upstream_parts/direct_requests.py:221-386`
- 触发: retry 时 provider 切换但 idempotency key 不变。
- 影响: 新 provider 无法用旧 key 去重，**可能重复扣费**。
- 修复: 幂等键纳入 provider 维度，或持久化 provider binding 后再重试。

#### E-12. [P1] video reconciliation 对 `submit_unknown` 超时项重投导致双重计费
- 位置: `reconciliation.py:41-42`
- 触发: 对账将超时的 `submit_unknown` 记录重新提交。
- 影响: 原请求若已被上游受理，重投造成**双份上游扣费**。
- 修复: 重投前先查询 provider 端任务状态，确认未受理才提交。

#### E-13. [P1] auto_title 缓存 check-then-set 非原子，并发重复扣费
- 位置: `auto_title.py:144-150`
- 触发: 多 worker 并发处理同一会话。
- 影响: 重复 enqueue 标题生成任务，重复消耗 token 并扣费。
- 修复: 改用 `SETNX` 原子占位。

#### E-14. [P1] storyboard_assembly 心跳计数无上界
- 位置: `storyboard_assembly.py:272-283`
- 触发: 长任务下 heartbeat 循环计数器无上限自增。
- 影响: 任务永不超时退出，占用 worker 槽位。
- 修复: 设置最大心跳次数，超限判定失败并释放资源。

#### E-15. [P1] canvas_execution_reconcile fingerprint 不匹配时缺清理
- 位置: `canvas_execution_reconcile.py:207, 138`
- 触发: fingerprint 与当前执行不一致。
- 影响: 旧执行记录残留形成脏状态，阻塞后续 canvas 执行。
- 修复: 不匹配时显式清理旧记录再继续。

#### E-16. [P1] Redis 连接未显式管理，缺重连机制
- 位置: `main.py:109`
- 触发: 网络抖动或 Redis 主从切换。
- 影响: 依赖 arq 自动清理，连接池参数未显式配置，断连后任务批量失败。
- 修复: 显式配置 `redis_settings`，关键操作前 ping 检查。

#### E-17. [P1] `owned_redis_lock` 释放失败缺上下文且调用方无感知
- 位置: `locks/owned_redis.py:120-131`
- 触发: release 异常被 catch 后仅 warning。
- 影响: 锁未释放却继续执行，只能等 TTL；日志缺 token 无法排查。
- 修复: 日志补充 token，关键锁释放失败时重试并告警。

#### E-18. [P1] HTTP 客户端退役未等待进行中的请求
- 位置: `upstream_parts/client_lifecycle.py:231-254`
- 触发: `_delayed_aclose` 超时后强制关闭。
- 影响: 中断长耗时请求（如 4K 图生成），造成结果丢失但上游已扣费。
- 修复: 超时提升至 30s，并记录活跃请求数用于观测。

#### E-19. [P1] Outbox 失败计数器无 TTL
- 位置: `outbox/publisher.py:130-134`
- 触发: 发布失败写入 Redis `fail_count`，永不过期。
- 影响: Redis 内存泄漏，长期运行后键空间膨胀。
- 修复: 设置 7 天 TTL。

#### E-20. [P1] 存储写入失败时临时文件清理异常被静默吞没
- 位置: `storage.py:102-108`
- 触发: `tmp.unlink()` 抛异常被捕获后不上抛。
- 影响: 临时文件堆积，最终耗尽 inode 或磁盘空间。
- 修复: 清理失败计入 metrics，并由定期任务扫描回收。

#### E-21. [P2] 视频 poll 返回相对路径时下载失败
- 位置: `upstream_parts/adapters.py:260-268`
- 触发: 第三方网关只返回 task_id 或相对路径，`_video_url()` 返回 None。
- 影响: `_absolute_url` 返回 None，download 抛异常，任务失败但上游已扣费。
- 修复: 增加基于 base_url 的 URL 拼接回退逻辑。

#### E-22. [P2] 图片下载未验证 redirect 目标，SSRF 绕过
- 位置: `upstream_parts/video_upstream.py:282-374`
- 触发: 上游返回 302 指向内网地址，或 HTTPS→HTTP 降级。
- 影响: 绕过初始 SSRF 校验访问内网服务。
- 修复: 逐跳校验 redirect target，禁止协议降级。

#### E-23. [P2] 视频 submit cache 无 TTL
- 位置: `upstream_parts/submission.py:467-482`（`video_submit_cache`）
- 触发: persist 反复失败时缓存条目永不过期。
- 影响: provider 端任务已 GC，poll 持续 404，任务卡死。
- 修复: 设置 1 小时 TTL。

#### E-24. [P2] account_limiter `daily_expire_at` 可能落在过去
- 位置: `account_limiter.py:171-175`
- 触发: 跨日边界计算过期时间。
- 影响: 限流窗口立即失效，配额形同虚设。
- 修复: 取 `max(now + ttl, 次日零点)`。

#### E-25. [P2] observability `safe_outcome` 返回空串
- 位置: `observability.py:323-327`
- 触发: outcome 为 None。
- 影响: 指标标签为空导致聚合错乱，监控失真。
- 修复: 回退为 `"unknown"`。

#### E-26. [P2] `runtime_settings.resolve_int` 未处理 None 默认值
- 位置: `runtime_settings.py:136-143`
- 触发: default 为 None 时 `int(None)` 抛异常。
- 影响: 配置读取崩溃导致任务启动失败。
- 修复: 显式 None 检查后短路返回。

#### E-27. [P2] sse_publish DLQ 去重每次扫描 200 行
- 位置: `sse_publish.py:809-839`
- 触发: 每次发布都线性扫描最近 200 条 DLQ。
- 影响: 高频事件下 O(n) 开销累积，SSE 推送延迟上升。
- 修复: 改用 Redis SET 成员判定去重。

#### E-28. [P2] `validate_image_job_base_url` 仅在 image_jobs_only 模式校验
- 位置: `config.py:126-134`
- 触发: 混合模式部署时配置非法 URL。
- 影响: 非法配置直到运行时才暴露，任务批量失败。
- 修复: 无条件校验。

#### E-29. [P2] runtime_settings 数据库查询无超时
- 位置: `runtime_settings.py:78-86`
- 触发: DB 慢查询或锁等待。
- 影响: SELECT 阻塞拖垮 worker 吞吐。
- 修复: `asyncio.wait_for` 设置 5s 超时。

#### E-30. [P2] context_summary PG advisory lock 无超时可能死锁
- 位置: `context_summary_parts/persistence.py:48-55`
- 触发: commit 失败时 advisory lock 残留。
- 影响: 后续 summary 任务永久无法获取锁。
- 修复: release 增加重试，connection 异常时 invalidate 连接。

#### E-31. [P2] 数据库未配置 `pool_recycle`，陈旧连接失效
- 位置: `db.py:13`
- 触发: 长连接被数据库侧或中间件超时断开。
- 影响: 复用陈旧连接触发异常，任务偶发失败。
- 修复: 配置 `pool_recycle=3600`。

#### E-32. [P3] 图片 generation settle 缺 provider 元信息
- 位置: `upstream_parts/billing.py:547-567`
- 触发: settle meta 未记录 `provider_name`。
- 影响: 计费争议时无法追溯实际服务方，审计困难。
- 修复: 补充 `provider_name / task_id / request_id`。

#### E-33. [P3] 视频 download 临时文件泄露风险
- 位置: `upstream_parts/video_upstream.py:314-369`
- 触发: `detect_video_media` 之后抛出非 `BaseException` 异常。
- 影响: 临时文件漏清理，磁盘缓慢占满。
- 修复: 改用 `NamedTemporaryFile` 或 finally 兜底清理。

#### E-34. [P3] 图片 completion settle 异常导致 hold 泄露
- 位置: `upstream_parts/billing.py:1178-1208`
- 触发: settle 内部异常导致任务放弃。
- 影响: hold 永不释放，用户余额被长期冻结。
- 修复: 增加异步清理任务扫描超时 hold。

#### E-35. [P3] `upstream_probe` 超时后未更新 provider 健康状态
- 位置: `upstream_probe.py:235-245`
- 触发: 探测超时。
- 影响: 继续路由到不健康 provider，失败率放大。
- 修复: 超时一律视为 unhealthy。

#### E-36. [P3] reconciliation cleanup 未处理 `status=None`
- 位置: `cleanup.py:56-57`
- 触发: 记录 status 为 None。
- 影响: 该类记录永不被清理，表持续膨胀。
- 修复: 将 None 纳入清理条件。

#### E-37. [P3] byok_retention 未处理配置读取失败
- 位置: `byok_retention.py:19-35`
- 触发: 读取保留策略异常。
- 影响: 任务崩溃中断 BYOK 密钥清理，敏感数据超期留存。
- 修复: 读取失败时回退默认保留期。

#### E-38. [P3] 其他健壮性项（合并）
- `billing.py:413-437` `flush_balance_cache` 未处理句柄为 None → 抛异常；修复：None 检查跳过。
- `main.py:96-99` startup 失败后 cleanup 中 ctx 缺 key 触发 `KeyError` → 用 `pop(key, None)` 与类型检查。
- `storage.py:29` `LINK_FALLBACK_MAX_ATTEMPTS=3` 硬编码 → 改为可配置。
- `db.py / main.py` 缺 DB 连接池与 Redis 监控指标 → 暴露 Prometheus 指标。
- 多处 session 依赖上下文管理器、极端异常下可能泄漏 → 关键路径 finally 显式 close。

> **关键风险总结**：E 区 7 个 P0 中有 3 个（E-1/E-2/E-3）集中于**视频计费与上游可知性判定**——当前实现在「上游是否已扣费」不确定时的默认行为不一致：`submit_unknown` 默认 release（平台吸收），而 `billable_hint=None` 默认按最大值扣费（多扣用户），两个方向的错误同时存在。另外 4 个 P0（E-4~E-7）是**基础设施层**的连接池与事务边界问题，属于全局性放大器。

> **已排除的误报**：adapter 层无重试（上层统一处理）✓、`dual_race` 未找到实现无法确认 ✓、video poll `usage_tokens` 已校验 ✓、`expire_on_commit=False` 合理 ✓、锁续期失败返回 None 为设计意图 ✓、commit 失败只 warn 是为保留根因 ✓。

---

### F. Core 计费与安全 — 发现 21 个问题（P0×6, P1×6, P2×6, P3×3）

本区经 **2 轮独立审计**（首轮 agent 遭 502 中断后重启，两轮均产出完整结果），下列为交叉合并去重后的清单。两轮独立命中同一缺陷的项已标注 **[双轮确认]**，可信度最高。

#### F-1. [P0] `settle` 可扣取超过 hold 的金额且透支保护可被绕过 **[双轮确认]**
- 位置: `packages/core/lumen_core/billing.py:664-748`（`allow_overdraw_micro` 路径）、`:667-681`（结算金额未受 hold 上限约束）
- 触发: 上游返回的 `actual_micro` 大于原 hold 金额；或调用方传入 `allow_overdraw_micro` 放宽校验。
- 影响: 用户被扣取超出预授权的金额，或余额被击穿为负；透支保护形同虚设。两轮独立审计均命中此路径。
- 修复: 结算金额强制 `min(actual, hold + 明确授权的浮动上限)`；`allow_overdraw_micro` 收窄为白名单调用方，并对超限差额单独告警。

#### F-2. [P0] `settle_video_cost` 向下取整造成系统性少收 **[双轮确认]**
- 位置: `packages/core/lumen_core/video_billing.py:242-274`
- 触发: 实际成本换算为 micro 单位时使用向下取整。
- 影响: 每笔交易少收不足 1 micro 的余额，**平台系统性吸收零头**，与「纯转嫁」原则相悖；高频调用下累积可观。
- 修复: 对平台应收金额使用向上取整（`math.ceil`），或统一采用 banker's rounding 并记录舍入方向。

#### F-3. [P0] `settle_video_cost` 允许 `actual_cost < 0`
- 位置: `packages/core/lumen_core/video_billing.py:242-274`
- 触发: 上游返回异常负值或解析错误产生负成本。
- 影响: 负成本反向**给用户充值**，形成免费刷余额漏洞。
- 修复: 入口处断言 `actual_cost >= 0`，负值拒绝并告警。

#### F-4. [P0] 定价计算存在整数溢出
- 位置: `packages/core/lumen_core/pricing.py:290-295`
- 触发: 超大 token 数或异常单价参与乘法运算。
- 影响: 溢出后金额回绕为极小值甚至负值，导致**近乎免费的生成**。
- 修复: 计算前校验各因子上界，使用 `Decimal` 或显式溢出检查。

#### F-5. [P0] 视频计费整数溢出可绕过成本上限
- 位置: `packages/core/lumen_core/video_billing.py:427-433`
- 触发: 构造极大时长/分辨率参数使乘积溢出。
- 影响: 溢出结果小于上限阈值从而**绕过 max cost 保护**，用户以极低价格获取昂贵视频。
- 修复: 上限校验前先做因子范围校验，并在溢出时直接拒绝请求。

#### F-6. [P0] HMAC 比较未使用常量时间函数
- 位置: `packages/core/lumen_core/billing.py:199-203`
- 触发: 攻击者对签名校验端点发起大量时序探测。
- 影响: 理论上可逐字节恢复签名，伪造计费引用凭证。
- 修复: 改用 `hmac.compare_digest`。

#### F-7. [P1] `lifetime_spend` 将透支额计入消费统计
- 位置: `packages/core/lumen_core/billing.py:678-680`
- 触发: 透支结算时 `lifetime_spend_micro += actual`，但透支部分用户并未实际支付。
- 影响: 消费统计虚高，影响等级/返利/风控判断；与 G-1 的平台吸收损失是同一问题的两面。
- 修复: 仅累计用户实际支付部分，透支额单列字段跟踪。

#### F-8. [P1] `pricing_fallback` 返回 None 导致计费链路崩溃
- 位置: `packages/core/lumen_core/pricing_fallback.py:132-144`
- 触发: 未匹配到任何 fallback 规则。
- 影响: 调用方拿到 None 后抛异常或按 0 计费，任务失败或**免费生成**。
- 修复: 提供保底价格常量，或显式抛出可识别异常阻断请求。

#### F-9. [P1] `estimate_image_cost` 可返回 0
- 位置: `packages/core/lumen_core/billing.py:446-491`
- 触发: 尺寸/模型参数未命中定价表。
- 影响: hold 金额为 0，用户在零预授权下发起生成，上游扣费后无从追缴。
- 修复: 估价为 0 时拒绝请求并告警，而非放行。

#### F-10. [P1] 视频结算 0 tokens 时抛异常
- 位置: `packages/core/lumen_core/video_billing.py:420-426`
- 触发: 上游返回 usage tokens 为 0（合法的短视频或计费口径差异）。
- 影响: 结算流程中断，hold 未释放也未结算，形成悬挂状态。
- 修复: 0 tokens 视为合法输入，按最低计费档处理。

#### F-11. [P1] 负数费率乘数检查不完整
- 位置: `packages/core/lumen_core/billing.py:440-445, 517-522`
- 触发: `billing_rate_multiplier_x10000` 被配置为负值。
- 影响: 负乘数使费用为负，**反向给用户充值**。
- 修复: 入口统一校验乘数 `> 0` 且在合理上界内。

#### F-12. [P1] `url_security` 存在 DNS rebinding 窗口
- 位置: `packages/core/lumen_core/url_security.py:245-291`
- 触发: 校验时解析到公网 IP，实际请求时 DNS 重新解析到内网。
- 影响: 绕过 SSRF 防护访问内网服务（与 E-22 的 redirect 绕过互补）。
- 修复: 校验后固定解析结果（pin IP）并以该 IP 发起连接，或使用统一的受控 resolver。

#### F-13. [P2] `image_signing` 校验存在时序侧信道
- 位置: `packages/core/lumen_core/image_signing.py:120-154`
- 触发: 对图片签名端点做时序分析。
- 影响: 可能伪造图片访问签名，越权读取他人图片。
- 修复: 使用 `hmac.compare_digest` 做常量时间比较。

#### F-14. [P2] 重试时 `generation_billing_ref_id` 不匹配
- 位置: `packages/core/lumen_core/billing.py:232-242`
- 触发: 生成任务重试时引用 ID 重新计算。
- 影响: 幂等键错位，可能重复扣费或退款找不到原交易。
- 修复: 引用 ID 绑定原始 generation 而非重试次数。

#### F-15. [P2] `billing_cache` MAX_WINDOW 可被绕过
- 位置: `packages/core/lumen_core/billing_cache.py:24`
- 触发: 调用方传入超出窗口的时间参数。
- 影响: 缓存一致性保证失效，读到陈旧余额并据此放行请求。
- 修复: 窗口值做上限钳制而非信任入参。

#### F-16. [P2] BYOK `hash_api_key` 使用固定盐
- 位置: `packages/core/lumen_core/byok.py:78-101`
- 触发: 所有用户 API key 共用同一盐值。
- 影响: 可预计算彩虹表，数据库泄露后加速还原用户上游密钥。
- 修复: 每条记录使用独立随机盐，或改用 KDF（如 scrypt/argon2）。

#### F-17. [P2] `wallet.version` 未用于乐观并发控制
- 位置: `packages/core/lumen_core/model_entities/billing_operations.py:56-58`
- 触发: 并发结算同一钱包。
- 影响: 版本字段存在却未参与 WHERE 条件，丢失更新风险（当前靠行锁兜底，但一旦有无锁路径即暴露）。
- 修复: 更新语句加入 `WHERE version = :expected` 并校验影响行数。

#### F-18. [P2] 余额缓存与写入未同步
- 位置: `packages/core/lumen_core/billing.py:633-698, 741-790`
- 触发: 结算写库成功但缓存刷新失败。
- 影响: 前端与限额判断读到陈旧余额，可能允许超额消费。
- 修复: 写库与失效缓存同事务边界处理，失败时强制缓存失效而非保留旧值。

#### F-19. [P2] 定价 priority 冲突时结果不确定
- 位置: `packages/core/lumen_core/pricing_resolver.py:76-78`
- 触发: 两条规则 priority 相同。
- 影响: 命中哪条依赖查询顺序，价格不可预测。
- 修复: 增加确定性次级排序键（如 id），并对重复 priority 做配置校验。

#### F-20. [P3] `compute_breakdown` 使用浮点运算
- 位置: `packages/core/lumen_core/pricing.py:277-345`
- 触发: 明细拆分涉及浮点乘除。
- 影响: 各明细项之和可能不等于总额，对账出现分币差异。
- 修复: 全程整数 micro 运算，最后一项用总额减去其余项。

#### F-21. [P3] 其他健壮性项（合并）
- `billing.py:71-89` `rmb_to_micro` 精度警告未上抛，转换误差静默 → 超阈值时告警。
- `url_security.py:71-105` IPv6 简写形式（如 `::ffff:127.0.0.1`）可能绕过内网判定 → 规范化后再比对。
- `video_billing.py:91-112` `video_pricing_variant` 未充分校验 resolution 取值 → 白名单枚举校验。

> **关键风险总结**：F 区是**资金安全的核心地带**，6 个 P0 呈现两类模式：一类是**取整与溢出**（F-2/F-4/F-5）——数值边界处理不当直接造成少收或绕过上限；另一类是**授权边界失效**（F-1/F-3/F-6）——结算金额不受 hold 约束、负值未拦截、签名比较不安全。F-2「向下取整系统性少收」与 G-1「透支平台吸收」共同构成对「视频计费纯转嫁」原则的持续性侵蚀。

---

### F 段补充说明

两轮审计对同一代码区独立作业，重合率约 25%（F-1、F-2 双轮命中），其余为互补发现——这印证了单轮审计对 core 计费这类高复杂度模块存在覆盖盲区，建议此类模块常态化采用多轮交叉审计。
### G. API 计费与生成端点 — 发现 2 个问题（P0×1, P1×1）

#### G-1. [P0] 零余额不可透支模式下平台吸收上游成本
- 位置: `packages/core/lumen_core/billing.py:673-678`
- 触发: `allow_negative_balance=false` + 用户余额不足覆盖 `actual_micro`（上游实际扣费）时，settle 强制 `balance_micro=0` 但 `lifetime_spend_micro += actual`。
- 影响: `overdraw_micro = -next_balance` 被记录在交易元数据，但用户未被实际扣取此部分，**平台吸收透支金额作为损失**。直接违反业务规则："只要上游 gateway 扣了费，用户就必须被扣费"。Worker 已在 billing.py:604-605,1096-1097 记录 `wallet_overdrawn_total` 指标并写 audit log (`event_type="wallet.overdrawn"`)，说明系统有意识记录此损失。
- 修复:
  - **选项 A（预防）**: 所有 hold 时强制 `balance >= hold_amount`，拒绝余额不足请求（当前 chat 场景已这样做，但图片/视频允许透支）。
  - **选项 B（事后追缴）**: 保持透支机制，但添加后台任务对 `overdraw_micro > 0` 的交易催缴或冻结账户。
  - 若透支是可接受运营成本（优化体验），则此项为**信息性发现**，需在文档明确此政策。

#### G-2. [P1] 图片生成重试预留金额遗漏费率乘数
- 位置: `apps/api/app/routes/tasks.py:154-187` (`_generation_retry_hold_micro`)
- 触发: 具有非默认 `billing_rate_multiplier_x10000` 的用户（溢价费率）重试图片生成任务。
- 影响:
  - 原始提交: 预留 = `base_cost × rate_multiplier` (message_submission.py:1059-1064)
  - 重试: 预留 = `base_cost`（未应用乘数）
  - Worker 结算: 实际扣费 = `base_cost × rate_multiplier` (worker/billing.py:478,497)
  - 费率 > 1.0 用户重试时预留不足，`allow_negative=false` 场景下平台吸收透支差额，违反对称性。
- 修复: 在 `_generation_retry_hold_micro` 中获取并应用 `rate_multiplier_x10000`，与原始提交对齐。

> **已排除的误报**（agent 核实非问题）: hold 与 generation 原子提交 ✓、视频重试无费率乘数不一致 ✓、幂等性防重复扣费 ✓、outbox 最终一致性 ✓、取消/重试 ref_id 匹配 ✓、视频取消 release 返回 None 是防御性检查 ✓。
---

### H. image-job 服务 — 发现 19 个问题（P0×4, P1×5, P2×5, P3×5）

`image-job` 为独立 sidecar 服务（自带 SQLite 持久化与队列 supervisor）。本区同样经 **2 轮独立审计**（8 条 + 16 条），下列为合并去重后的清单，**[双轮确认]** 表示两轮独立命中。

#### H-1. [P0] 上游成功但本地保存失败时不退款 **[双轮确认]**
- 位置: `upstream_runtime.py:237-242, 243-264`
- 触发: 上游返回 200 且已扣费，但本地保存图片失败（磁盘满、权限错误、写入异常）。
- 影响: **用户被扣费但拿不到图片**，且无退款路径——这是「纯转嫁」原则的反向失衡：平台不该吸收成本，但也不该在未交付时收费。
- 修复: 保存失败路径显式触发退款或标记为待人工处理，并将上游已扣费事实持久化以便对账。

#### H-2. [P0] 流式超时结果不确定时不退款
- 位置: `image_candidates.py:744-754`
- 触发: 流式响应中途超时，无法判定上游是否完成计费。
- 影响: hold 既未 settle 也未 release，用户余额被冻结；若上游已扣费则形成资金黑洞。
- 修复: 超时后主动查询上游最终状态；不可知时按 E 区统一策略处理并入对账队列。

#### H-3. [P0] 幂等性缺失导致自动重试重复扣费 **[双轮确认]**
- 位置: `upstream_runtime.py:340-354`、`job_persistence.py:533-554`
- 触发: worker 重启或异常后对同一 job 自动重试，未携带幂等键。
- 影响: 上游对每次请求独立计费，**用户被重复扣费**；幂等模式下重启还可能无限重试。
- 修复: 请求携带稳定幂等键（基于 job_id），并对重试次数设上限。

#### H-4. [P0] worker 异常时计费状态误报
- 位置: `job_service.py:299-307`
- 触发: worker 处理过程中抛出异常。
- 影响: 计费状态被错误标记，导致后续对账依据错误数据做退款或补扣决策。
- 修复: 异常路径下计费状态置为 `unknown` 而非猜测值，交由对账裁决。

#### H-5. [P1] 流式部分成功时返回不完整结果
- 位置: `image_candidates.py:854-863`、`:643-662`（非流式同类问题）
- 触发: 请求 n 张图，上游只返回部分即中断。
- 影响: 用户按 n 张被扣费却只得到部分图片，计费与交付不匹配。
- 修复: 按实际交付数量结算，或视为失败整体退款。

#### H-6. [P1] 并发重复处理同一 job_id
- 位置: `queue_supervisor.py:154-164`
- 触发: 多个 supervisor 实例或重启窗口内并发拉取同一任务。
- 影响: 同一任务被处理两次，**双重上游扣费**。
- 修复: 领取任务时使用原子 CAS 更新状态（`WHERE status='pending'`）并校验影响行数。

#### H-7. [P1] worker 重启后 running 任务状态不一致
- 位置: `job_persistence.py:531-596`
- 触发: worker 崩溃时任务停留在 running。
- 影响: 任务永久悬挂，既不重试也不失败，用户 hold 长期冻结。
- 修复: 启动时扫描 running 任务，依据心跳时间戳判定超时并恢复或失败。

#### H-8. [P1] 图片下载重定向可导致无限循环
- 位置: `image_candidates.py:310-350`
- 触发: 上游返回相互指向的重定向链。
- 影响: 请求线程被占满，服务不可用。
- 修复: 限制最大重定向次数（如 5 次）。

#### H-9. [P1] 上游 200 但无图片时计费对称性缺失
- 位置: `upstream_runtime.py:250-263`
- 触发: 上游返回成功状态码但响应体不含任何图片。
- 影响: 按成功结算但无交付物，用户付费却无结果。
- 修复: 校验图片数量为 0 时按失败处理并退款。

#### H-10. [P2] 空 JSON body 未正确处理
- 位置: `app_factory.py:102-108`
- 触发: 客户端发送空请求体。
- 影响: 解析异常返回 500 而非 400，掩盖真实错误。
- 修复: 显式校验并返回 400。

#### H-11. [P2] SQLite `busy_timeout` 设置不足
- 位置: `job_persistence.py:67`
- 触发: 并发写入争抢锁。
- 影响: `database is locked` 错误导致任务失败。
- 修复: 提升 busy_timeout 至数秒，并启用 WAL 模式。

#### H-12. [P2] 大文件原子写可能耗尽 `/tmp`
- 位置: `image_artifacts.py:129-137`
- 触发: 批量大尺寸图片同时写入临时目录。
- 影响: `/tmp` 空间耗尽，所有写入失败（并触发 H-1 的扣费不退款链路）。
- 修复: 临时文件写入目标同分区目录，并增加空间预检。

#### H-13. [P2] Pillow 校验可被绕过
- 位置: `image_artifacts.py:65-105`
- 触发: 构造能通过 `verify()` 但实际为恶意载荷的文件。
- 影响: 恶意文件进入存储并可能被下游解析。
- 修复: `verify()` 后重新 `open()` 并强制重编码为标准格式。

#### H-14. [P2] SHA256 去重可能误判
- 位置: `image_artifacts.py:178-181`
- 触发: 不同任务生成完全相同的图片内容。
- 影响: 去重逻辑可能错误关联到他人任务的产物，或导致计费与产物归属不一致。
- 修复: 去重键纳入 job/owner 维度。

#### H-15. [P3] 解压炸弹防护缺失
- 位置: `filesystem_artifacts.py:51-79`
- 触发: 上游返回高压缩比的恶意归档/图片。
- 影响: 解压耗尽内存或磁盘。
- 修复: 限制解压后总大小与单文件大小。

#### H-16. [P3] HTTP 连接池泄漏
- 位置: `http_upstream.py:42-46`
- 触发: 客户端未在生命周期结束时关闭。
- 影响: 连接数缓慢增长，长期运行后耗尽文件描述符。
- 修复: 纳入应用 lifespan 统一关闭。

#### H-17. [P3] 临时目录缺兜底清理
- 位置: `job_persistence.py:795-809`
- 触发: 异常路径跳过清理。
- 影响: 临时目录堆积占满磁盘。
- 修复: 增加定期扫描清理过期临时目录的后台任务。

#### H-18. [P3] 日志可能记录敏感信息
- 位置: `upstream_runtime.py:356-365`
- 触发: 异常时记录完整请求上下文。
- 影响: 上游 API key 或用户提示词进入日志。
- 修复: 记录前做字段脱敏。

#### H-19. [P3] 可观测性缺口（合并）
- 缺 `request_id` 贯穿链路，跨服务问题无法关联追踪 → 透传并记录 request_id。
- `runtime.py:69-84` Metrics 仅有基础指标，缺业务指标（成功率、上游耗时、退款次数）→ 补充业务维度指标。
- `runtime_config.py` 存在死代码 → 清理。

> **关键风险总结**：H 区 4 个 P0 全部指向同一根因——**image-job 作为独立 sidecar，其本地失败与上游计费之间缺少补偿事务**。上游扣费成功而本地保存/交付失败时（H-1、H-2、H-9），既无退款也无对账登记；同时缺乏幂等键使重试放大为重复扣费（H-3）。该服务需要一个与主 worker 对齐的 settle/release 状态机。

---
### I. Web 交互组件 — 发现 8 个问题（P0×1, P1×2, P2×3, P3×2）

#### I-1. [P0] Storyboard 全部 mutation 缺 onError，失败静默（含计费操作）
- 位置: `apps/web/src/components/ui/projects/storyboard/StoryboardPages.tsx:999-1024, 1032-1123, 1139-1164, 1180-1216`
- 触发: generate / approve / remove / rebuild / create / patch / submit / assemble 等 mutation 均未设置 `onError`，且无全局兜底。
- 影响: 生成、装配等**会扣费的操作**失败后前端无任何提示，用户以为未执行会重复点击，二次扣费；文件内已定义 `notifyStoryboardError` 辅助函数却从未被调用。违反计费透明原则。
- 修复: 为每个 mutation 补 `onError` 调用 `notifyStoryboardError`；对计费类操作失败必须显式提示并阻止静默重试。

#### I-2. [P1] ConfirmDialog 无防重复点击
- 位置: `apps/web/src/components/ui/primitives/ConfirmDialog.tsx:56-58, 138-147`
- 触发: 确认按钮点击后未禁用，异步 onConfirm 未完成前可连点。
- 影响: 危险/计费类确认（删除、提交）可被触发多次。
- 修复: 点击后立即置 pending 态禁用按钮，onConfirm 结束再恢复。

#### I-3. [P1] Storyboard seed 校验仅在前端
- 位置: `apps/web/src/components/ui/projects/storyboard/StoryboardPages.tsx:112-121, 756-771`
- 触发: seed 范围/类型仅前端校验，绕过前端可提交非法值。
- 影响: 非法 seed 直达后端，可能触发生成异常或不可预期计费。
- 修复: 后端同步校验 seed，前端仅作体验提示。

#### I-4. [P2] MaskBoard exportMask 逐像素 16M 次循环卡顿
- 位置: `apps/web/src/components/ui/inpaint/MaskBoard.tsx:808-820`
- 触发: 大图导出遮罩时对 4096×4096 级别逐像素循环。
- 影响: 主线程长时间阻塞，UI 冻结。
- 修复: 用 typed array 批处理或 OffscreenCanvas/Worker。

#### I-5. [P2] RangeField 仅 onPointerUp 提交，触屏变更可能丢失
- 位置: `apps/web/src/components/ui/canvas/CanvasNodeConfigFields.tsx:106-153`
- 触发: 值在 pointerup 才提交，触屏 pointercancel 场景不触发。
- 影响: 用户调整的参数未保存却无感知。
- 修复: 补 onPointerCancel/onBlur 兜底提交。

#### I-6. [P2] CanvasImageAssetDropZone 上传中切换 nodeId 覆盖错误节点
- 位置: `apps/web/src/components/ui/canvas/nodes/CanvasImageAssetDropZone.tsx:68-122`
- 触发: 上传异步进行中组件 nodeId 变更，回调闭包捕获旧/新 id 不一致。
- 影响: 上传结果写入错误节点，资产错乱。
- 修复: 用 ref 锁定发起时的 nodeId，或在回调中校验 id 一致性。

#### I-7. [P3] ModelLibraryReferenceUploader 失败后清空 input.value 无法重试同文件
- 位置: `apps/web/src/components/ui/projects/library/ModelLibraryReferenceUploader.tsx:41-72`
- 触发: 上传失败后清空 `input.value`，再次选同一文件不触发 change。
- 影响: 用户必须换文件或改名才能重试。
- 修复: 失败时保留 value，或提供显式重试入口。

#### I-8. [P3] MaskBoard retryTimerRef 清理（基本正常，建议加固）
- 位置: `apps/web/src/components/ui/inpaint/MaskBoard.tsx:349-383`
- 说明: 卸载清理路径基本正确，仅建议在所有早退分支统一 clear，防边界泄漏。

---

### J. tgbot 与脚本/部署 — 发现 7 个问题（P1×1, P2×1, P3×5）

#### J-1. [P1] redo 丢弃原生成参考图，img2img 退化为 text2img
- 位置: `apps/tgbot/app/handlers/actions.py:29-42, 67`
- 触发: `_payload_from_gen` 重建 payload 时未带 `attachment_ids`；对照 `retry.py:57` 正确保留了 `input_image_ids`。
- 影响: 用户对图片点"重做"，实际丢掉参考图变成纯文生图，结果与预期严重不符，且照常扣费。
- 修复: `_payload_from_gen` 补齐 attachment/input_image ids，与 retry 路径对齐。

#### J-2. [P2] 菜单回调 `cfg:count:<非数字>` 触发未捕获 ValueError
- 位置: `apps/tgbot/app/handlers/menu.py:21-26, 78`
- 触发: `_coerce` 对回调参数直接 `int(value)` 无保护，构造非数字回调即抛异常。
- 影响: handler 崩溃，spinner 永不清除，用户界面卡死。
- 修复: `int()` 包裹 try/except 或先正则校验，非法值回退默认并清 spinner。

#### J-3. [P3] listener 误判"没有图片返回"并 mark_notified
- 位置: `apps/tgbot/app/listener.py:856-882, 968-975`
- 触发: 某些结果结构下图片列表解析为空即判定无图并标记已通知。
- 影响: 有图却漏发，且被标记已处理不再重试。
- 修复: 收紧空判定条件，区分"确无图"与"解析失败"。

#### J-4. [P3] listener 多图部分发送失败后 replay 重发已成功图
- 位置: `apps/tgbot/app/listener.py:937-970`
- 触发: 多图逐张发送，中途失败后重放从头开始。
- 影响: 已成功的图被重复发送给用户。
- 修复: 记录已发送索引，重试仅补发失败项。

#### J-5. [P3] restore.sh Redis 旧数据备份目录永不清理
- 位置: `scripts/restore.sh:543-580`
- 触发: 恢复前把旧 Redis 数据移到备份目录，但无清理策略。
- 影响: 磁盘随恢复次数累积占用。
- 修复: 加保留 N 份的轮转清理。

#### J-6. [P3] restore.sh Redis 先于 Postgres 切换导致短暂不一致
- 位置: `scripts/restore.sh:522-615`
- 触发: 恢复顺序 Redis 早于 Postgres。
- 影响: 切换窗口内缓存与库不一致。
- 修复: 调整顺序或恢复期间挂维护模式。

#### J-7. [P3] tgbot 未声明 prometheus-client 依赖
- 位置: `apps/tgbot/pyproject.toml:10-18`, `apps/tgbot/app/main.py:30`
- 触发: main.py 导入 prometheus_client，但 pyproject 未列该依赖。
- 影响: 干净环境安装后启动即 ImportError。
- 修复: 在 pyproject 补 prometheus-client。

> 误报排除（agent 已核实非问题）: wallet_audit.py 排序正确；update_runner/restore_runner 文件校验健全；idempotency_key 去重可防重复扣费；docker-compose 无硬编码密钥。

---

## 审计完成状态

### 第一轮
全部 10 个分区审计完成，共确认 **126 个问题**（P0×23 · P1×40 · P2×36 · P3×27）。

- 每项发现均带 `文件:行号`、触发场景、影响与修复方向。
- 各区末尾列出 agent 已核实排除的**误报**，避免后续重复排查。
- F、H 两区经两轮独立审计交叉验证；两轮重合率约 25%，其余为互补发现——这表明对 core 计费、image-job 这类高复杂度模块，单轮审计存在覆盖盲区，建议常态化多轮交叉审计。

### 第二轮
9 路 agent 全部完成，新增确认 **46 个问题**（P0×3 · P1×15 · P2×17 · P3×11），并完成对第一轮 23 个 P0 的逐条代码复核。

**两轮累计 172 个问题**（P0×26 · P1×55 · P2×53 · P3×38）。

第二轮的核心结论与第一轮不同：第一轮回答的是"有哪些 bug"，第二轮回答的是**"为什么这些 bug 不会被修复"**——CI 从不执行治理 gate（`ci.yml:69-70`）、image-job 完全不在扫描范围（`check_complexity.py:19-23`）、baseline 是只增不减的棘轮。P0 修复率 8.7% 是这三者的直接结果。

因此建议的执行顺序是：**先修管道，再修代码**。在 CI 真正阻断违规之前，任何一轮审计结论都只会重复沉积。

---

*第一轮审计完成于 2026-07-26；第二轮审计完成于 2026-07-26。*
