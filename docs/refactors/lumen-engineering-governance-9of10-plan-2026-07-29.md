# Lumen 工程治理 9/10 执行计划

- Status: proposed
- Date: 2026-07-29
- Target baseline: `origin/main` at execution start
- Source audit:
[`lumen-architecture-governance-deep-audit-2026-07-29.md`](./lumen-architecture-governance-deep-audit-2026-07-29.md)

## 1. 计划目标

本计划的目标不是把检查项数量做多，而是把 Lumen 从当前约 **5/10** 的治理成熟度，
推进到可由机器证据证明的 **至少 9/10**。

9/10 的含义：

- 已知 P0 和 P1 缺陷为零。
- 资金、状态转换、恢复和发布都有可执行的不变量。
- 运行时资源均有明确 owner、构造点和关闭路径。
- 模块边界由默认禁止规则保护，不能靠目录漏配绕过。
- CI 覆盖产品代码、构建输入、迁移、治理基线和发布配置。
- 复杂度治理按职责和趋势衡量，不再依赖单一 1,500 行阈值。
- Web 私有状态按用户、tab 和连接生命周期隔离。
- 正式发布必须证明 main 来源、迁移安全、镜像完整、稳定 alias 和回滚能力。
- 分数由脚本根据仓库证据生成，不能手工填写。

9/10 不代表零风险。它代表关键风险已经机器化约束，剩余风险有 owner、有期限、
有预算，并且不能静默增长。

## 2. 达标判定模型

最终达标需要同时满足：

1. 所有一票否决条件均通过。
2. 加权治理分数不低于 9.0。
3. 完整 CI、正式 tag release 和稳定更新通道使用同一个 commit。
4. 连续两个治理波次没有新增未登记债务。

任何一项不满足，都不能宣称达到 9/10。

## 3. 一票否决条件

以下条件独立于加权分数。任一失败，最终状态直接判定为未达标。

### 3.1 缺陷与资金

- 已知 P0 数量必须为 0。
- 已知 P1 数量必须为 0。
- 不允许存在“上游可能已产生费用但本地 release”的自动路径。
- 接单后的异步任务必须可恢复，不能通过重新提交代替恢复。
- 所有取消结果必须区分提交前、已提交、已终态和结果未知。

### 3.2 Runtime ownership

- 未登记进程级可变 runtime 数量必须为 0。
- 每个 runtime 必须声明 owner、composition root、shutdown 和测试 reset 责任。
- Core 不得隐式持有未登记线程、子进程、连接池、锁或事件循环绑定资源。
- 禁止新增 `globals()` service locator 和 `ContextVar[Any]` 动态 facade。

### 3.3 CI 和治理基线

- production/build/config changed file 的 unmatched 数量必须为 0。
- architecture、complexity、runtime、facade 等基线只允许相对 merge-base 收缩。
- 修改治理脚本或 baseline 必须运行 full mandatory suite。
- 禁止通过 inline disable、注释、死代码或修改 baseline 让门禁假通过。

### 3.4 发布、迁移和更新

- release tag commit 必须是 `origin/main` 的祖先或与 main HEAD 完全一致。
- Alembic breaking lint 必须覆盖 PR、main 和 tag release。
- stable alias promotion 必须具备原子写入或完整回滚语义。
- updater 只能在最终 health proof 成功后进入 committed。
- 无 migration 的更新失败必须能够自动恢复旧 release。

### 3.5 Web 数据隔离

- `auth_invalidated` 必须使 session 进入 unauthorized 并清理私有状态。
- 每个 tab 必须独立完成本地 snapshot recovery。
- 所有用户私有 query、store、timer、selection 和 realtime channel 必须带用户作用域。
- 账号变化后不得继续访问旧用户 task id、conversation id 或 asset id。

## 4. 加权评分

评分范围为 0-10，按机器证据计算。

| 维度 | 权重 | 当前估值 | 目标 | 目标加权贡献 |
| --- | ---: | ---: | ---: | ---: |
| 资金与异步执行正确性 | 15% | 3.0 | 9.5 | 1.425 |
| Runtime ownership 与生命周期 | 12% | 3.0 | 9.0 | 1.080 |
| 模块边界与语义拆分 | 12% | 5.0 | 9.0 | 1.080 |
| CI、测试与门禁不可绕过性 | 12% | 7.0 | 9.5 | 1.140 |
| 发布、更新与回滚 | 10% | 5.0 | 9.5 | 0.950 |
| Web 状态隔离与实时恢复 | 10% | 4.0 | 9.0 | 0.900 |
| 数据、迁移与存储一致性 | 8% | 6.0 | 9.0 | 0.720 |
| 可观测性与故障恢复 | 8% | 6.0 | 9.0 | 0.720 |
| 安全与供应链 | 6% | 7.0 | 9.0 | 0.540 |
| 死代码、文档与债务闭环 | 7% | 6.0 | 9.0 | 0.630 |
| **合计** | **100%** | **约 4.95** | **约 9.19** | **9.185** |

