# 计费系统彻底优化方案

> 状态: 计划稿 · 待评审
> 关联设计: `docs/billing-and-redemption-design.md`
> 影响范围: API (`apps/api`) · Web 管理后台 + 用户钱包页 (`apps/web`) · Worker 计费 hook (`apps/worker`) · 设置注册 (`packages/core/lumen_core/runtime_settings.py`)

## 0. 目标与非目标

**目标**
1. **可用性**: 全新部署的实例,管理员能够*仅通过 Admin Web UI* 把整个计费 + 兑换码流程从 0 跑通,无需登 SSH 改 env / 写 SQL。
2. **直观性**: 设置面板按"先开关 → 配价 → 发码 → 调账 → 监控"的顺序呈现,所有 raw JSON / micro 整数字段都隐藏在折叠区或以"元/张"等单位转换后呈现。
3. **可观测性**: 任一计费写入失败都能在面板里看到 (`audit_logs`、最近流水、孤儿 hold),不必去翻日志。
4. **正确性**: 双扣 / 漏扣 / hold 不释放 / 兑换码竞态等并发 bug 全部覆盖测试。

**非目标 (留 V2)**
- 第三方支付网关 (Stripe / 微信 / Telegram Stars 真钱支付) — 已有 `aiogram` 但不接入
- 多币种与汇率自动同步
- 邀请返利 / 分销
- 团队 / 子账号额度共享

## 1. 现状诊断 (bug 清单)

> 优先级 **P0** = 计费/兑换核心不可用; **P1** = 严重影响管理体验; **P2** = 抛光。

### 1.1 后端 / 配置 (`apps/api`, `packages/core`)

| # | 优先级 | 位置 | 现象 | 根因 |
|---|---|---|---|---|
| B1 | **P0** | `runtime_settings.py:283` + Admin Web | `billing.redemption_code_secret` 是 `sensitive=True` 字符串,**Admin Web 的 BillingPanel 完全没有暴露这一项**。新部署的实例创建/兑换任意码都返回 `503 REDEMPTION_SECRET_NOT_CONFIGURED` (`billing.py:118`),管理员必须去 SettingsPanel 在一堆 key 里手动找。 | UI 缺口 |
| B2 | **P0** | `apps/api/app/routes/billing.py:117-125` | secret 未配置时返回 503 而非 412 + 明确指引,前端只能展示"creation failed",管理员看不出要去哪里配。 | 错误消息不友好 |
| B3 | **P1** | `billing.py` `admin_list_wallets` | 用户搜索 `or_(User.email.ilike(...), User.id == q.strip())`,但 `q` 短于 3 字符时仍触发全表 ilike;并且**不返回最近一次充值/扣费时间**,管理员无法判断哪些账号是活跃的。 | 查询能力不足 |
| B4 | **P1** | `billing.py` `admin_list_redemption_codes` | 没有 `status=active` 默认; 列表不分页 (写死 `limit=100`,>100 张码就丢失); 不返回 `usable_count = max_redemptions - redeemed_count`。 | 列表能力不足 |
| B5 | **P0** | `billing.py:759-794` 创建兑换码 | **当前流程让兑换码事实上没法给用户**: 创建接口仅返回 `download_token` (不含明文 code); 前端立刻 `window.open(...)` 触发浏览器下载 CSV; 服务端用一次性 redis key 在下载后立即 `DEL` (`billing.py:815`); 缓存窗口 5 分钟。**任何一个环节出问题都永久失去明文** — 新标签被浏览器拦截 / 下载被取消 / CSV 文件丢失 / 想复制给用户但只有文件没有可点选文本 / 管理员误关标签。设计稿 §8.2 写"明文不直接回 JSON 避免日志泄漏"是合理的安全考量,但实现把这条原则做成了"管理员根本拿不到明文"。 | UX 设计错误 |
| B6 | **P2** | `billing.py:830` `admin_revoke_redemption_code` | 撤销已撤销的码静默 no-op,不返回 409,前端无法区分。 | 幂等语义 |
| B7 | **P2** | `billing.py:1066` `admin_list_wallet_transactions` | 没有 cursor 分页 (用户端 `/me/wallet/transactions` 有,管理员端反而没有)。 | 端点不一致 |
| B8 | **P0** | `apps/worker/app/billing.py` settle/release/charge | `idempotency_key` 是 `settle:{id}` / `release:{id}` 等。worker 重试或 fan-in 多张图时,如果 worker 主动重跑同一 generation,settle 已写过会按 IntegrityError 跳过 — 但当前**没有 audit-log 反查机制确认"已结算"**,只有 `unique_violation` 异常被吞掉,运维很难判断是"已经 settle 过"还是"settle 写入失败"。 | 幂等观测性 |
| B9 | **P1** | `system_settings` | `billing.image_size_thresholds` 与 `pricing_rules.key` 是两张表,改 thresholds 时 pricing_rules 的 `key` 不会跟着改,会出现"前端按 8k 算价、后端按 4k 扣"的不对齐。 | 数据漂移 |
| B10 | **P2** | `/admin/wallets/{user_id}/transactions` | 不带 ref 反查 (只能拉用户全量流水),不能筛 `kind=hold` 看是否有孤儿 hold。 | 排障不便 |

