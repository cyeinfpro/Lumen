# 未提交改动 Review —— 深度 Bug 挖掘

- 日期: 2026-07-27
- 分支: `main`（工作区未提交状态，基线 `bec89c9`）
- 范围: 全部未提交修改，224 个文件（+10666 / −6322），含 15 个未跟踪新文件
- 方法: 逐模块精读生产代码 diff + 对关键推断编写复现脚本实际执行验证
- 结论: **确认 8 个缺陷（P0×3 · P1×3 · P2×2）**，另有 1 项运维语义变更需确认

> 本文档是对 `2026-07-26-deep-bug-audit.md` 所记录修复动作的**独立复核**。
> 上一份文档在「修复后复核」章节声称相关 P0 已全部修复；本轮复核发现，其中
> 数项修复**彼此方向相反**，落地后互相抵消，并引入了新的阻断级缺陷。

---

## 测试现状

| 套件 | 结果 |
|------|------|
| `packages/core` | 555 passed |
| `apps/worker` | 1498 passed, 5 skipped |
| `apps/api` + `apps/tgbot` + `image-job` + `tests` | 1862 passed, 3 skipped, 26 failed |

那 26 个失败全部集中在 `tests/test_storage_mount_script.py`。经 `git stash` 在**干净树**上复跑，同样 26 failed / 11 passed —— 属于 macOS 上跑 Linux mount 脚本的既有环境问题，与本批改动无关。

**本文档记录的 8 个缺陷，测试套件全部未覆盖。** 其中 P0-1 反而有一条测试（`test_settle_caps_charge_at_the_authorized_hold`）明确断言了错误的一侧行为。

---

## 核心矛盾：两条互斥原则同时被写进代码

本批改动同时采纳了两套无法共存的计费原则：

| 原则 | 出处 | 在 `actual > hold` 时的要求 |
|------|------|------------------------------|
| **A. hold 是用户授权上限** | `packages/core/lumen_core/billing.py:729-732` 注释 | 只能扣到 hold，超出部分平台吸收 |
| **B. 纯转嫁，平台绝不吸收上游成本** | `packages/core/lumen_core/upstream_billing.py:3-6` 模块 docstring | 必须全额扣，差额记为欠费 |

这两条在 `actual > hold` 时**必然冲突**。代码里两边都实现了，运行时 A 生效、B 成为死代码：

```
apps/worker/app/video_billing.py:133-165   按 B 实现：捕获超额异常，返回真实成本
        ↓ actual_micro=5000
packages/core/lumen_core/billing.py:733    按 A 实现：actual = min(5000, 100) = 100
        ↓
结果：只扣 100，B 侧代码与日志全部失效
```

同样的冲突出现在图片失败路径（P1-4）。**这是本轮所有 P0/P1 的共同根因：先定原则，再改代码。**

---

# P0 级缺陷

## P0-1 `settle()` 封顶把超额上游成本转嫁给平台，与同批 worker 代码正面冲突

**位置**: `packages/core/lumen_core/billing.py:733` ｜ `apps/worker/app/video_billing.py:133-165`

`settle()` 新增封顶：

```python
actual = min(raw_actual, held)
unauthorized_micro = max(0, raw_actual - held)
```

而同一批改动里，`_usage_charge_micro` 专门捕获 `video_cost_exceeds_estimate`，docstring 明确写着：

> we still charge it in full per the pure-pass-through rule
> Falling back to `max(held, est)` here would make the platform silently eat the excess

然后 `return int(exc.actual_micro)` 把真实成本交给 core —— core 转手砍掉。

**实测验证**（复刻 `settle()` 余额计算段）:

```
hold=100µ，上游真实成本 5000µ
  worker 传入 actual_micro      = 5000
  实际记账 meta['actual_micro'] = 100
  未收取 unauthorized_micro     = 4900   ← 平台吸收
  钱包余额变化 amount_micro     = 0
  lifetime_spend_micro          = 100   （真实消费 5000）
```