目标预留约 0.19 分缓冲，避免某个维度刚好贴线时仍被误判为达标。

## 5. 评分证据设计

新增统一评分入口：

```text
scripts/governance_score.py
```

建议输出：

```text
docs/refactors/governance-score.json
docs/refactors/governance-score.md
```

评分脚本只读取机器生成证据：

- architecture graph 和 baseline diff。
- complexity metrics 和 per-file ratchet。
- runtime-state inventory。
- facade inventory 和生产调用者数量。
- test manifest coverage。
- CI required checks。
- migration lint。
- release/update fault tests。
- known-defect registry。
- dead-code reachability report。
- documentation freshness checks。

评分脚本不得：

- 接受任意手工 `score=10` 输入。
- 因文件不存在而默认满分。
- 将未执行测试视为通过。
- 将 unmatched 或 unknown 状态视为无问题。

任何证据缺失必须按失败或零分处理。

## 6. 治理控制面

### 6.1 新增声明式治理文件

计划新增：

```text
scripts/architecture-layers.toml
docs/refactors/module-ownership.json
docs/refactors/governance-exceptions.json
docs/refactors/known-defects.json
docs/refactors/governance-score.json
```

职责：

- `architecture-layers.toml`：声明 package、layer 和允许依赖方向。
- `module-ownership.json`：声明模块 owner、composition root 和生命周期。
- `governance-exceptions.json`：记录临时例外、owner、原因、预算和到期条件。
- `known-defects.json`：记录 P0-P3、状态、回归测试和修复 commit。
- `governance-score.json`：评分脚本的机器输出。

### 6.2 例外规则

任何治理例外必须包含：

```text
id
severity
owner
path
reason
introduced_at
retirement_condition
max_budget
expires_at
verification
```

规则：

- 例外必须有明确到期条件。
- P0/P1 不允许成为长期例外。
- 到期例外直接使 CI 失败。
- 同一 PR 不能同时新增债务并提高对应预算。
- 例外数量和预算只能相对 merge-base 下降。

### 6.3 防刷分规则

- 所有治理结果与 merge-base 比较。
- baseline update 不能掩盖新增 finding。
- 文件重命名后沿用原 ratchet，不得重置预算。
- 拆成多个文件后，聚合职责数和依赖扇出仍需下降。
- 删除测试不能提高覆盖得分。
- 跳过测试、未运行和 unknown 均按失败处理。
- 文档声明不作为完成证据，必须引用测试、CI run 或发布 artifact。

## 7. 执行所有权

所有工作遵循互斥写范围。共享根、CI、锁文件和最终集成由 Lead 独占。

### Lead 独占

- `.github/**`
- 根目录 Docker/Compose 文件
- `scripts/test-manifest.toml`
- `scripts/test_impact.py`
- `scripts/run_test_plan.py`
- `scripts/governance_score.py`
- 所有 governance baseline/ledger
- `pyproject.toml`
- `uv.lock`
- `apps/web/package-lock.json`
- 版本同步、最终合并、正式发布

### Track A：Worker 和 image-job

- `apps/worker/app/upstream_parts/**`
- `apps/worker/app/upstream_clients/**`
- `apps/worker/app/tasks/generation_parts/**`
- `image-job/image_job/**`
- 对应测试

### Track B：API、Billing 和 Canvas

- `apps/api/app/services/task_submission.py`
- `apps/api/app/services/video/**`
- `apps/api/app/routes/billing.py`
- `apps/api/app/routes/billing_parts/**`
- 对应测试

### Track C：Web realtime 和用户状态

- `apps/web/src/features/realtime/**`
- `apps/web/src/components/useIdentityRevalidation.ts`
- `apps/web/src/lib/runtimeResilience.ts`
- 对应测试

### Track D：Web video 和 Canvas

- `apps/web/src/app/video/**`
- `apps/web/src/components/ui/canvas/**`
- `apps/web/src/lib/canvas/**`
- 对应测试

### Track E：Updater 和 release scripts

- `scripts/update/**`
- `scripts/promote_release_images.py`
- `scripts/release_manifest_guard.py`
- 对应测试

### Track F：死代码和 facade 退休

- 每波只领取 Lead 明确分配的互斥文件。
- 不直接修改共享 lockfile、CI 或治理 ledger。
- 依赖删除和 ledger 更新由 Lead 最终处理。