### 1.2 Admin Web UI (`apps/web/src/app/admin`)

| # | 优先级 | 位置 | 现象 |
|---|---|---|---|
| W1 | **P0** | `_panels/BillingPanel.tsx` | 顶部注释自称"Tab 1/2/3",但实际三个 Card 平铺,没有 Tabs。新人完全不知道"对话模型定价"在哪儿。 |
| W2 | **P0** | `BillingPanel.tsx:286-295` | `billing.enabled` 用 `<select>` 让管理员在 "0/关闭" 与 "1/开启" 之间选,而不是显式 Switch。设计稿明确要求 Switch。 |
| W3 | **P0** | `BillingPanel.tsx:295-322` | `image_size_thresholds JSON` 是 raw textarea,要管理员手写 JSON;同一档位的"像素下界"在尺寸定价 Card 里也有输入框,**两处不同步**,后保存覆盖前一次。 |
| W4 | **P0** | `BillingPanel.tsx` 模型定价区 | 唯一交互是"粘贴 YAML/JSON → 导入"。**没有当前已配模型的列表**,管理员看不见自己已经导入了什么、无法删除单行、无法改单个模型的价。 |
| W5 | **P0** | BillingPanel 全部 | 没有 redemption_code_secret 配置入口 (见 B1) |
| W6 | **P1** | `BillingPanel.tsx:266-269` 与 :298-303 | "USD→RMB" 输入框出现在两张 Card 里,**绑同一个 `rateDraft`** 但**分别由不同的保存按钮**写库 (`importMut` 重算价 vs `settingsMut` 存 setting)。在 A 处改了点 B 处的保存按钮会覆盖,反之亦然。 |
| W7 | **P1** | `_panels/RedemptionPanel.tsx:128-169` 创建发码区 | 5 列网格 (`md:grid-cols-[120px_120px_180px_1fr_auto]`) 在桌面挤,移动端塌;面额是裸 `<input>`,没有元/分提示,管理员可能填 "5000" 想要 50 元结果发了 5000 元码。 |
| W8 | **P0** | `RedemptionPanel.tsx:69-82` 创建后兜底 | 创建成功只 `window.open(csvUrl, "_blank", "noopener")` + 顶部 status 提示一行,**没有把明文 code 直接显示在管理后台让管理员选中复制**。配合 B5 的服务端一次性消费,导致"管理员根本无法把兑换码发给用户"。需要改为: 创建成功后弹一个 Modal,内含 (a) 明文 codes 列表 (一行一个,带"复制单条""复制全部 16x16"); (b) "下载 CSV""下载 TXT"按钮; (c) "复制到剪贴板"; (d) 5 分钟内可重打开。后端配合 B5 改造。 |
| W8b | **P1** | `RedemptionPanel.tsx` 兑换码表格 | 没有状态过滤 chips (后端 `status=active/revoked/expired/exhausted` 有,前端没用); 没有"复制前缀"按钮; 撤销没有二次确认; 没有"重新下载本批次 CSV" / "重新查看明文" (与 B5 配套)。 |
| W9 | **P0** | `RedemptionPanel.tsx:286-339` "用户钱包调账" | 搜出来用户列表后,点击一行只设 `adjustUserId / modeUserId / nextMode`,**不展示该用户当前余额、流水、最近兑换**。管理员只能盲调。 |
| W10 | **P1** | `RedemptionPanel.tsx:294-315` 用户搜索 | `<input>` 没在 `<form>` 内 — 按 Enter 不触发搜索;按 button 实质是 `walletsQ.refetch()`,但搜索其实是响应式靠 `queryKey: [..., walletQText, walletMode]` 自动跑的。按钮多余且让人误以为"必须点才能查"。 |
| W11 | **P1** | RedemptionPanel "切换账号模式" | 三个下拉 + 一个 user-id 输入框紧贴 "钱包调账",视觉上像是同一表单的延续,管理员容易把 mode_user_id 当成 adjust_user_id。 |
| W12 | **P2** | 全部 Admin 面板 | 没有"上次保存于 / 上次更新于"时间戳,管理员改了价之后不知道是否真的写库。 |
| W13 | **P2** | 全部 Admin 面板 | 顶部状态条 `{status && ...}` 是普通 div,不会自动消失,改多次会越堆越长。设计稿要求用 `toast`。 |

### 1.3 用户端 (`apps/web/src/app/me/wallet`)