**影响**:
1. 违反「视频计费纯转嫁：只要上游扣费用户就必须付」；
2. `apps/worker/app/video_billing.py:155-163` 的 `logger.error(... "charging in full (pure pass-through)")` 会打出**与事实相反**的日志，掩盖资损；
3. `unauthorized_micro` 只落在 tx meta 里，没有任何追缴路径。

**注意**: `packages/core/tests/test_billing.py:951` 的 `test_settle_caps_charge_at_the_authorized_hold` 明确断言了封顶行为。修复时需一并决定该测试的去留。

---

## P0-2 `charge` 扣穿 + `settle` 新 409 = 负余额账户永久无法结算

**位置**: `packages/core/lumen_core/billing.py:842`（charge）｜ `billing.py:738-743`（settle）｜ `apps/worker/app/billing.py:568`（未捕获）

### 第一步：`charge` 现在无条件扣穿

删除 `cap_overdraw` 开关后：

```python
next_balance = wallet.balance_micro - amount
overdraw_micro = max(0, -next_balance) if not allow_negative else 0
wallet.balance_micro = next_balance      # ← 直接写入，可为负
```

实测：

```
balance=  100 charge= 5000 allow_negative=False -> balance_after= -4900
balance=    0 charge=  300 allow_negative=False -> balance_after=  -300
```

即使 `allow_negative=False`，余额照样为负 —— 该参数现在只影响 meta 里的 `overdraw_micro` 标记。

### 第二步：`settle` 的新 409 判据实际在检测「钱包本来是负的」

```python
balance_delta = held - actual        # actual 已被 min 到 held ⇒ 恒 >= 0
next_balance = wallet.balance_micro + balance_delta
overdraw_micro = max(0, -next_balance)
if overdraw_micro and not allow_negative:
    raise BillingError("SETTLEMENT_EXCEEDS_AUTHORIZATION", ..., 409)
```

因为 `balance_delta >= 0`，`next_balance < 0` 的**唯一**可能是 `wallet.balance_micro` 本身为负。实测：

```
balance=-100 held= 50 actual= 50 -> RAISE SETTLEMENT_EXCEEDS_AUTHORIZATION (409)
balance=-500 held=100 actual=100 -> RAISE SETTLEMENT_EXCEEDS_AUTHORIZATION (409)
balance=  -1 held= 10 actual=  1 -> OK (amount=9)
```

错误码语义完全错位：`actual` 已被封顶，「结算超出授权」在新逻辑下不可能发生。

### 第三步：`settle_generation` 不捕获 `BillingError`

`apps/worker/app/billing.py:568` 的 `billing_core.settle(...)` 调用不在任何 `try` 内（该文件 521 行的 `except BillingError` 只包住定价查询）。

### 完整故障链

```
用户被 charge 扣成负余额
    ↓
下次生图成功、图片已写入存储
    ↓
_persist_generation_success → settle_generation → settle() 抛 409
    ↓
异常穿过 _cleanup_storage_on_error（刚写的存储文件被删除）
    ↓
冒泡到 run_generation 的 except Exception → failure.handle_generation_exception
    ↓
decide_image_failure_billing 判 release（见 P1-4）→ hold 释放
```

**结果：用户拿不到图，平台吸收上游成本** —— 与本批改动的目标完全相反。

---

## P0-3 `BonusBillingReconciler` 单行数据异常瘫痪整个对账系统

**位置**: `apps/worker/app/reconciliation/bonus_billing.py:73` ｜ `apps/worker/app/reconciliation/coordinator.py:63`

新增的 reconciler 在批量循环里直接抛异常：

```python
if image is None:
    raise LookupError(f"billable bonus generation has no image: {generation.id}")
```

而 coordinator 把三个 reconciler 放进**同一个事务**，且 `BONUS_BILLING_RECONCILER` 注册在最后（`service.py:57-61`）：

