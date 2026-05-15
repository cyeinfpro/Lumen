# 计费与兑换码系统设计

> 状态: 设计稿 · 待评审
> 影响范围: API · Worker · Web (admin & user) · DB schema

## 1. 背景与目标

Lumen 当前没有"额度 / 计费"概念,所有生成 / 对话都是免费消耗。本设计引入:

- **钱包 (Wallet)**: 每个用户一份 RMB 余额。
- **兑换码 (Redemption Code)**: 管理员发码,用户输码充值。
- **计费 (Billing)**: 生图按尺寸档位计费 (1k / 2k / 4k),对话按 OpenAI 官方 token 价计费且 1 USD = 1 RMB 直接折算。
- **管理后台配置**: 价格表与兑换码均在 admin 面板维护,无需重启服务。

**V1 范围**
- 单一币种 (RMB), 单一钱包
- 不实现现金支付 / 退款 / 发票
- 不实现订阅 / 团队额度共享
- BYOK 用户走自己的上游 key, **不**扣 Lumen 钱包 (与现有 byok_service 行为一致)

**V1 非目标 / 留给 V2**
- 多币种、汇率自动同步 (V1 写死 1 USD = 1 RMB)
- 分销 / 邀请返利
- 价格 A/B 测试与按用户分级定价
- 月结发票、第三方支付网关

## 2. 单位与精度

**问题**: RMB 浮点会累积误差; 0.2 + 0.2 + 0.2 ≠ 0.6 在 float64。

**决策**: 所有金额持久化为 **`BIGINT` micro-RMB**, 即 `1 RMB = 1_000_000 µRMB`。
- 余额、流水、兑换面额、定价规则统一用 µRMB 存。
- API 出参提供 `amount_micro` (整数, 权威值) 和 `amount_rmb` (`Decimal`/`str`, 展示值)。前端只读 `amount_rmb` 渲染,不要回写到计算。
- 这样 OpenAI 给出的 `0.005 USD / 1K tokens` 这种 6 位小数也能精确表达 (`5_000 µRMB / 1K tokens`)。

> 不用 Postgres `NUMERIC`: `BIGINT` 做加减更快,且 ORM 端不引入 `Decimal` 边界处理; 展示层临用临转。

## 3. 与现有机制的边界

| 关系 | 处理 |
|---|---|
| `system_settings` (KV) | 计费"开关"与"汇率"放这里 (`billing.enabled`, `billing.usd_to_rmb_rate`),价格 / 兑换码这类多行结构走独立表 |
| `audit_logs` | 所有钱包变动、兑换、定价变更都写一条 `audit_logs.event_type`,便于事后排查 |
| `users.account_mode` (**新**) | 账号种类。BYOK 注册的账号 = `byok`,邀请链接注册的账号 = `wallet`。两类**互斥**,功能集不重叠 — 见 §3.1 / §3.2 |
| `user_api_credentials` (BYOK) | 只允许 `account_mode='byok'` 的用户增删查; wallet 用户调用 `/me/api-credentials/*` 一律 `403` |
| `generations` / `completions` | 不在这两张表加金额列,改在 `wallet_transactions` 通过 `ref_type` + `ref_id` 反查 |
| `invites` | 邀请奖励 (新人注册送 X RMB) V1 不做,但 schema 留 `wallet_transactions.kind='grant'` 给未来 |

### 3.1 账号种类: `users.account_mode`

**两类账号互斥,出口能力完全不重叠**:

- **`byok` 账号** (BYOK 自助注册路径): 全程使用自己上传的 API Key 调上游。token / image 费用直接由用户在自己上游 (OpenAI 等) 的账单里结算,Lumen **不收一分钱、不维护钱包、不发兑换码**。**没有有效 key 就用不了服务**, Lumen 不提供平台 key 兜底。
- **`wallet` 账号** (邀请链接注册路径): 全程使用平台 Provider Pool 调上游,按本设计的钱包扣费。**不能上传/使用自己的 API Key**,即使他知道 BYOK 接口存在也调不通。

为此在 `users` 表上加一个 enum 字段:

```sql
ALTER TABLE users
  ADD COLUMN account_mode VARCHAR(16) NOT NULL DEFAULT 'wallet';
-- 取值: 'wallet' | 'byok'
CREATE INDEX ix_users_account_mode ON users (account_mode);
```

> 命名: 用 `account_mode` 而不是 `billing_mode` — 它不只决定怎么收钱,还决定走哪条上游通道、能不能上传 key、UI 渲染什么入口,是**账号种类**而非"计费策略"。

**写入时机** (由对应注册路径在 `INSERT INTO users` 时一并写,不依赖事后回填):

| 注册路径 | 入口 | `account_mode` | 备注 |
|---|---|---|---|
| 邀请链接邮密注册 | `POST /auth/signup` (受 `AllowedEmail` / `InviteLink` 守门) | `wallet` | 默认值,可不显式赋值 |
| 邀请链接 OAuth (Google 等) | `POST /auth/oauth/callback` | `wallet` | 同上 |
| BYOK 自助注册 | `POST /auth/signup/byok` (`apps/api/app/routes/auth.py:423`) | `byok` | 注册流程已经验证过 key 可用 (`PendingApiKeyVerification`),注册成功一定带活跃凭证 |
| Admin 后台手动建号 | (V1 不开放) | — | 想要内测白名单"白嫖"账号,走 `byok` 模式 + 内部共享 key,或保留 `wallet` 模式由 admin `adjust_admin` 直接打额度 |

### 3.2 功能门禁矩阵