| # | 优先级 | 位置 | 现象 |
|---|---|---|---|
| U1 | **P1** | `wallet/page.tsx:35-37` | `normalizeCode` 把输入裁到 16 个 alnum 字符 → 输出 `LMN-XXXX-XXXX-XXXX-XXXX` 即 4 段 16 字符。但 `placeholder` 也写 `LMN-XXXX-XXXX-XXXX-XXXX` 共 5 段。设计稿 §7.1 写"4 段 × 4 字符 = 16 位明文",**与代码一致**;但 design doc 文字示例 `LMN-XXXX-XXXX-XXXX` (3 段) 自相矛盾。需要 align。 |
| U2 | **P1** | `wallet/page.tsx:160` | 按钮 `disabled` 阈值 `< 19` 是基于 4-段格式;若改 3 段 (12 字符 body + LMN = 15) 就要同步。但当前的 generator/normalize/threshold 已自洽,**只是 design doc 的口径要校准**。 |
| U3 | **P1** | `wallet/page.tsx` | 没有"低余额"高亮 banner (只把数字标红,但页面顶上没有"余额不足,无法 4K 出图"提示); 没有"最近一次充值"展示; 没有"复制兑换记录" / 流水筛选 (kind=hold/settle/charge)。 |
| U4 | **P2** | `wallet/page.tsx:79-96` BYOK 分支 | 文案"费用由上游 API 账单结算",但**没有解释为什么会看到这个页面**(头部没有"为什么你的账号没有钱包"提示),BYOK 用户可能困惑。 |
| U5 | **P1** | 整个 app | 生图/对话发送前的"本次预计扣 ¥X.XX"提示**未实现**。设计稿 §9.1 明确要求,wallet 用户需要在 PromptComposer 看到本次大概扣多少。 |
| U6 | **P2** | header / topbar | 顶部导航**没有"余额胶囊"** (设计稿 §9.1 第一项)。wallet 用户只能进 `/me/wallet` 看余额。 |
| U7 | **P1** | `/me/redemptions` | 用户没有"我的兑换历史"页 (后端 `/me/redemptions` 已经实现,前端不展示)。 |

### 1.4 流程 / 跨层

| # | 优先级 | 现象 |
|---|---|---|
| X1 | **P0** | "新部署 → 跑通"目前**需要 7 步手动**: 设 secret → 设 enabled=1 → 设 rate → 创建 image_size 价格 → 设 thresholds → 导入 chat_model 价 → 调账测试。其中 secret 没 UI,thresholds 与 pricing.key 易漂移。需要做"一键引导向导" (Wizard) 或至少集中到一个 Onboarding Card。 |
| X2 | **P0** | 灰度开关 `billing.enabled=0` 时,wallet 用户兑换码**仍然成功**,余额会增加,但生图/对话不会扣 — 看起来一切正常,管理员关闭计费做测试时会留下"幽灵余额"。是否要让 enabled=0 也拒绝兑换? 决策点见 §4.4。 |
| X3 | **P1** | 没有"对账巡检"前端入口。脚本 `scripts/wallet_audit.py` 存在,但管理员不知道;设计稿 §14 提到"回放测试",生产应有一个"运行对账"按钮 + 异常列表面板。 |
| X4 | **P1** | 没有"孤儿 hold"清理 UI: worker crash → hold 不 release 时,管理员只能写 SQL。 |

## 2. 设计原则

1. **单一入口**: 计费 + 兑换码 + 用户余额管理在 Admin 后台合并为一个 **"计费"顶级 Tab**, 内含 4 个 sub-tab: `概览` / `定价` / `兑换码` / `用户钱包`。原 "计费" 与 "兑换码" 两个 Tab 取消。
2. **配置即文档**: 每个 setting 输入框旁边一行短描述 + 默认值 + "上次更新时间";隐藏 micro / JSON 等内部表达,只暴露元、张数、时长。
3. **写前预览**: 改价、发码、调账三个写操作,提交前都弹一个二次确认 + "影响范围预览" (例如"3 个 active 价格规则将更新,5 张待生效兑换码不受影响")。
4. **写后审计**: 写操作完成后:
   - 顶部 toast 短反馈
   - 当前 Card 显示"上次更新于 ..."
   - 跳转到对应 audit_logs 行的链接
5. **无 raw**: 不在 UI 出现 raw JSON、raw micro 整数、raw kind enum (例如把 `topup_redeem` 渲染成"兑换码充值")。
6. **可灰度回滚**: 所有改动落在 `billing.enabled=0` 仍然安全; 任一前端组件渲染失败不能阻塞其他 admin Tab。

## 3. 解决方案 — 后端

### 3.1 配置层 (`packages/core/lumen_core/runtime_settings.py`)

新增 / 调整:

- 新增 `billing.redemption_code_secret` 的 admin-UI 编辑路径 (现已有 setting 注册;问题在前端,详见 §4.2)。后端**不**改 spec。
- 新增 `billing.bootstrap_completed` (bool, internal, 初始 `false`): 由 admin 完成首次配置后写 `true`。前端"未完成"时在 BillingPanel 顶部展示 Wizard,完成后展示常规面板。
- 新增 `billing.show_estimate_in_composer` (bool, 默认 `true`): wallet 用户在 PromptComposer 是否显示"本次约扣"。