```python
async with session.begin():
    for reconciler in reconcilers:          # GENERATION → COMPLETION → BONUS_BILLING
        aggregate.merge(await reconciler.reconcile(context))
```

它一抛，`session.begin()` 回滚 —— **前两个 reconciler 本轮的全部修复工作一起丢失**。异常继续向上（`coordinator.py:80` 只记 metric 后 `raise`）。

### 两条触发路径都很平常

1. **bonus generation 的图被删**：查询带 `Image.deleted_at.is_(None)`，而删图是软删除（`apps/api/app/images/application/http_routes.py:1314`）—— 用户删自己的图是完全正常的操作；
2. **P0-2 的 409**：`context.billing.settle_generation` 对负余额账户抛 `BillingError`，同样未捕获。

由于每轮 cron 都会重新扫到同一行坏数据，**对账系统会永久停摆**，超时任务的 hold 释放、outbox 投递全部停止。

**修复方向**: 单行异常应 `continue` + 计数告警，而不是 raise；或至少把 BonusBilling 移出共享事务。

---

# P1 级缺陷

## P1-4 `decide_image_failure_billing` 两个分支都返回 RELEASE，settle 分支是死代码

**位置**: `packages/core/lumen_core/upstream_billing.py:223-238` ｜ `apps/worker/app/tasks/generation_parts/failure.py:565-585`

实测全部输入的返回值：

```
direct_image_result_unknown   knowledge=unknown        action=release  released=True
image_job_result_unknown      knowledge=unknown        action=release  released=True
no_image_returned             knowledge=unknown        action=release  released=True
timeout                       knowledge=proven_absent  action=release  released=True
some_other_failure            knowledge=proven_absent  action=release  released=True
None                          knowledge=proven_absent  action=release  released=True
```

于是 `failure.py:583` 的这段永不执行：

```python
if decision.released:
    ...
    return
await g.billing.worker_billing.settle_generation_unknown_upstream(...)   # ← 死代码
```

**连带失效的新增代码**:
- `apps/worker/app/billing.py:799` `settle_generation_unknown_upstream`（图片路径上无调用者）
- `packages/core/lumen_core/constants.py` 新增的 `IMAGE_JOB_RESULT_UNKNOWN` 错误码
- `IMAGE_UPSTREAM_RESULT_UNKNOWN_CODES` 集合在计费决策上不产生任何差异（仅在 `retry.py:247` 的重试判定上仍有效）

**矛盾点**: 该函数绕过了同文件的决策表 `resolve_billing_action`（UNKNOWN → `SETTLE_DEFAULT`），也与模块 docstring 直接冲突：

> 只要上游有可能已经扣费，本地就必须结算（settle）而不是释放（release）

这正是 P0-1 同款矛盾在图片路径上的复现。

---

## P1-5 worker 的倍率解析未同步 core 修复，负倍率 = 该账号免费

**位置**: `apps/worker/app/billing.py:183-192`

core 新增了 `parse_rate_multiplier_x10000`（`billing.py:115`），其 docstring 明确批判旧写法：

> 早先的实现把负数夹到 0，等于让一条脏数据把该账号变成永久免费 —— 上游照扣，平台全额吸收，正是纯转嫁禁止的方向。

`apps/api/app/task_billing.py:80` 已切换到该函数。但 **worker —— 实际结算路径 —— 保留了被批判的实现**：

```python
return max(0, int(Decimal(str(value)) * 10_000))
```

实测对比：

| `billing_rate_multiplier` | core（api 侧） | worker（结算侧） |
|---------------------------|----------------|------------------|
| `1.0009` | 10009 | 10009 ✓ |
| `-1.0` | 10000（原价） | **0（免费）** |
| `-0.5` | 10000（原价） | **0（免费）** |
| `99999999` | 10000（越界回落） | 999999990000 |
| `1.00005` | 10001（ROUND_UP） | 10000（截断） |

修复只做了一半，且落在非关键路径上。

---

## P1-6 单次 Redis 抖动即取消正在运行的业务任务