## 8. Wave E0：资金安全紧急修复

目标分数：约 `4.95 -> 5.8`。
阻塞关系：所有后续大规模拆分之前必须完成。

### 范围

- 修复 sidecar 接单后 poll timeout 的重新生成和 release。
- 修复 sidecar succeeded 后下载失败的重新生成和 release。
- 持久化 sidecar execution handle。
- 区分 submit、poll、deliver、cancel 和 billing 状态。

### 核心不变量

```text
accepted(job_id) => never submit another generation automatically
succeeded(job_id) => retry delivery only
cost_unknown => never release
cost_incurred => never retry generation
```

### 必需测试

- POST 返回 `job_id` 后 poll timeout。
- poll timeout 后原任务稍后成功。
- succeeded 后下载超时。
- succeeded 后结果 404、空 body、超限。
- Worker 在 POST 响应后立即 SIGKILL。
- Worker 重启后继续 poll 原 job。
- 最终 billing decision 不得 release。

### Exit gate

- P0-01、P0-02 关闭。
- 无第二次 POST。
- 无错误 release。
- fault test 和账本证据进入 CI。

## 9. Wave 0：治理测量基础

目标分数：约 `5.8 -> 6.3`。
Lead 独占共享治理文件。

### 范围

- 实现 `governance_score.py`。
- 建立 known defects、ownership 和 exception registry。
- 所有 baseline 加入 merge-base 单调性检查。
- 修正 image-job production root。
- 将 Docker/Compose/env/build 文件纳入 test manifest。

### Exit gate

- 当前分数可由单一命令生成。
- 缺失证据不能获得分数。
- unmatched production/build/config 文件为零。
- 同一 PR 扩容 baseline 会失败。
- 治理文件变更自动触发 full mandatory suite。

## 10. Wave 1：取消、retention、Canvas 和状态 fencing

目标分数：约 `6.3 -> 7.0`。

### Track A

- 实现 sidecar DELETE。
- 明确 queued/running/terminal 取消结果。
- retention 只处理终态。
- active stale policy 独立。
- 所有终态写入使用 attempt/execution token fencing。
- sidecar 暂时强制 `n=1`，直到统一多图交付。

### Track B

- Canvas 视频提交改用 `VideoSubmissionContext`。
- 测试使用真实签名或 autospec。
- 增加 Canvas video service-level 和 transaction 测试。

### Exit gate

- P1-01、P1-02、P1-03、P1-04 关闭。
- 取消失败不能静默 release。
- retention 不处理 queued/running。
- stale worker 不能覆盖新状态。
- Canvas 视频节点真实提交成功。

## 11. Wave 2：Web 会话隔离和实时恢复

目标分数：约 `7.0 -> 7.7`。

### Track C

- follower 在 recovery complete 前执行本地 snapshot。
- `auth_invalidated` 同时更新 realtime 和 session。
- 清理用户私有 query/store。
- 首次连接 snapshot 使用可追踪 AbortController。
- snapshot 与连接 generation 绑定，旧恢复结果不得覆盖新连接。

### Track D

- Video feed 按 user id 建立 scope。
- 账号变化时 abort requests、清 timers、清 local items 和 selection。
- realtime channels 从当前 user scope 的 active items 生成。
- Canvas stale upload 使用统一 cleanup contract。

### 核心不变量

```text
recovery_complete(tab) => local snapshot completed(tab)
auth_invalidated => session == unauthorized
user_changed => no old-user private state remains
```

### Exit gate

- P1-05、P1-06、P1-07 关闭。
- 两标签 replay gap 测试通过。
- 多标签 cookie 切换测试通过。
- 旧用户 task/channel/request 数量为零。

## 12. Wave 3：CI、发布、迁移和 updater

目标分数：约 `7.7 -> 8.3`。
Lead 与 Track E 串行集成，禁止并行修改 workflow 和 shared scripts。

### CI

- Dockerfile、Compose、env、build context 进入 full mandatory。
- generic full suite 与 focused subsets 不再并发重复。
- `--rerun-failed` 绑定 plan digest、base、head 和 command set。
- 新增或未执行命令不能被视为 rerun 成功。

### Migration

- Alembic breaking lint 进入普通 CI。
- Docker Release quality gate 也必须执行。
- migration policy 测试执行 AST/操作语义，不只检查字符串。

### Release

- tag 必须来自 main。
- first-major alias 支持 rollback-to-absence 或明确删除恢复。
- GitHub Release 在 stable alias 成功后创建，或使用 draft/finalize 两阶段。
- Release API 完整分页。
- Node 22 在第一次 Web gate 之前配置，去除重复 Web build。