### 3.2 API 端点扩展 (`apps/api/app/routes/billing.py`)

| 改动 | 详情 |
|---|---|
| `GET /admin/billing/overview` (新) | 返回看板数据: `{wallet_total_balance_micro, holds_count, holds_micro, codes_active, codes_redeemed_24h, recent_audit_events[10]}`,供"概览"sub-tab。 |
| `GET /admin/wallets/{user_id}` (新) | 返回单用户完整画像: `{user_id, email, account_mode, wallet, last_topup_at, last_charge_at, transactions: top 20, redemptions: top 10}`。前端点击"用户搜索"行后展开。 |
| `GET /admin/wallets/{user_id}/transactions` (改) | 增加 cursor 分页参数 + `kind` 过滤;与 `/me/wallet/transactions` 对齐。 |
| `POST /admin/redemption_codes` (改) | 创建响应**新增 `plaintext_codes: string[]` 字段直接返回明文 code 列表**,前端可立即展示+复制。同时保留 `download_token` 用于 CSV 下载。理由: 当前"只回 token 不回明文"的设计假设 admin 一定走 CSV 下载,但实际管理员往往直接复制黏给单个用户。安全顾虑 (日志泄漏) 通过: (1) `apps/api/app/main.py` 已经做 access-log 不打 body; (2) 响应头加 `Cache-Control: no-store`; (3) 前端 Modal 关闭后立刻从 state 清空。 |
| `POST /admin/redemption_codes/batches/{batch_id}/redownload` (新) | 在 5 分钟窗口内允许同一 admin 再生成 download_token / 再次查看明文。需要在 `admin_create_redemption_codes` 改为**不立即 DEL** Redis 缓存,改为 token 多次使用、buffer 5 分钟 TTL 自然过期。每次重下载写 `audit_logs.event_type='redemption.batch.redownload'`。 |
| `POST /admin/redemption_codes/{code_id}:revoke` (改) | 二次撤销返回 `409 ALREADY_REVOKED` 而非 200 (B6)。 |
| `GET /admin/redemption_codes` (改) | 默认 `status=active`; 支持 cursor 分页; 返回 `usable_count`; `q` 接受前缀或 batch_id 后 4 位。 |
| `POST /admin/billing/bootstrap` (新) | 一次性请求: 设置 secret + enabled + 默认 thresholds + 默认 image_size 价格 + USD→RMB rate。幂等。 |
| `GET /admin/billing/audit?event_type=&limit=` (新) | `audit_logs` 中所有 `wallet.* / pricing.* / redemption.* / account.mode_change` 的拉取接口,供"概览"显示。 |

错误码补充:

```
REDEMPTION_SECRET_NOT_CONFIGURED  412   # B2: 从 503 改为 412 + UI 引导
ALREADY_REVOKED                   409   # B6
WALLET_HAS_ACTIVE_HOLDS           409   # 已有, 抛在 set_account_mode
BOOTSTRAP_INCOMPLETE              412   # /admin/redemption_codes/* 在未完成 bootstrap 时直接拒绝
```

### 3.3 兑换码 secret UI 路径

- 前端 BillingPanel 直接调 `/admin/settings` 单 key 更新 `billing.redemption_code_secret`。
- 在 `PUT /admin/settings` 写入该 sensitive key 时,后端额外写一条 `audit_logs.event_type='billing.secret.rotate'`,details 留 hash(secret) 前 8 位以便审计但不存明文。
- 旋转 secret 会让所有未兑换码失效 — 在前端二次确认中明确提示"将作废 N 张未兑换码"。后端会把未兑换码标记为 `revoked_at=now()` 便于后台审计; 用户输入旧明文码时,由于 HMAC secret 已变,通常仍会得到 `CODE_NOT_FOUND`。

### 3.4 Worker 计费幂等性 (B8)

- `apps/worker/app/billing.py` settle / release / charge: 捕获到 `IntegrityError(uq_wallet_tx_idemp)` 时,改为**主动 SELECT 已有流水**,把 `balance_after / hold_after` 拿出来,写一条 `audit_logs.event_type='wallet.{kind}.replay'`,然后视作"已结算"返回。当前是吞掉异常没记录。
- 增加 `tests/worker/test_billing_idempotency.py`: 模拟同一 generation 调 settle 两次,断言第二次 returns ok + 写 audit。

### 3.5 thresholds 与 pricing_rules 漂移 (B9)

- `PUT /admin/pricing` 在事务内同时:
  1. upsert `pricing_rules`
  2. 检查 `image_size_thresholds` 中是否每个 tier 都有对应的 `pricing_rules` 行 (scope=image_size)
  3. 若不一致,要求前端在请求体里附 `thresholds` 字段一起改;后端做原子写。