**位置**: `apps/worker/app/locks/owned_redis.py:129`

续期循环的判据从 `renewed is False` 放宽到 `renewed is not True`：

```python
if renewed is not True:
    # ``False`` = ownership confirmed lost, ``None`` = Redis unreachable.
    ...
    if not holder_task.done():
        holder_task.cancel(f"owned redis lock lost: {key}")
    return
```

而 `renew_owned_lock`（同文件 `:78-90`）对**任何异常**都 `return None`：

```python
except Exception:
    ...
    log.warning("redis lock renew failed ...")
    return None
```

**后果**: 一次网络超时 / 短暂抖动 → 直接 `cancel()` 掉正在跑的对账或 outbox 任务，尽管此时锁还有约 2/3 TTL 的有效期（续期间隔是 `ttl_s / 3`）可用于重试。

**不对称性**: 同一批改动给 release 路径加了 `RELEASE_RETRY_DELAYS_S = (0.05, 0.2)` 重试，renew 路径却一次都不重试。合理的做法是先重试续期，连续失败到逼近 TTL 才取消 holder。

---

# P2 级缺陷

## P2-7 image-job 流式响应上限在重构中丢失下界

**位置**: `image-job/image_job/config.py:179-183`

重构成 `_int_env` 辅助函数时丢掉了外层下界：

```python
# 改动前
responses_stream_max_bytes=max(
    max_image_bytes,                                   # ← 下界
    int(env.get("IMAGE_JOB_RESPONSES_STREAM_MAX_BYTES", ...)),
),

# 改动后
responses_stream_max_bytes=_int_env(
    env, "IMAGE_JOB_RESPONSES_STREAM_MAX_BYTES",
    max(max_image_bytes * 2, 64 * 1024 * 1024),        # 仅是 default，非下界
),
```

实测：设 `IMAGE_JOB_RESPONSES_STREAM_MAX_BYTES=1000` 得到 `1000`，而 `max_image_bytes = 83886080` —— 流上限小于单图上限，任何正常大小的图都会被流层截断。

**同批的兄弟字段都保留了下界**（`max_total_image_bytes`、`max_upstream_response_bytes`），唯独这一个漏了，属重构疏漏而非有意变更。

---

## P2-8 `lifetime_spend_micro` 语义倒退

**位置**: `packages/core/lumen_core/billing.py:746`

改动删除了这段注释：

```
lifetime_spend_micro tracks gross consumed service cost. Overdraw remains
visible in transaction metadata, but spend analytics must not hide it.
```

而 `wallet.lifetime_spend_micro += max(0, actual)` 中的 `actual` 现在是封顶后的值。结果正是原注释要防止的：**消费统计隐藏了真实的上游成本**。P0-1 场景下真实消费 5000µ，统计只记 100µ。

---

# 运维语义变更（非缺陷，需确认）

## Redis 淘汰策略从 `allkeys-lru` 改为强制 `noeviction`

**位置**: `deploy/redis/redis-entrypoint.sh:32-36`

```bash
REDIS_MAXMEMORY_POLICY="${REDIS_MAXMEMORY_POLICY:-noeviction}"
if [ "$REDIS_MAXMEMORY_POLICY" != "noeviction" ]; then
    echo "... Only noeviction is allowed." >&2
    exit 1
fi
```

理由（保护 task lease 不被驱逐）成立。已确认 `.env.example` 中无此变量，现有部署不会因此启动失败。

但内存打满时的行为从「驱逐旧 key」变为「**所有写入报错**」—— 队列入队、事件发布、锁获取会同时失败。上线前需确认：
1. `REDIS_MAXMEMORY` 容量相对实际用量有足够余量；
2. 内存水位有告警，且告警阈值远早于打满；
3. 有非驱逐的清理机制（TTL 覆盖所有大对象 key）。

---

# 已验证、确认无问题的改动

以下改动经实际执行验证，未发现缺陷：