| 能力 | `wallet` 用户 | `byok` 用户 |
|---|---|---|
| 调用生图 / 对话 | ✅ 走 Provider Pool,扣钱包 | ✅ 走**自己的** `user_api_credentials`; 无有效凭证 → `412 NO_ACTIVE_API_KEY` |
| 上传 / 替换 / 删除自己的 API Key (`/me/api-credentials/*`) | ❌ 全部 `403 ACCOUNT_MODE_FORBIDDEN` | ✅ 正常 |
| `GET /me/wallet` 看余额 | ✅ 返回余额对象 | ✅ 返回 `{mode:'byok', balance:null}` 让前端隐藏 UI |
| `POST /me/redemptions` 兑换码 | ✅ | ❌ `403 ACCOUNT_MODE_FORBIDDEN` |
| `GET /me/pricing` 价格表 | ✅ 用于前端预估 | ✅ 仅供透明展示 (不强制隐藏) |
| Provider Pool 平台 key 兜底 | n/a (本来就是它走) | ❌ **没有兜底**。设计上禁止 byok 用户透支到平台 key,否则就把 BYOK 模式的成本结算前提破坏了 |
| admin `adjust_admin` 给钱包加额度 | ✅ | ❌ `409 ACCOUNT_NOT_WALLET` (后台 UI 也禁掉该按钮) |

**实现要点**

1. **`/me/api-credentials/*` 守门**: 在 `apps/api/app/routes/byok.py` 的 `router_me` 三个端点 (`GET`/`PUT`/`DELETE`,见 `byok.py:470/518/629`) 顶部加 `require_account_mode('byok')` 依赖。wallet 用户得到 `403 ACCOUNT_MODE_FORBIDDEN` 而非"看起来能传但传完用不了"。
2. **生图 / 对话路径分发**: 现有 `byok_service` 已经按"用户是否有 active credential"路由。改造为:

    ```python
    if user.account_mode == "byok":
        cred = pick_active_credential(user)
        if cred is None:
            raise _http("NO_ACTIVE_API_KEY", "请上传可用的 API Key", 412)
        return cred                # 不允许 fallback 到平台 key
    else:  # wallet
        return None                # 走 Provider Pool;调用方据此扣钱包
    ```

    与现有 `byok.fallback_to_admin_provider` 设置的关系: 该开关仅对 byok 用户的**临时**失效 (key 过期 / 上游 401) 有意义,V1 默认 `false`; 真要打开,需要同时在计费链路加补丁"该次调用按 wallet 计费" — 留 V2。
3. **BYOK 注册后凭证被删光的回收**: byok 用户删掉最后一条凭证后,**账号不会自动转 wallet**。他下次发图直接得 `412 NO_ACTIVE_API_KEY`,前端引导"重新绑定 key"或"联系管理员转 wallet 模式"。这是有意为之,见下文"模式变更"。

### 3.3 模式变更 (admin-only)

V1 不开放用户自助切换 — 互斥账号种类如果自助切,会变成"白嫖套利"通道 (例: byok 注册→不绑 key→切 wallet→兑码; 或 wallet→切 byok→白嫖完再切回兑码)。只允许 admin 在后台手动改,且写 `audit_logs.event_type='account.mode_change'`:

- **`byok → wallet`**: 一般是用户主动放弃 BYOK 改走平台。admin 操作后:
  1. 软删该用户全部 `user_api_credentials` (`deleted_at=now()`),保留历史可审计;
  2. lazy 创建空钱包行;
  3. 不自动赠送额度,需要 admin 另发兑换码或 `adjust_admin`。
- **`wallet → byok`**: 不常见; 一般用于"内测账号被错配成 wallet 现在转 BYOK 测试"。admin 操作时需要二选一处理遗留余额:
  1. **冻结余额**: balance 保留但永不被消费 (后续不再扣钱包); 不退现金,V2 接退款再说。
  2. **清零余额**: 写一条 `adjust_admin` 行清零,流水留底。
  默认选 1。完成后用户**仍需自行上传 key**,admin 不能代上传 (避免管理员看到密钥)。

> 这套规则的核心 invariant: **任一时刻,`account_mode` 唯一决定能走哪些功能**。代码里不出现 "byok 用户也能用钱包" 或 "wallet 用户也能 BYOK" 的 if 分支,违反这条 invariant 的改动需要专门评审。

## 4. 数据模型

```text
user_wallets        N 用户 → 1 钱包行
wallet_transactions 钱包流水 (immutable ledger)
pricing_rules       计费规则 (image_size / chat_model 两类)
redemption_codes    管理员发布的兑换码
redemption_codes_usage 每次兑换的日志 (多次兑换支持留扩展位,V1 一次性码 1:1)
```

### 4.1 `user_wallets`

```sql
CREATE TABLE user_wallets (
  user_id            VARCHAR(36) PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  balance_micro      BIGINT NOT NULL DEFAULT 0 CHECK (balance_micro >= 0),
  hold_micro         BIGINT NOT NULL DEFAULT 0 CHECK (hold_micro >= 0),
  lifetime_topup_micro BIGINT NOT NULL DEFAULT 0,
  lifetime_spend_micro BIGINT NOT NULL DEFAULT 0,
  version            BIGINT NOT NULL DEFAULT 0, -- 乐观锁备用
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- `balance_micro`: 可用余额。
- `hold_micro`: 预扣 (生图任务尚未确认结果),`balance_micro` **不**包含该部分,即"可用 = balance_micro"; 总持有 = `balance_micro + hold_micro`。
- `lifetime_*`: 仅用于展示 / 统计,不参与扣减决策。
- 钱包行 lazy 创建: 第一次用到 (兑换、首次生图前置检查) 时 upsert。

### 4.2 `wallet_transactions` (流水)

```sql
CREATE TABLE wallet_transactions (
  id                VARCHAR(36) PRIMARY KEY,        -- uuid7
  user_id           VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind              VARCHAR(32) NOT NULL,            -- topup_redeem | hold | settle | release | refund | adjust_admin | grant
  amount_micro      BIGINT NOT NULL,                 -- 正=入账, 负=出账
  balance_after     BIGINT NOT NULL,                 -- 写入后的 balance_micro 快照
  hold_after        BIGINT NOT NULL,                 -- 写入后的 hold_micro 快照
  ref_type          VARCHAR(32),                     -- generation | completion | redemption | admin_adjust | NULL
  ref_id            VARCHAR(64),                     -- 对应业务实体 id (generation.id / redemption_codes.id / ...)
  idempotency_key   VARCHAR(96) NOT NULL,            -- 见 §6.4
  meta              JSONB NOT NULL DEFAULT '{}',     -- 模型、尺寸、token 数、单价等审计冗余
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_by_admin  VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL,
  CONSTRAINT uq_wallet_tx_idemp UNIQUE (user_id, idempotency_key)
);