- `PUT /admin/settings billing.image_size_thresholds` 单独修改 thresholds 时,同样校验"每个 tier 都有 enabled pricing_rule",否则 422 `THRESHOLDS_PRICING_MISMATCH`。

## 4. 解决方案 — Admin Web UI

> 遵循 `docs/frontend-theme-dialog-standards.md`、`apps/web/CLAUDE.md`、`apps/web/AGENTS.md` (Next.js 文档与 deprecation 必读)。所有 UI 用语义 token,不硬编码 `bg-neutral-900` 等。

### 4.1 顶级结构

- `admin/page.tsx` 的 `TABS` 中:
  - 删除独立 "兑换码" tab
  - 把 "计费" tab 内部展开为 4 个 sub-tab,通过 `BillingPanel` 内部状态切换:
    - **`概览`** (默认): 看板 — 钱包总余额 / 活跃 hold / 24h 兑换 / 最近审计 / 健康检查 (secret 是否配、enabled 是否开、image_size 是否对齐)
    - **`定价`**: 尺寸定价 + 对话模型定价 + 全局开关
    - **`兑换码`**: 发码 / 列表 / 重下载 / 状态过滤
    - **`用户钱包`**: 搜用户 / 看详情 / 调账 / 切换 account_mode

### 4.2 概览 sub-tab (`OverviewSubpanel.tsx`,新建)

布局:

```
┌─ 健康检查 ──────────────────────────────────────────┐
│ ✓ 计费开关: 开启                                     │
│ ✓ USD→RMB: 1.0                                      │
│ ✗ 兑换码 secret: 未配置 → [立即配置]                  │
│ ⚠ 尺寸价格 4k 档 enabled=false → [去定价]            │
└──────────────────────────────────────────────────┘

┌─ 数据看板 ────────────────────────────────────────┐
│ 钱包总余额  ¥12,345.67    活跃 hold  3 笔 / ¥4.20  │
│ 24h 兑换    12 张 / ¥600  24h 扣费   ¥83.40        │
└──────────────────────────────────────────────────┘

┌─ 最近审计 (滚动列表 30 行) ────────────────────────┐
│ 2026-05-15 14:02  wallet.adjust.admin  user@x ...  │
│ 2026-05-15 13:55  redemption.create    batch ...   │
│ ...                                                 │
└──────────────────────────────────────────────────┘
```

健康检查项每条点击跳转到对应 sub-tab 并 scroll 到对应 Card; 没配 secret 时弹一个 inline 设置框,直接调 `PUT /admin/settings`。

### 4.3 定价 sub-tab (`PricingSubpanel.tsx`,接管原 `BillingPanel` 内容)

- **全局开关** 区域 (顶部):
  - `billing.enabled` 用 `<Switch>` (不是 select),旁边显示"开启后 wallet 用户生图/对话开始扣费"
  - `billing.usd_to_rmb_rate` 数字 input + "重算所有对话模型 RMB 价"按钮 (调用 `/admin/pricing/import_openai` 或新增 `/admin/pricing:recalc_rmb`)
  - `billing.low_balance_warn_micro` 在前端转成 ¥ 单位输入 ("低于 ¥2.00 时提示用户"),提交时再转 micro
  - `billing.allow_negative_balance` Switch,默认关
  - `billing.redemption_code_secret`: 当前状态显示"已配置 (sha256...0a3f)" 或"未配置";"修改"按钮弹对话框,要求二次输入 + "确认作废所有未兑换码"checkbox。

- **尺寸定价** 区域 (中部):
  - 表格列: `档位` / `像素下界` / `单价 (¥/张)` / `enabled` / `操作`
  - 内联编辑;"添加档位"在表格底部 + row 添加 → 直接 PUT
  - 删除档位时校验"此档位是否有未结算 generation"(后端 `/admin/pricing/{tier}:can_delete` 返回 `usage_24h`),弹确认
  - 保存按钮变 sticky bar, 显示"3 项待保存"

- **对话模型定价** 区域 (底部):
  - **新增表格视图**: 列 `模型` / `输入 ¥/1K` / `输出 ¥/1K` / `源 USD/1M` / `enabled` / `更新于` / `操作`
  - "导入"按钮打开抽屉(Drawer): YAML/JSON 粘贴 + USD→RMB 预览表格(显示导入后会变成哪些 RMB 价)
  - 内联编辑 RMB 价位
  - 删除单行 (调用 `PUT /admin/pricing` 把 `enabled=false`)

- **不再有** raw JSON thresholds 输入框;改 thresholds 走"尺寸定价"表格的"像素下界"列。后端配合 §3.5 原子写。

### 4.4 兑换码 sub-tab (`CodesSubpanel.tsx`)

