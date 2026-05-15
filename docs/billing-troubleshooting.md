# 计费排障手册

| 报错 / 现象 | 含义 | 处理 |
|---|---|---|
| `REDEMPTION_SECRET_NOT_CONFIGURED` | 兑换码 secret 未配置 | Admin → 计费 → 定价 → 兑换码 secret |
| `BOOTSTRAP_INCOMPLETE` | 计费初始化未完成 | Admin → 计费 → 概览,执行初始化 |
| `BILLING_DISABLED` | 全局计费开关关闭 | Admin → 计费 → 定价,开启计费后再兑换 |
| `THRESHOLDS_PRICING_MISMATCH` | 尺寸阈值没有对应启用价格 | Admin → 计费 → 定价,补齐尺寸档位价格后再保存 |
| `CODE_NOT_FOUND` | code hash 不匹配或码不存在 | 检查输入格式; 若刚轮换 secret,旧码已失效 |
| `CODE_REVOKED` / `ALREADY_REVOKED` | 兑换码已被撤销 | 在兑换码列表按批次或前缀查询审计 |
| `CODE_EXPIRED` | 超过有效期 | 重新发码 |
| `CODE_EXHAUSTED` | 兑换次数用完 | 查看兑换记录确认使用者 |
| `CODE_ALREADY_USED` | 当前用户已兑过 | 用“兑换记录”核对 |
| `INSUFFICIENT_BALANCE` | 余额不足 | 兑换充值或管理员调账 |
| 有预扣不释放 | worker 任务失败后未 release | Admin → 计费 → 用户钱包,按 `hold` 过滤流水,再按 ref_id 查任务 |
| 重试后看不清是否重复扣费 | worker 命中幂等重放 | Admin → 计费 → 概览 → 最近审计,查 `wallet.*.replay` |

## 快速对账

运行:

```bash
python3 scripts/wallet_audit.py --report-json
```

若发现孤儿 hold,先确认对应任务状态,再人工 release 或调账。不要直接改 `user_wallets`。

## 监控指标

- `wallet_balance_total`: 钱包总余额,单位 micro RMB。
- `wallet_hold_active` / `wallet_hold_micro`: 当前有预扣的钱包数和金额。
- `wallet_orphan_holds`: 管理后台孤儿 hold 扫描发现的数量。
- `redemption_redeemed_total`: 兑换码成功充值次数。
- `wallet_overdrawn_total`: 透支补差次数,正常应为 0。
- `wallet_charge_lost_total`: 上游完成后扣费失败次数,正常应为 0。