### Updater

- committed 移至最终 health proof 后。
- 无 migration 失败自动回滚。
- 有 migration 时根据 restore boundary 决定自动或人工恢复。
- `manual_required` 必须保留完整现场和明确恢复命令。

### Exit gate

- P1-08、P1-09、P1-10、P1-11 关闭。
- 非 main tag 发布测试失败。
- destructive migration 三条路径均失败。
- first-major alias fault test 通过。
- updater health failure 自动恢复测试通过。

## 13. Wave 4：Runtime ownership 和动态 facade 清理

目标分数：约 `8.3 -> 8.65`。

### Runtime scanner

- 扫描 API、Worker、Core、TgBot、image-job。
- 检测所有顶层构造实例。
- 分析实例字段中的锁、线程、子进程、连接池、cache 和 event-loop 资源。
- 未登记实例直接失败。

### 已知目标

- `_SSH_TUNNEL_RUNTIME`
- `_VIDEO_TRANSCODE_RUNTIME`
- `_TOKEN_COUNTER_RUNTIME`
- `_ADAPTER_RUNTIME_PORT`
- `_BILLING_RUNTIME`

### Billing

- 建立 typed `BillingQueries/BillingCommands/BillingServices`。
- 由 application runtime 构造。
- 删除 `globals()` 和 `ContextVar[Any]`。
- route parts 不再访问 facade 私有符号。

### Exit gate

- 未登记 runtime 数量为零。
- Billing runtime cycle 消失。
- 所有资源有 startup、shutdown 和 test reset。
- Core runtime 不依赖 app 生命周期猜测。

## 14. Wave 5：模块语义拆分

目标分数：约 `8.65 -> 8.95`。

拆分不是按行数平均切块，而是按状态、事务和业务责任建立边界。

### 14.1 Storyboards

拆分：

```text
transport
queries
commands
submission
assembly
publish
```

验收：

- route 只做输入输出转换。
- transaction 在 command service 收口。
- publish 与 DB commit 使用 outbox/明确一致性协议。
- 单文件不超过角色 ceiling。

### 14.2 Filesystem store

拆分：

```text
objects
staging
publish
variants
reconcile
retention
```

验收：

- 文件生命周期和数据库生命周期使用统一状态机。
- 并发 loser 文件可以回收。
- DB 命中但文件缺失可以自愈。

### 14.3 Web video

拆分：

```text
identity scope
history
active tasks
realtime
selection
submission
playback
```

验收：

- page 只做 composition。
- hook state 数、effect 数、依赖扇出进入预算。
- 用户作用域由类型和 query key policy 强制。

### 14.4 Canvas

- Inspector 只做表单 composition。
- Viewport 只做渲染和交互 orchestration。
- 上传、cleanup、selection 和 graph mutation 各有独立 service/hook。

### 14.5 Shell

拆分：

- `install.sh`
- `lumenctl.sh`
- `lib.sh`

要求：

- entrypoint 仅做参数解析和命令路由。
- update、release、backup、health、system service 各自独立。
- shell 文件进入同一 complexity 和 line ratchet。

### Role ceilings

最终建议 ceiling：

| 角色 | Hard ceiling | Warning |
| --- | ---: | ---: |
| Python route/controller | 800 | 500 |
| Python service/adapter | 1,000 | 700 |
| React page/component | 800 | 500 |
| React hook/controller | 600 | 350 |
| Shell entrypoint | 600 | 350 |
| 通用模块 | 1,000 | 700 |

临时例外必须进入 `governance-exceptions.json`，并设置更低的 per-file ratchet。

### Quantitative exit gate

- 不少于 1,000 行的生产文件从 78 降至不超过 20。
- 不少于 1,400 行的生产文件从 26 降至 0。
- 不存在 1,490-1,500 行贴线文件。
- route/page/controller 超过 800 行的文件为 0。
- Shell 三个巨型入口全部低于 hard ceiling。

## 15. Wave 6：死代码、facade 和依赖退休

目标分数：约 `8.95 -> 9.08`。

### 删除

- 高可信生产不可达模块。
- Worker 测试专用状态机原型。
- image-job 空包和未使用 Protocol。
- 前端旧路径 re-export。
- 未使用 GSAP 依赖。
- 未启用 Core Alembic helper。

### Facade 退休

- 自动发现全部 facade。
- 每个 facade 记录调用者数量和退休条件。
- 迁移 `lumen_core.models` 和 `lumen_core.schemas` 内部调用者。
- facade 只能 re-export 或 typed delegation，不得包含业务逻辑。

### Exit gate