- **创建发码** 卡片:
  - 单列垂直表单 (面额、数量、有效期、备注、每码最大兑换次数 max_redemptions),不再 5-column 挤
  - 面额输入前后缀显示 `¥ __ 元` + 实时显示"本批次总价值 ¥ N.NN"
  - 数量 > 200 时弹警告"将生成超过 200 张明文 code,确认?"
  - **修复 B5 / W8 (核心痛点 — 兑换码必须能复制给用户)**: 提交后**不再走 `window.open` 触发文件下载**,改为弹一个全屏 Modal `<NewCodesModal>`:

    ```
    ┌─ 已生成 10 张兑换码 ─────────────────────── × ┐
    │ 面额 ¥50  批次 01HX...                        │
    │                                                │
    │ [复制全部] [下载 CSV] [下载 TXT]               │
    │                                                │
    │ ┌────────────────────────────────────────┐    │
    │ │ LMN-A2C4-...-XXXX  [复制]              │    │
    │ │ LMN-B3D5-...-XXXX  [复制]              │    │
    │ │ ... (列表可滚动, 等宽字体, 单行可选中)   │    │
    │ └────────────────────────────────────────┘    │
    │                                                │
    │ ⓘ 关闭此窗口后, 5 分钟内可在列表"重新查看"     │
    │   按钮再次取回; 超过 5 分钟将永久失去明文。     │
    └────────────────────────────────────────────────┘
    ```

  - Modal 数据来源: 创建接口响应里直接带 `plaintext_codes` (后端改造见 §3.2)
  - 列表行的"操作"列增加 **"重新查看明文"** 按钮 (5 分钟内可点),调用 `POST /admin/redemption_codes/batches/{batch_id}/redownload`,重弹 Modal
  - 列表行的"操作"列另加 **"下载 CSV"** 按钮,直接走 token URL
  - Modal 关闭时清空 React state 的 plaintext_codes,避免内存里残留
  - 大量码 (count > 50) 时 Modal 列表虚拟滚动 + "搜索 code 前缀" 输入框,方便客服按用户找一张

- **列表** 区域:
  - 顶部状态过滤 chips: `全部 / 可兑 / 已兑完 / 撤销 / 过期`
  - 搜索框 (前缀或 batch_id) — 在 form,Enter 触发
  - 列: `前缀` / `面额` / `兑换 N/M` / `状态徽章` / `批次 (复制)` / `备注` / `创建时间` / `操作`
  - 操作: `复制前缀` / `撤销 (二次确认)` / `查看兑换记录 (Drawer 而非内联表格)` / `撤销整批`
  - cursor 分页 (后端 §3.2 已加)
  - 多选 + "批量撤销" (调用同接口循环 — V1 简化为前端 N 次请求,V2 后端 batch endpoint)

- **不在此 sub-tab 做**用户钱包调账。

### 4.5 用户钱包 sub-tab (`UserWalletsSubpanel.tsx`)

修复 W9 / W10 / W11:

布局:

```
┌─ 搜索 (form, Enter 触发) ──────────────────────────┐
│ [q: email/uuid______] [mode: wallet/byok/all] [搜] │
└──────────────────────────────────────────────────┘

┌─ 结果列表 (10/页) ─────────────────────────────────┐
│ user@x  wallet  ¥12.35   3 笔今日   [详情▼]        │
│ ...                                                │
└──────────────────────────────────────────────────┘

┌─ 详情 (展开) ─────────────────────────────────────┐
│ ┌ 概要 ─────────┐  ┌ 调账 ──────────────┐         │
│ │ 余额 ¥12.35   │  │ +/- 金额 [____]    │         │
│ │ 预扣 ¥0.00    │  │ 理由   [____]      │         │
│ │ 24h 充值 ¥50  │  │ [提交 (二次确认)]   │         │
│ │ 24h 扣费 ¥3   │  └────────────────────┘         │
│ │ mode wallet  │                                  │
│ │ [切换模式▼]   │                                  │
│ └──────────────┘                                  │
│                                                    │
│ 流水 (cursor 分页, 可按 kind 过滤)                  │
│ ──────────────────────────────────                 │
│ 14:02  兑换码充值  +¥50.00  余额 ¥62.35           │
│ 13:58  对话扣费   -¥0.12   余额 ¥12.35           │
│ ...                                                │
└──────────────────────────────────────────────────┘
```

详情区由 `GET /admin/wallets/{user_id}` (§3.2 新接口) 一次拉齐; 调账按钮二次确认,显示"`+5.00 RMB`,余额将变为 `¥17.35`"。

切换 account_mode 改为详情区内的下拉 + 浮层提示,不再裸露 user_id 输入框。

## 5. 解决方案 — 用户端

### 5.1 顶部余额胶囊 (`apps/web/src/components/ui/shell/*`)