| 改动 | 验证方式 | 结果 |
|------|----------|------|
| `image_artifacts._thumbnail_size` 重写 PIL thumbnail 逻辑 | 1512 组尺寸穷举对比 `Image.thumbnail` | 尺寸 100% 一致 |
| `_scaled_for_variant` 的 `yield orig` 分支（不 copy） | 实跑 preview/display/thumb 三条路径 | `convert()` 恒返回新对象，orig 不会被误关 |
| `useSSE` 改用 `useEffectEvent` | 检查 `react@19.2.4` 导出 | development / production 均为稳定导出 |
| `next build --webpack` | 检查 `next@16.2.4` CLI 定义 | `--webpack` 是官方支持的标志 |
| `build_redis_settings` 设置 retry 相关字段 | 检查 arq `RedisSettings` dataclass 字段 + `create_pool` 用法 | 字段存在且被真实使用 |
| workflow 路由迁移到 `workflow_services/project_endpoints.py` | 正则对比 HEAD 与工作区全部 `@router` 装饰器 | 13 → 26 条，**无丢失** |
| `scripts/restore.sh` 新增进程替换语法 | `bash -n` 语法检查 | 通过（shebang 是 `#!/usr/bin/env bash`） |
| `pricing_values` 改用 `parse_float=Decimal` | 追踪下游消费点 | 仅流向 `_openai_price_micro`（`Decimal(str(...))` 包装）与 note 文本，无 JSON 序列化风险 |
| `BrowserEventSourceTransport` 改为单流 | 检查实例化点 | 每个 `RealtimeRuntime` 独占一个 transport，无跨流互踢 |
| `redemptions.py` 把 `now` 移到 `FOR UPDATE` 之后 | 代码审读 | 修复正确，消除了锁等待期的过期窗口 |

---

# 修复建议顺序

P0-1 / P0-2 / P0-3 / P1-4 咬合成一条链，**必须先做一个决策**，否则会反复来回改：

> ### 决策点：当上游真实成本 > hold 时，超出部分谁承担？

| 若选 **B（纯转嫁，用户承担）** | 若选 **A（hold 为硬上限，平台承担）** |
|---|---|
| 删除 `billing.py:733` 的 `min()` | 删除 `video_billing.py:133-165` 的异常捕获与误导性日志 |
| 删除 `SETTLEMENT_EXCEEDS_AUTHORIZATION` 分支 | 保留 `min()`，但把 `lifetime_spend` 改回记 `raw_actual` |
| `decide_image_failure_billing` 改为走决策表（UNKNOWN → SETTLE） | 删除 `settle_generation_unknown_upstream` 及 `IMAGE_JOB_RESULT_UNKNOWN` 死代码 |
| 更新 `test_settle_caps_charge_at_the_authorized_hold` | 更新 `upstream_billing.py` 模块 docstring 的「铁律」表述 |
| 为 `unauthorized_micro` 建立追缴流程 | 为超额部分建立平台成本监控 |

**与 `2026-07-26-deep-bug-audit.md` 的记录冲突**：该文档「修复后复核」表把 `settle() 可突破 hold` 与 `上游成功但图片交付失败仍向用户扣费` 都列为**已修复**，采用的正是 A 方向；但同一批改动的 worker 侧与 `upstream_billing.py` docstring 采用 B 方向。两份修复来自不同判断，需要统一。

无论选哪条路，以下三项独立于该决策，可立即修复：

1. **P0-3** —— `bonus_billing.py:73` 的 `raise` 改 `continue` + 告警计数（阻断级，且与决策无关）；
2. **P1-5** —— worker `_rate_multiplier_x10000` 改调 `parse_rate_multiplier_x10000`（一行）；
3. **P2-7** —— 恢复 `responses_stream_max_bytes` 的 `max(max_image_bytes, ...)` 下界（一行）。

P1-6 建议给 renew 加上与 release 对称的重试，仅在连续失败逼近 TTL 时才 cancel holder。