- 高可信生产不可达模块为零。
- 未登记 facade 为零。
- 动态 facade 为零。
- 所有保留 compatibility 都有线上证据和到期条件。
- 生产镜像不再复制纯 CI/审计脚本。

## 16. Wave 7：故障注入、观测和 9/10 证明

目标分数：约 `9.08 -> 9.18`。

### Fault matrix

必须覆盖：

- API/Worker 在事务提交前后终止。
- Redis pipeline 部分成功。
- SSE 多连接重复事件。
- sidecar 接单后 Worker 终止。
- sidecar 成功后 artifact 下载失败。
- updater switch 后 health 失败。
- alias promotion 部分成功。
- migration 开始前、执行中和执行后失败。
- 用户在多 tab 切换账号。
- runtime shutdown 超时和资源泄漏。

### Observability

每个关键状态机至少暴露：

- transition counter
- invalid transition counter
- recovery counter
- uncertain outcome counter
- rollback counter
- resource leak/shutdown timeout counter

资金路径必须能够按 generation/ref 查询：

```text
hold
dispatch
sidecar job
provider response
delivery
settle/release
reconciliation
```

### 9/10 proof bundle

产出：

```text
docs/refactors/governance-score.json
docs/refactors/governance-score.md
docs/refactors/governance-proof-2026-XX-XX.md
```

proof 必须包含：

- 最终 commit。
- 所有 hard gate 结果。
- 加权分数。
- 全量测试结果。
- fault matrix 结果。
- Docker/Compose build proof。
- migration lint。
- release manifest。
- stable alias digest。
- updater/rollback proof。
- 已知剩余 P2/P3 和 owner。

## 17. 分数推进预期

| 阶段 | 预期分数 | 主要收益 |
| --- | ---: | --- |
| 当前 | 4.95 | 已有基础门禁和拆分账本 |
| Wave E0 | 5.80 | 消除 P0 资金风险 |
| Wave 0 | 6.30 | 建立可信评分和不可扩容基线 |
| Wave 1 | 7.00 | 取消、retention、Canvas 和 fencing |
| Wave 2 | 7.70 | Web 用户隔离和实时恢复 |
| Wave 3 | 8.30 | CI、迁移、发布和 updater 闭环 |
| Wave 4 | 8.65 | Runtime ownership 和 Billing 动态耦合清理 |
| Wave 5 | 8.95 | 主要超大模块完成语义拆分 |
| Wave 6 | 9.08 | 死代码和 facade 退休 |
| Wave 7 | 9.18 | 故障注入、观测和正式证明 |

分数是进度预测，不是完成证据。实际分数只能由评分脚本计算。

## 18. 每波通用交付要求

每个 wave 必须交付：

1. 变更前 baseline。
2. 明确 write scope 和 owner。
3. 不变量列表。
4. 新增或修改的回归测试。
5. fault/rollback 说明。
6. 治理指标变化。
7. 本地验证结果。
8. CI 结果。
9. 未完成项和剩余风险。

禁止：

- 为通过门禁扩大 baseline。
- 用宽松 mock 替代真实契约测试。
- 把旧逻辑复制到新模块后宣称完成拆分。
- 在同一文件中混合无关重构。
- 未经 Lead 修改共享 CI、ledger 或 lockfile。
- 在 review/fix 波次中自动发布。

## 19. 合并策略

- 每个 track 使用独立 branch/worktree。
- 文件写范围互斥。
- 每个 commit 只解决一个可验证责任。
- Lead 在集成前重新运行 impact plan。
- 共享文件由 Lead 最后修改。
- 先合并测试和治理门禁，再合并高风险行为变化。
- 资金、迁移、发布和 updater 变更必须双人交叉审查。
- 任何 P0/P1 回归立即停止后续 wave。

## 20. 正式完成与发布

达到 9/10 后，最终交付必须执行仓库正式发布流程：

1. 更新 `VERSION`。
2. 运行 `python3 scripts/version.py sync`。
3. 运行 `python3 scripts/version.py check`。
4. 提交代码、测试、治理证据和版本变更。
5. 推送 `main`。
6. 创建并推送匹配的 `vX.Y.Z` tag。
7. 等待 tag-triggered `Docker Release` 成功。
8. 验证 GitHub Release manifest。
9. 验证 `latest`、major、minor alias digest。
10. 验证稳定更新通道解析到同一 commit。

只有完成上述步骤，并且最终 governance proof 显示：

```text
hard_gates = passed
weighted_score >= 9.0
known_p0 = 0
known_p1 = 0
unmatched_production_files = 0
unowned_runtime_instances = 0
```

才能将工程治理状态标记为 9/10。