在 `TopBar` 右侧 (头像左) 加 `<WalletPill>`:
- wallet 用户: 显示 `¥12.35`; 低于阈值标红 + 微抖动一次
- byok 用户: 不渲染
- billing.enabled=false: 不渲染 (公开 setting `/me/settings/billing-enabled-public` 暴露; 或随 `/me` 一起返回 `billing_enabled` 布尔)
- 点击跳 `/me/wallet`

### 5.2 钱包页改造 (`apps/web/src/app/me/wallet/page.tsx`)

- 顶部新增 banner: 余额 < 阈值时全宽红条 "余额不足,4K 图可能无法生成"
- 余额卡片: 显示 "24h 变化 +¥30.00 / -¥3.20" 微统计
- 兑换码 form: 失败时显示具体错误 (`CODE_EXPIRED` 红 / `CODE_REVOKED` 黄 / `CODE_NOT_FOUND` 灰)
- 流水: 加 kind 过滤 chips、cursor "加载更多"
- 新增 "我的兑换历史" 折叠卡片,使用 `GET /me/redemptions`

### 5.3 BYOK 文案 (U4)

页面顶部加一行小字: "你的账号由 BYOK 自助注册流程创建,所以费用直接由你在 OpenAI/Claude 等上游账单结算,Lumen 平台不收钱、不维护钱包。"

### 5.4 PromptComposer 预扣预览 (U5)

`apps/web/src/components/composer/PromptComposer.tsx`:
- wallet 用户在选完尺寸/张数后,旁边显示"本次约扣 ¥0.40" (用 `/me/pricing` 缓存的尺寸价 × n)
- 4K 单张超过 1 元时颜色变 amber
- 余额不足以发送时,发送按钮 disabled + 文案"余额不足,前往充值"

### 5.5 错误码本地化

`apps/web/src/lib/errors.ts` 新增映射:

```
INSUFFICIENT_BALANCE     -> "余额不足,请兑换充值或联系管理员"
WALLET_FROZEN            -> "钱包已冻结,请联系管理员"
NO_ACTIVE_API_KEY        -> "没有可用的 API Key,请到设置重新绑定"
ACCOUNT_MODE_FORBIDDEN   -> "当前账号类型不支持此操作"
ACCOUNT_NOT_WALLET       -> "目标账号不是 wallet 模式"
CODE_NOT_FOUND           -> "兑换码无效"
CODE_REVOKED             -> "兑换码已被撤销"
CODE_EXPIRED             -> "兑换码已过期"
CODE_EXHAUSTED           -> "兑换码已被兑完"
CODE_ALREADY_USED        -> "你已经兑换过此码"
PRICING_NOT_CONFIGURED   -> "管理员尚未配置该规则,请联系管理员"
REDEMPTION_SECRET_NOT_CONFIGURED -> "兑换码功能未配置,请联系管理员"
BOOTSTRAP_INCOMPLETE     -> "计费功能未初始化,请联系管理员"
```

## 6. 决策点 (需要确认后再开工)

1. **`billing.enabled=0` 时是否允许兑换?**
   - 选项 A: 仍允许,理由"管理员可以在灰度前预先发码";现有行为。
   - 选项 B: 拒绝,理由"避免幽灵余额"。
   - **推荐 A**,但在 BillingPanel 概览健康检查里高亮"已发 N 张码但计费未开启"。

2. **兑换码明文获取 (B5 / W8 — 核心痛点)**
   - 选项 A: **创建响应直接返回 `plaintext_codes` 数组 + Redis 保留 5 分钟可重取**。安全顾虑通过响应头 `Cache-Control: no-store` + access-log 不打 body + 前端关 Modal 清 state 缓解。**推荐**。
   - 选项 B: 保留现行 token-only,但允许多次下载 (token 不再单次消费)。
   - 选项 C: 持久化明文到 DB 单独表,加 admin password 二次解锁。最重,V2 再考虑。
   - **决策 A**: 这是用户明确反馈的痛点,管理员需要能直接选中复制单条 code 黏给单个用户,而不是被迫下载 CSV 再开表格找。

3. **rotate secret 后**
   - 选项 A: 自动撤销所有未兑换码 (`revoked_at=now()`) 便于后台审计; 用户输入旧明文码时通常仍是 CODE_NOT_FOUND,因为 hash 已随 secret 改变。
   - 选项 B: 不改 redemption_codes,旧码自然 hash 对不上变 CODE_NOT_FOUND。
   - **推荐 A**,显式撤销审计清晰。

4. **顶部余额胶囊在所有页面都显示?**
   - 选项 A: 全部页面 (含生图/对话/admin)。
   - 选项 B: 仅 `/me/*` 与 `/composer`。
   - **推荐 A**,但 admin 路径下不渲染。

## 7. 实施排序 (PR 拆分)

> 每个 PR 走 Lumen Release Workflow (版本号同步、tag、镜像)。所有 PR 都遵循 `apps/web/CLAUDE.md` 与 `apps/web/AGENTS.md` 的 Next.js 注意事项 + `frontend-theme-dialog-standards.md` 主题规范。