CREATE INDEX ix_wallet_tx_user_created ON wallet_transactions (user_id, created_at DESC);
CREATE INDEX ix_wallet_tx_ref ON wallet_transactions (ref_type, ref_id);
```

**关键约束**

- 流水**只追加,不更新,不删除**。任何修正都写一条 `adjust_admin` 反向行,审计完整可重放。
- `idempotency_key` 在 `(user_id, idempotency_key)` 上唯一: 重放写入要么命中既有流水返回相同结果,要么因约束冲突回滚事务 (worker 层捕获后视作"已记账")。
- `balance_after` / `hold_after` 是写入后快照,可在线对账 (顺序回放 `amount_micro` 应等于 `balance_after`)。

**`kind` 取值与方向**

| kind | 用途 | amount 符号 | 影响 |
|---|---|---|---|
| `topup_redeem` | 兑换码充值 | + | `balance` += |
| `hold` | 生图入队前预扣 | − | `balance` -= , `hold` += (镜像; 流水里 amount 只算 balance 变化, hold 单独跟) |
| `settle` | 任务成功结算 | 0 / 负调整 | 释放 hold; 实际计价≠预扣时调整 |
| `release` | 任务失败 / 取消退款 | + | hold 全额回 balance |
| `refund` | 管理员事后退款 (已 settle) | + | balance += |
| `adjust_admin` | 任意调整 | ± | 由 admin 在后台手动操作 |
| `grant` | 平台赠送 (V2 邀请奖励占位) | + | balance += |

`hold` 与 `release` 这类**只挪动 `hold`** 的行,`amount_micro=0`, 但仍写流水以便审计; 它们的影响通过 `hold_after - hold_before` 体现 (meta 里冗余 `hold_delta`)。

### 4.3 `pricing_rules`

```sql
CREATE TABLE pricing_rules (
  id               VARCHAR(36) PRIMARY KEY,
  scope            VARCHAR(32) NOT NULL,    -- 'image_size' | 'chat_model'
  key              VARCHAR(64) NOT NULL,    -- image_size: '1k'|'2k'|'4k'; chat_model: 'gpt-5.5' 等
  variant          VARCHAR(32) NOT NULL DEFAULT 'default',  -- 预留 (例如 input/output 二价位时拆两行)
  unit             VARCHAR(32) NOT NULL,    -- 'per_image' | 'per_1k_tokens_in' | 'per_1k_tokens_out'
  price_micro      BIGINT NOT NULL CHECK (price_micro >= 0),
  enabled          BOOLEAN NOT NULL DEFAULT TRUE,
  note             TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_pricing_scope_key_variant_unit UNIQUE (scope, key, variant, unit)
);
```

**两类 scope:**

- **`scope='image_size'`**: 按预设尺寸档位定价。`key ∈ {'1k','2k','4k'}`, `unit='per_image'`。Worker 在 settle 时根据请求最长边映射到档位 (见 §6.2)。
- **`scope='chat_model'`**: 按模型 + token 方向定价。一个模型出两行: `unit='per_1k_tokens_in'` 与 `unit='per_1k_tokens_out'`。`price_micro` 直接是 µRMB/1K tokens (而非 USD,见 §6.3 折算)。

> 不用 `system_settings` JSON blob 装价格表的原因: 改一行价就要 PUT 整段 JSON, 并且没有 unique 约束保护; 独立表带索引更安全。

### 4.4 `redemption_codes`

```sql
CREATE TABLE redemption_codes (
  id               VARCHAR(36) PRIMARY KEY,
  code_hash        VARCHAR(64) NOT NULL UNIQUE,  -- HMAC-SHA256(secret, code), 见 §7.2
  code_prefix      VARCHAR(8) NOT NULL,           -- code 前 4 位明文, 用于列表展示
  amount_micro     BIGINT NOT NULL CHECK (amount_micro > 0),
  max_redemptions  INTEGER NOT NULL DEFAULT 1 CHECK (max_redemptions >= 1),
  redeemed_count   INTEGER NOT NULL DEFAULT 0,
  batch_id         VARCHAR(36),                   -- 同一批次共享, 便于撤销整批
  note             TEXT,
  expires_at       TIMESTAMPTZ,                   -- NULL = 永不过期
  revoked_at       TIMESTAMPTZ,                   -- 管理员撤销
  created_by       VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_redemption_codes_batch ON redemption_codes (batch_id);
CREATE INDEX ix_redemption_codes_status ON redemption_codes (revoked_at, expires_at);
```

- **不存明文 code**: 只存 `HMAC(secret, code)`; 管理员"看一眼明文"只在创建响应里返回一次,之后不可再回显。
- `max_redemptions = 1` 是默认 (一码一兑),设计上允许 N 次兑换 (例如内测共享码),但同一用户对同一码**只能兑换一次** (`redemption_codes_usage` 唯一约束)。
- `batch_id`: 批量创建 (例如一次 1000 张码) 时分配同一 batch,后台可"撤销整批"。

### 4.5 `redemption_codes_usage`

```sql
CREATE TABLE redemption_codes_usage (
  id               VARCHAR(36) PRIMARY KEY,
  code_id          VARCHAR(36) NOT NULL REFERENCES redemption_codes(id) ON DELETE RESTRICT,
  user_id          VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  amount_micro     BIGINT NOT NULL,
  wallet_tx_id     VARCHAR(36) NOT NULL REFERENCES wallet_transactions(id),
  redeemed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  ip_hash          VARCHAR(64),
  CONSTRAINT uq_redeem_code_user UNIQUE (code_id, user_id)
);

CREATE INDEX ix_redeem_user_time ON redemption_codes_usage (user_id, redeemed_at DESC);
```

## 5. `system_settings` 新增 key

| key | parser | 默认 | 说明 |
|---|---|---|---|
| `billing.enabled` | bool | `false` | 总开关。`false` 时所有计费逻辑短路,等同当前免费行为,便于灰度 |
| `billing.usd_to_rmb_rate` | float | `1.0` | OpenAI USD 价折算到 RMB 的倍率。V1 写死 1.0; 接入支付后再放管理员调 |
| `billing.allow_negative_balance` | bool | `false` | `true` 时允许扣到负数 (内测白名单友好); 生产保持 `false` |
| `billing.image_size_thresholds` | str (JSON) | `{"1k":1572864,"2k":3686400,"4k":8294400}` | 像素数 → 档位映射的**右开区间下界**,见 §6.2 |
| `billing.redemption_code_secret` | str (sensitive) | `RNG@deploy` | HMAC 盐; 部署时 admin 写入,旋转需要兼容老码: 表里加 `secret_version` 字段; V1 先单 secret,日后再加 |
| `billing.low_balance_warn_micro` | int | `2_000_000` (= 2 RMB) | 余额低于此值时,前端横幅提示 |

> 计费规则本身不放这里 (见 §4.3 独立表),只放全局开关 & 阈值。

## 6. 计费流程

### 6.0 前置: 是否进入计费

**所有计费写路径 (`hold` / `settle` / `release` / `charge`) 在第一行都做两个短路判断**:

```python
if user.account_mode != "wallet":         # §3.1 byok 账号根本不走钱包
    return
if not billing_enabled:                   # §5 全局灰度开关
    return
```

下面 §6.1 / §6.3 的伪代码默认已通过这两个判断,即"该用户是 wallet 模式 + 全局已开启计费"。

`byok` 用户的请求在更上游已经分支出去 (`byok_service` 路由到自有凭证),根本不会进到生图 / 对话的计费 hook 路径; 这里再判一次是 belt-and-suspenders。

### 6.1 生图 (Generation)

生图是异步任务: API 收请求 → 入队 → Worker 拉任务 → 调上游 → 写回。计费要解决两个问题:

1. 入队前用户要够钱; 否则前端立刻提示而不是任务跑一半失败。
2. 任务失败 / 取消 / 超时,不能让钱"消失"。

**采用 hold → settle / release 模式:**

```
[API.POST /messages]
  └─ 估算预扣金额: estimate_micro = price_of(planned_size)
  └─ 同事务:
       SELECT user_wallets WHERE user_id=:u FOR UPDATE;
       if balance < estimate: 422 INSUFFICIENT_BALANCE
       UPDATE balance -= estimate, hold += estimate;
       INSERT wallet_transactions(kind='hold', amount=-estimate,
                                   ref_type='generation', ref_id=:gen_id,
                                   idempotency_key=f"hold:{gen_id}");
  └─ enqueue arq job

[Worker.run_generation]
  └─ 调上游,拿到实际成片 (size_actual, image_count_actual)
  └─ on SUCCESS:
       actual_micro = price_of(size_actual) * image_count_actual
       UPDATE balance += (estimate - actual_micro), hold -= estimate;  # 释放 hold, 把多/少的回填给 balance
       INSERT wallet_transactions(kind='settle', amount=-(actual_micro - estimate) ...
                                   idempotency_key=f"settle:{gen_id}");
  └─ on FAIL / CANCEL / TIMEOUT:
       UPDATE balance += estimate, hold -= estimate;
       INSERT wallet_transactions(kind='release', amount=+estimate,
                                   idempotency_key=f"release:{gen_id}");
```

**注意**:

- 单条 generation 只能进入 settle 或 release **之一**,且最多一次。约束由 `(user_id, idempotency_key)` 保证。
- 多图任务 (n>=2): 入队前按 `n * unit_price` 一次性 hold; settle 时按实际产出张数计价 (失败的张数不计费,只占了 hold 那部分进 release 退回)。**实现细节**: 每张图的 worker 独立 settle 自己那 1/n 份额, 最后一张完成时由 fan-in 触发 release 任何余款。
- 4K 等高价档位: 当 `balance < estimate(4k)` 但 `balance >= estimate(2k)` 时, API 直接拒绝,不做"降档兜底"; 前端引导用户充值。

### 6.2 尺寸 → 档位映射

像素数到档位的映射在 `billing.image_size_thresholds` 配:

```python
# 默认值, 阈值是档位的"最低像素", 用 bisect 选档
THRESHOLDS = [
  ("1k", 1572864),   # 1K (≈ 1.57M, 当前 PIXEL_BUDGET)
  ("2k", 3686400),   # 2K (2560*1440)
  ("4k", 8294400),   # 4K (3840*2160)
]

def tier_for_pixels(px: int) -> str:
    # 落在最大不超过的桶, 超过 4k 的也按 4k 计 (与 MAX_EXPLICIT_PIXELS 一致)
    tier = "1k"
    for name, lo in THRESHOLDS:
        if px >= lo:
            tier = name
    return tier
```

最终单价在 `pricing_rules` 查: `WHERE scope='image_size' AND key=:tier AND unit='per_image' AND enabled=TRUE`。
缺行 (例如 admin 删了 `1k`): 视为 `0` (免费) 而非报错,且写一条 `audit_logs` 提醒 (admin 配置缺失)。

### 6.3 对话 (Completion)

对话**同步等上游返回**,拿到 `tokens_in / tokens_out` 后再扣,无需 hold。

```python
# Worker 完成 completion 后:
in_rate_micro_per_1k = pricing_rules[(chat_model, 'per_1k_tokens_in')].price_micro
out_rate_micro_per_1k = pricing_rules[(chat_model, 'per_1k_tokens_out')].price_micro
cost = (tokens_in * in_rate_micro_per_1k + tokens_out * out_rate_micro_per_1k) // 1000
# 1 USD = 1 RMB: pricing_rules 里直接以 RMB 维护; 见 §6.3.1 导入流程
```

**6.3.1 OpenAI USD 价 → pricing_rules 导入**

V1 不自动同步,管理员手动维护。但提供脚本 `scripts/import_openai_prices.py`:

```bash
python3 scripts/import_openai_prices.py --rate 1.0 --file ./openai-prices.yaml
```

`openai-prices.yaml` 形如:

```yaml
- model: gpt-5.5
  input_usd_per_1m: 5.00      # OpenAI 官网价
  output_usd_per_1m: 15.00
- model: gpt-5.4
  input_usd_per_1m: 2.50
  output_usd_per_1m: 10.00
```

脚本动作: `price_micro = round(usd_per_1m * usd_to_rmb_rate * 1_000_000 / 1000)` (单位是 µRMB/1K tokens),upsert 到 `pricing_rules`。后台也提供"重新计算 RMB 价"按钮,改 `usd_to_rmb_rate` 后一键刷新。

**6.3.2 余额不足处理**

- 对话**先发送请求,再算价**。如果 worker 完成后发现余额扣到负,而 `allow_negative_balance=false`: 仍允许这条对话返回 (避免半截), 但**记 `balance` = 0, 写一条 `adjust_admin` 行补差**,并把用户钱包标志 `frozen_at` (新增字段, V1 简化为流水里发 `audit_logs.event_type='wallet.overdrawn'`), 之后所有计费操作都拒绝直到充值。
- 入口处仍做软检查: `balance < 0.01 RMB && 非 BYOK` → API 直接 402,引导充值,避免每次都"先讲后扣"撑出账。

### 6.4 幂等键约定

| 业务 | idempotency_key |
|---|---|
| 生图 hold | `hold:<generation_id>` |
| 生图 settle | `settle:<generation_id>` |
| 生图 release | `release:<generation_id>` |
| 对话扣费 | `complete:<completion_id>` |
| 兑换码兑换 | `redeem:<redemption_code_usage_id>` |
| 管理员调整 | `adjust:<random_uuid>` (由 API 端生成,前端不指定) |

worker 重试时直接复用上述 key,DB 唯一约束兜底; 命中冲突时回退到"读最近一行流水校验状态"。

## 7. 兑换码生命周期

### 7.1 编码格式

```
LMN-XXXX-XXXX-XXXX     16 位明文 (4 段 × 4 字符)
```

- 字符集: Crockford Base32 (去掉易混的 0/O/1/I/L) = `23456789ABCDEFGHJKMNPQRSTVWXYZ`。
- 26.9 ≈ 字符 → 约 80 bit entropy,足够防猜测。
- `LMN-` 前缀供 UI 校验 / 友好提示 ("看起来不像兑换码")。
- 第一段 `XXXX` (即 `code_prefix`) 在后台列表展示,便于客服对账。

### 7.2 存储

```python
def hash_code(code: str) -> str:
    secret = get_setting("billing.redemption_code_secret")
    norm = code.strip().upper().replace("-", "")  # 用户输入容错
    return hmac.new(secret.encode(), norm.encode(), hashlib.sha256).hexdigest()
```

- 数据库不存明文 code, 只存 `code_hash`。
- 创建响应**只回传一次明文** (供管理员复制 / 下载 CSV); 后续无法找回 → 列表只显示 `code_prefix`。
- secret 旋转: 当前 V1 不支持; 留 `code_hash` 字段加 `secret_version`,日后追加列即可。

### 7.3 兑换流程

```
POST /me/redemptions  { code: "LMN-..." }

API 端 (单事务):
  1. norm = normalize(code); hash = hmac(norm)
  2. SELECT redemption_codes WHERE code_hash=:hash FOR UPDATE;
        if NULL → 404 CODE_NOT_FOUND
        if revoked_at → 410 CODE_REVOKED
        if expires_at < now → 410 CODE_EXPIRED
        if redeemed_count >= max_redemptions → 409 CODE_EXHAUSTED
  3. INSERT redemption_codes_usage (uq 命中 → 409 CODE_ALREADY_USED_BY_USER)
  4. UPDATE redemption_codes SET redeemed_count += 1
  5. UPDATE user_wallets SET balance += amount, lifetime_topup += amount (upsert if missing)
  6. INSERT wallet_transactions(kind='topup_redeem', amount=+amount, ref_type='redemption', ref_id=usage.id, idempotency_key=f"redeem:{usage.id}")
  7. INSERT audit_logs(event_type='wallet.topup.redeem', ...)
返回: { amount_rmb, balance_rmb }
```

**速率限制**: 同 IP / 同账号在 5 分钟内最多尝试 10 次,使用现有 `RateLimiter` (Redis 令牌桶)。失败 1 次即扣 1 个 token。防止穷举猜码。

### 7.4 撤销 / 失效

- 后台支持单码撤销 (`UPDATE revoked_at = now()`) 与批次撤销 (按 `batch_id`)。
- 已兑换的 usage 行不动 (历史事实不能改)。仅未来兑换被拒。
- 不支持"撤销并退款"自动化: 如需追回,管理员走 `adjust_admin` 手动出账。

## 8. 公开 API

> 所有金额输入参数都是 **元** 单位的字符串/小数 (admin 友好),后端立刻 `Decimal → micro` 转换并校验; 出参同时给 micro + rmb。

### 8.1 用户端

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/me/wallet` | 余额 + 流水分页; `byok` 用户返回 `mode='byok'` 让前端隐藏入口 |
| `GET` | `/me/wallet/transactions?cursor=&limit=` | 流水分页; `byok` 用户 → `403 ACCOUNT_MODE_FORBIDDEN` |
| `POST` | `/me/redemptions` | 兑换 `{code}`; 422/404/409/410 见 §7.3; `byok` 用户 → `403 ACCOUNT_MODE_FORBIDDEN` |
| `GET` | `/me/redemptions` | 我的兑换历史; `byok` 用户 → `403 ACCOUNT_MODE_FORBIDDEN` |
| `GET` | `/me/pricing` | 当前可用价格表 (只回 enabled=TRUE); 两类用户都能查,前端按 `account_mode` 决定是否渲染 |

对 BYOK 凭证管理端点同步收紧 (`byok.py:470/518/629`):

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/me/api-credentials` | `wallet` 用户 → `403 ACCOUNT_MODE_FORBIDDEN` |
| `PUT` | `/me/api-credentials/{id}` | 同上 |
| `DELETE` | `/me/api-credentials/{id}` | 同上 |

`GET /me/wallet` 响应 (wallet 模式):

```json
{
  "mode": "wallet",
  "balance": { "micro": 12345000, "rmb": "12.345" },
  "hold":    { "micro":        0, "rmb":  "0.000" },
  "low_balance_threshold": { "micro": 2000000, "rmb": "2.000" },
  "frozen": false
}
```

`GET /me/wallet` 响应 (`byok` 模式):

```json
{
  "mode": "byok",
  "balance": null,
  "hold": null,
  "frozen": false
}
```

### 8.2 Admin 端

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/admin/pricing` | 列价格规则 |
| `PUT` | `/admin/pricing` | 批量 upsert (按 `(scope,key,variant,unit)` 主键; `enabled=false` 即软停) |
| `POST` | `/admin/pricing/import_openai` | 调用 §6.3.1 脚本逻辑,接收 yaml/json 内容 |
| `GET` | `/admin/redemption_codes?status=&batch_id=&q=` | 列表 (只展示 `code_prefix`) |
| `POST` | `/admin/redemption_codes` | 创建 `{amount_rmb, count, max_redemptions?, expires_at?, note?}` |
| `POST` | `/admin/redemption_codes/{id}:revoke` | 撤销单码 |
| `POST` | `/admin/redemption_codes/batches/{batch_id}:revoke` | 撤销整批 |
| `GET` | `/admin/redemption_codes/batches/{batch_id}.csv` | 下载该批次明文 (**仅在创建时返回的临时 download_token 有效期内可下载**) |
| `GET` | `/admin/wallets?q=email&mode=` | 列用户钱包 (可按 `account_mode` 过滤,默认只列 `wallet`); 行上显示 `mode` 列 |
| `POST` | `/admin/wallets/{user_id}:adjust` | `{amount_rmb_signed, reason}`; 写 `adjust_admin` 流水; 对 `byok` 用户返回 `409 ACCOUNT_NOT_WALLET` |
| `POST` | `/admin/users/{user_id}:set_account_mode` | `{mode, on_residual_balance: 'freeze'\|'zero'}`; 切换 `users.account_mode`,见 §3.3 (含软删 BYOK 凭证、余额处理) |
| `GET` | `/admin/wallets/{user_id}/transactions` | 该用户流水 |

**`POST /admin/redemption_codes` 创建响应**

```json
{
  "batch_id": "01HX...",
  "count": 100,
  "amount_rmb": "50.00",
  "download_token": "tok_...",   // 5 分钟内可调下载接口拿明文 CSV
  "expires_at": "2026-06-30T00:00:00Z"
}
```

明文 code 列表**不直接回 JSON** (避免日志 / 浏览器历史泄漏); 强制走带 `download_token` 的下载接口,后端按 `token` 在 Redis 取一次性 buffer,取完即焚。

### 8.3 错误码

```
INSUFFICIENT_BALANCE     402  生图 / 对话前置检查 (wallet 用户)
WALLET_FROZEN            402  对话过度透支后冻结
NO_ACTIVE_API_KEY        412  byok 用户调生图/对话时没有可用凭证, 引导去 /me/api-credentials 重传
ACCOUNT_MODE_FORBIDDEN   403  跨模式调用: wallet 用户访问 /me/api-credentials 或 byok 用户访问 /me/redemptions
ACCOUNT_NOT_WALLET       409  admin 对 byok 用户做钱包 adjust 时
CODE_NOT_FOUND           404
CODE_REVOKED             410
CODE_EXPIRED             410
CODE_EXHAUSTED           409
CODE_ALREADY_USED        409  (本用户已兑过此码)
PRICING_NOT_CONFIGURED   503  (生图/对话发现对应档位/模型无 rule)
```

## 9. Web UI

### 9.1 用户侧

**前提**: `UserOut` schema 加 `account_mode: 'wallet' | 'byok'` 字段; 所有 UI 分支都从这个字段读,不要在前端推断。

**wallet 用户 (邀请链接注册)**

- 顶部导航右侧 (头像左) 加 **余额胶囊**: `¥12.35` (低于阈值标红)。
- 点击进 `/me/wallet` 页面: 余额卡片 + 流水表 + 兑换码输入框。
- 兑换码输入框: 大字号 `LMN-XXXX-XXXX-XXXX`, 自动补连字符 / 大写; 成功后 toast `+¥50.00` 并刷新余额。
- 生图 / 对话发送前在前端按 `/me/pricing` 缓存做"本次大约扣 ¥X.XX"提示 (尤其 4K 单张 0.8 元这种); 不替代后端 hold,只为体验。
- 设置页**不渲染** "BYOK / API Keys" 入口。

**byok 用户 (BYOK 自助注册)**

- **不渲染**余额胶囊、不渲染"兑换码"入口。
- `/me/wallet` 路由仍可访问但展示为说明卡: "你的账号通过 BYOK 注册,所有费用由你的上游 API 账单结算,Lumen 不收取额外费用。"
- 设置页保留 "BYOK / API Keys" 入口 (`/me/api-credentials`)。
- 调生图 / 对话失败拿到 `412 NO_ACTIVE_API_KEY` 时: 前端拦截这个错误码,跳出引导浮层 "请到 设置 → API Keys 重新绑定一张可用的 API Key",带快捷跳转。
- 生图 / 对话面板**不**显示"本次约扣 ¥X.XX"提示 (费用透明度由用户自己看上游账单)。

### 9.2 管理员侧

新增两个面板 (按现有 `_panels/*.tsx` 模式):

- **`BillingPanel.tsx`**
  - Tab 1 "尺寸定价": 1k / 2k / 4k 三行 + "添加档位"; 内联编辑 `price_rmb` 直接写库。
  - Tab 2 "对话模型定价": 按模型分组,每行展示 输入价 / 输出价 (µRMB/1K) + USD 来源价 (注释列), "从 OpenAI 价目重算" 按钮。
  - Tab 3 "全局开关": `billing.enabled` / `usd_to_rmb_rate` / `low_balance_threshold` / `image_size_thresholds`。
- **`RedemptionPanel.tsx`**
  - 顶部"批量发码" → 弹窗 `面额 / 数量 / 有效期 / 备注`, 提交后给一份**只看一次**的 CSV 下载。
  - 列表筛选: 状态 (可兑/已兑完/撤销/过期) / 批次 / 前缀搜索。
  - 行操作: 撤销 / 复制前缀 / 查看兑换记录。
  - 顶部"用户钱包调账" → 输 email/uuid → 看余额 + 流水 → 加减额输框 + 必填理由,提交后写 `adjust_admin`。

两个面板加入 `admin/page.tsx` 的 tab 注册表; 走主题/对话标准 (`docs/frontend-theme-dialog-standards.md`),不用硬编码深色。

## 10. 集成点 (调用路径)

```
apps/api/app/routes/auth.py
  └─ signup           → users.account_mode='wallet'   (默认; 邀请路径)
  └─ signup_byok      → users.account_mode='byok'     (apps/api/app/routes/auth.py:423)
  └─ oauth_callback   → users.account_mode='wallet'   (邀请路径)

apps/api/app/routes/byok.py              (router_me, prefix=/me/api-credentials)
  └─ GET / PUT / DELETE → 顶部加 require_account_mode('byok')
                          wallet 用户 → 403 ACCOUNT_MODE_FORBIDDEN

apps/api/app/byok_service.py             (上游凭证选取)
  └─ if account_mode == 'byok' and no active credential:
         raise 412 NO_ACTIVE_API_KEY    (不 fallback 到平台 key)
  └─ if account_mode == 'wallet':
         return None  (上层走 Provider Pool + 计费)

apps/api/app/routes/messages.py          POST /messages
  └─ before-enqueue:
       if user.account_mode == 'wallet':              # §6.0 短路
           billing.estimate_image_cost(generation)
           billing.hold(user, amount, ref=generation) # §6.1

apps/worker/.../image_pipeline.py        on_success / on_failure
  └─ billing.settle_image(...)  # 函数内首行检查 user.account_mode
  └─ billing.release_image(...)

apps/worker/.../completion_pipeline.py   after upstream returns
  └─ billing.charge_completion(...)  # 同上

apps/api/app/routes/me.py
  └─ GET /me/wallet                 → 按 account_mode 分支返回
  └─ POST /me/redemptions           → byok → 403 ACCOUNT_MODE_FORBIDDEN

apps/api/app/routes/admin_billing.py     新文件
  └─ /admin/pricing, /admin/redemption_codes, /admin/wallets/*
  └─ POST /admin/users/{id}:set_account_mode  (§3.3)
```

新增模块 `apps/api/app/billing.py` 集中提供:

```python
async def hold(db, user_id, amount_micro, *, ref_type, ref_id, idemp): ...
async def settle(db, user_id, *, ref_type, ref_id, actual_micro, idemp): ...
async def release(db, user_id, *, ref_type, ref_id, idemp): ...
async def charge(db, user_id, amount_micro, *, ref_type, ref_id, idemp, meta): ...
async def adjust(db, user_id, amount_micro_signed, *, admin_id, reason): ...

async def estimate_image_cost(db, *, size_px, n): ...
async def estimate_completion_cost(db, *, model, tokens_in, tokens_out): ...
async def get_wallet(db, user_id, *, lock=False): ...
```

所有写路径必须在 **同一事务** 内: 先 `SELECT ... FOR UPDATE` 取钱包,再 INSERT 流水,最后 UPDATE 钱包。这保证并发两条请求不会双扣或双发。

`billing.enabled=false` 时所有 `hold/settle/charge` 提前 return,无任何 DB 写入 (灰度安全)。

## 11. 并发与失败

| 场景 | 风险 | 处理 |
|---|---|---|
| 同一兑换码两台手机同点 | 双兑 | `FOR UPDATE` 锁 `redemption_codes` 行 + `redeemed_count` 版本号检查 |
| 同一用户并发发起两条 4K 生图,余额刚好够一条 | 双 hold 致负 | 钱包 `SELECT FOR UPDATE`; 第二条事务等到第一条提交,看到 balance 不够,422 |
| Worker settle 时 DB 暂不可用 | settle 丢失 → 钱永远 hold | settle 在 worker 重试链路里; 永久失败时进 outbox_dead_letters, admin 后台手动 release |
| API 写完 hold 但 enqueue 失败 | hold 永挂 | hold 与 enqueue 同事务: 用 `outbox_events` (项目已有) 把 enqueue 任务一起入库,worker 端拉 outbox; 入库即认 "已入队",worker 拉到再投 arq |
| OpenAI 已扣 token 但 worker crash 在 charge 之前 | 漏扣 | completion 行落库时附 `tokens_in/out`; charge 用 `idempotency_key=complete:<id>`; 若 charge 失败 → outbox 重试; 极端永久失败 → audit_logs `wallet.charge.lost` + alert |
| 兑换码穷举 | 安全 | 见 §7.3 速率限制; 另外 `code_hash` 唯一索引保证一次 SELECT 即返,不留时序泄漏 |

## 12. 审计

每个钱包动作都同时写 `audit_logs`,`event_type` 命名:

```
wallet.topup.redeem
wallet.hold.image
wallet.settle.image
wallet.release.image
wallet.charge.completion
wallet.adjust.admin
wallet.overdrawn

pricing.update
redemption.create
redemption.revoke
redemption.batch.revoke
```

`details` JSONB 冗余 `amount_micro / amount_rmb / ref_id / before / after`,便于 BI / 客服直接查。

## 13. 迁移与上线

### 13.1 Alembic 0023

新增表: `user_wallets` / `wallet_transactions` / `pricing_rules` / `redemption_codes` / `redemption_codes_usage`。

新增列: `users.account_mode VARCHAR(16) NOT NULL DEFAULT 'wallet'` + `ix_users_account_mode` 索引 + `CHECK (account_mode IN ('wallet','byok'))`。

**存量用户回填**:

```sql
-- 把通过 /auth/signup/byok 注册过的存量用户标记为 byok。
-- 判别条件: audit_logs 里有 auth.signup.byok.success 事件 (权威)。
-- 不用 "当前有 user_api_credentials" 作为判据 — 现网允许了 wallet-ish 用户后加 BYOK 凭证, 那不是 byok 账号。
UPDATE users u SET account_mode = 'byok'
WHERE EXISTS (
  SELECT 1 FROM audit_logs a
   WHERE a.user_id = u.id AND a.event_type = 'auth.signup.byok.success'
);

-- 数据一致性校验 (回填后跑, 不通过则需 admin 手工核对再放行):
-- 1) 所有 byok 账号都应该至少有过一条 user_api_credentials (即使现在软删了)
SELECT u.id, u.email FROM users u
 WHERE u.account_mode='byok'
   AND NOT EXISTS (SELECT 1 FROM user_api_credentials c WHERE c.user_id=u.id);
-- 2) 所有 wallet 账号都不应该当前还有 active BYOK 凭证 (否则违反 §3.2)
SELECT u.id, u.email FROM users u
 JOIN user_api_credentials c ON c.user_id=u.id AND c.deleted_at IS NULL
 WHERE u.account_mode='wallet';
```

回填策略说明: V1 假定线上现状是"BYOK 注册用户都用 BYOK,邀请用户没人传 BYOK 凭证"。如果校验查询 2 命中非空 (历史上让邀请用户也传过 key),需要 admin 在迁移前先决策这些账号到底标 `wallet` (软删它们的凭证) 还是 `byok` (走 §3.3 标准切换)。**Migration 不自动选边**。

`pricing_rules` 同 migration 插入默认行:

```sql
INSERT INTO pricing_rules (id, scope, key, variant, unit, price_micro, enabled, note) VALUES
  (uuid7(), 'image_size', '1k', 'default', 'per_image', 200000, true, '默认 0.20 元/张'),
  (uuid7(), 'image_size', '2k', 'default', 'per_image', 400000, true, '默认 0.40 元/张'),
  (uuid7(), 'image_size', '4k', 'default', 'per_image', 800000, true, '默认 0.80 元/张');
```

对话模型默认行**不**塞 (各部署模型不同; admin 自己导)。

`SUPPORTED_SETTINGS` 追加 §5 的 6 个 key。

### 13.2 灰度

1. Migration 上线 → `billing.enabled=false` (默认), 等于一切照旧。
2. 后台导入价格 + secret + 测试码; admin 自己兑一张,确认流水正确。
3. 选一两个内部账号开 `billing.enabled=true` (per-user flag 简化为 settings JSON allow-list `billing.allow_users` 或直接发布到所有用户)。
4. 监控 1 周: `wallet.overdrawn` 与 `wallet.charge.lost` 应为 0。
5. 全量开启。

### 13.3 回滚

`billing.enabled=false` 即停所有计费,数据不丢; 真要回滚 schema 需先 export `wallet_transactions` (审计要求)。

## 14. 测试计划

- **单测**: `billing.py` 每个函数; pricing 选档算子; HMAC 编码 / 容错 ("lmn xxxx" 也能识别)。
- **并发测**: pytest + `asyncio.gather` 模拟同 IP 双兑、同用户双 4K 提交; 断言流水唯一约束生效。
- **集成测**: `tests/api/test_billing_flow.py`: 兑换 → 生图 (mocked worker) → 成功 settle → 余额对账。
- **回放测**: 把 `wallet_transactions.amount_micro` 顺序累加,应与 `balance_after` 完全一致 (用于线上对账巡检脚本 `scripts/wallet_audit.py`)。
- **失败模拟**: worker 注入异常,确认 `release` 走通; 注入 settle 失败,确认进 outbox。

## 15. 实施排序 (建议 PR 拆分)

1. **PR-1 Schema + 账号种类门禁 + billing 核心库**:
   - Alembic 0023 (含 `users.account_mode` 列 + 校验/回填脚本)
   - `lumen_core/models.py` 新增 ORM 类 + `User.account_mode`
   - `apps/api/app/deps.py` 加 `require_account_mode('wallet'|'byok')` 依赖
   - `/me/api-credentials/*` 三个端点挂 `require_account_mode('byok')`
   - `byok_service` 调整为 §3.2 "byok 无凭证 → 412, 不 fallback"
   - `/auth/signup/byok` 写 `account_mode='byok'`
   - `apps/api/app/billing.py` 提供函数 (首行 `account_mode != 'wallet'` / `billing.enabled` 双短路),**不**接生图/对话路径
   - 带单测,重点覆盖 §3.2 矩阵每一格
2. **PR-2 Admin API + Web BillingPanel/RedemptionPanel**: 让 admin 能配价、发码、调账; 用户仍走免费路径 (`billing.enabled` 仍 false)。
3. **PR-3 User API + 余额胶囊 + 兑换页**: 用户能看余额、能兑换、`/me/pricing` 可查; 仍未接计费。
4. **PR-4 接入生图 hold/settle/release**: 灰度 1-2 个测试账号。
5. **PR-5 接入对话 charge + 余额负值处理**: 灰度。
6. **PR-6 OpenAI USD 价导入脚本 + 后台一键重算**: 价格维护工具链。
7. **PR-7 对账脚本 + 监控 alert**: 上线后日运维。

每个 PR 都遵循 Lumen Release Workflow (版本号同步、tag、镜像)。