### PR-A (P0 修复, 1 个 PR 包打底)
1. 后端: B2 (错误码 412 + 消息) / B6 / B9 (thresholds-pricing 原子) / 新增 `/admin/billing/overview` / `/admin/wallets/{user_id}` / `/admin/redemption_codes/batches/{batch_id}/redownload` 决策点后定
2. 前端: BillingPanel 重构为 4 个 sub-tab; 概览 + 定价 + secret UI; W1/W2/W3/W4/W5/W6
3. 文档: 更新 `docs/billing-and-redemption-design.md` 把 LMN-XXXX-XXXX-XXXX 的字符段数对齐为 4 段
4. 测试: secret 未配置时管理员 UI 仍可加载; bootstrap 流程 e2e
5. 灰度: `billing.enabled` 保持现状

### PR-B (P0 修复, 兑换码 + 用户钱包)
1. 后端: B4 (列表分页 + status default + usable_count) / B5 (re-download) / B7 / B10 / W7 后端配合
2. 前端: CodesSubpanel + UserWalletsSubpanel; W7-W11 全部修复
3. 测试: 重下载、状态过滤、cursor 分页、用户详情拉取

### PR-C (P1 用户端体验)
1. 前端: `<WalletPill>` topbar 胶囊 (U6) + 钱包页改造 (U3) + 兑换历史 (U7) + BYOK 文案 (U4) + PromptComposer 预扣预览 (U5)
2. 后端: 新增 `/me/billing_status` 或扩展 `/me` 返回 `{billing_enabled, account_mode, wallet}` 一次性
3. 错误码本地化 (§5.5)

### PR-D (Worker 幂等 + 观测)
1. Worker B8 修复 + audit_logs.replay 事件
2. `scripts/wallet_audit.py` 增加 `--report-json`,Admin Web 概览 sub-tab 加"运行对账"按钮拉日志
3. 孤儿 hold 列表 + "强制 release" 按钮 (X4)

### PR-E (灰度 + 监控)
1. `billing.bootstrap_completed` 引导态
2. Prometheus / 内部 metrics: `wallet_balance_total`, `wallet_hold_active`, `redemption_redeemed_total`, `wallet_overdrawn_total`
3. Alert: `wallet.charge.lost` > 0 / `wallet.overdrawn` > 0 / 孤儿 hold > N

## 8. 测试计划

- **单测**: 新增/修改的 billing 函数 100% 覆盖; secret 旋转; thresholds-pricing 校验。
- **集成测**: `tests/api/test_billing_flow_e2e.py` 完整 happy-path: bootstrap → 创建 code → 用户 redeem → 生图 hold → settle → 余额对账。
- **并发测**: 同码双兑、同用户双 4k 提交、worker 同 generation 多次 settle。
- **回归测**: `billing.enabled=0` 时所有 wallet 写路径 no-op; byok 用户访问任一钱包 endpoint 403。
- **UI 测**: BillingPanel 在 secret 未配置时显示健康检查警告; redemption secret 修改对话框二次确认; CSV 重下载按钮在 5 分钟后自动 disabled。

## 9. 文档更新

- `docs/billing-and-redemption-design.md`: 兑换码格式段数统一为 4 段 16 字符; §9 UI 章节按本计划重写。
- 新增 `docs/billing-admin-runbook.md` (P1): 给运维写"从 0 启用计费的 10 步"。
- 新增 `docs/billing-troubleshooting.md` (P2): 常见报错 → 排查路径。

## 10. 风险与回滚

| 风险 | 应对 |
|---|---|
| 重构 BillingPanel 期间 admin 失去原有调账能力 | PR-A 强制保持 `/admin/wallets:adjust` 后端不变,前端旧入口暂时保留兜底 |
| Secret 旋转误操作作废大量未兑换码 | 二次确认对话框 + 后端 dry-run 接口 `/admin/billing/secret:rotate_preview` 返回"将作废 N 张" |
| thresholds 原子写校验过严导致管理员无法调整价格 | 校验提供 `force=true` 旁路 + 强烈警告 |
| 概览看板的 24h 统计聚合在大库下慢 | 用 `wallet_transactions (user_id, created_at desc)` 索引 + 限制窗口 24h; >100 万行时改物化视图 |
| 全局 `<WalletPill>` 在每个页面发 `/me/wallet` 请求带来负担 | `staleTime: 30s` + react-query 共享; 写操作后主动 invalidate |

---

**附:Lumen 发布检查表** (每个 PR 完成时跑一遍)
1. `python3 scripts/version.py sync` + `python3 scripts/version.py check`
2. `git diff --check`
3. `npm run type-check && npm run lint && npm run build` (in `apps/web`)
4. `pytest packages/core/tests apps/api/tests apps/worker/tests -x`
5. 提交 → push main → push tag `vX.Y.Z` → 等 `Docker Release` 成功
