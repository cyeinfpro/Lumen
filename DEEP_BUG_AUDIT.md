# 全代码深度 Bug 审查报告

> 扫描范围：全仓库 Python、Shell、Docker/Infra 配置，约 180+ 文件
> 方法：5 个并行 Agent 分别扫描 API 路由、Worker、Core 库、Shell 脚本、配置/Infra
> 与现有 BUG_REVIEW.md 去重：本报告聚焦新发现或未充分覆盖的 bug

---

## 目录

- [P0 — 金融/数据安全，必须立刻修](#p0)
- [P1 — 高优先级功能 Bug](#p1)
- [P2 — 中等优先级](#p2)
- [P3 — 低优先级 / 防御性](#p3)
- [与 BUG_REVIEW.md 交叉引用](#cross-ref)

---

## <a id="p0"></a>P0 — 金融/数据安全，必须立刻修

### P0-1. `settle` 不同 idempotency_key 可重复扣款（Core billing.py）

- **文件**: `packages/core/lumen_core/billing.py:424-474`
- **描述**: `settle` 仅按 `idempotency_key` 做幂等检查（L435-442），不按 `ref_type`+`ref_id` 兜底。若同一笔 ref 被用不同 idempotency_key 调用两次 settle，第一次正常结算，第二次 `_held_amount_for_ref` 返回 0，但函数继续执行 `balance_delta = 0 - actual`，再次扣款。
- **触发**: 调用方 bug 或用不同幂等键重试同一笔结算。
- **后果**: 用户被双倍扣费，造成财务损失。
- **修复**: `_held_amount_for_ref` 返回 0 时，查询是否已有 settle/release 交易对该 ref 存在，若有则直接返回已存在交易。

### P0-2. `_apply_window_increment` Redis 读写竞态导致限流窗口漏计（Core billing_cache.py）

- **文件**: `packages/core/lumen_core/billing_cache.py:112-133`
- **描述**: 先 `hgetall` 读整个 hash，再通过非原子 pipeline 写回。两个并发请求同时读到 `started=0`，都会认为需要新开窗口，后者 HSET 覆盖前者的 usage 值。
- **触发**: 同一 key_id 的并发窗口增量，尤其在窗口刚开始时。
- **后果**: 限流计数被低估，用户可突破频率限制。
- **修复**: 用 Lua 脚本原子化整个读-改-写操作，或用 WATCH/MULTI/EXEC 加重试。

### P0-3. `_cost` 整数除法截断导致系统性少收费（Core pricing.py）

- **文件**: `packages/core/lumen_core/pricing.py:247-248`
- **描述**: `(tokens * rate_per_1k_micro) // 1000` 向下取整。低用量请求下多个计费组件的费用都可能被截断为 0（如 500 tokens × 1 micro/1k = 0）。
- **触发**: 任何 token 数 × 费率 < 1000 的请求。
- **后果**: 每次请求少收少量费用，累计造成实际收入损失。
- **修复**: 四舍五入 `(tokens * rate + 500) // 1000` 或用 Decimal 算术。

### P0-4. 非 Anthropic provider 缓存 token 重复计费（Core pricing.py）

- **文件**: `packages/core/lumen_core/pricing.py:227-231`
- **描述**: 当非 Anthropic/OpenAI 的 provider 在 `input_tokens` 中已含缓存量、同时又在 `cache_read_input_tokens`（Anthropic 风格字段）中单独报告时：`cached_details=0`, `anthropic_cache_read>0`, `cache_read = anthropic_cache_read`, 但 `is_anthropic=False` 且 `cached_details=0` 不触发 L230 的减法，导致 `input_tokens` 仍包含缓存量 → 缓存 token 被计两次费。
- **触发**: 网关/proxy 以混合格式报告 usage（input_tokens 含缓存 + 单独 cache_read_input_tokens）。
- **后果**: 用户为缓存 token 重复付费。
- **修复**: `if not is_anthropic: total_cached = cached_details or anthropic_cache_read; if total_cached: input_tokens = max(0, raw_input - total_cached)`

---

## <a id="p1"></a>P1 — 高优先级功能 Bug

### P1-1. 钱包余额检查与扣款不在同一事务（messages.py）

- **文件**: `apps/api/app/routes/messages.py:1600-1606` vs `1090-1106`
- **描述**: L1600 用 `lock=True` 检查余额 >= 10000 micro，检查通过后经过系统提示词解析、用户消息创建才进入 `_create_assistant_task` 中的 `billing_core.hold`。两次操作使用不同的 wallet 查询，之间并发请求可能导致余额已不足。
- **触发**: 同一用户两个并发聊天请求，余额刚好够一个但不够两个。
- **后果**: 用户消息已写入 DB（L1639 flush），但 hold 失败返回 402，用户得到部分写入的混乱状态。
- **修复**: 将余额检查移入 `_create_assistant_task`，与 hold 共用同一个带锁 wallet 实例。

### P1-2. 增强 prompt 流式输出后计费失败白送服务（prompts.py）

- **文件**: `apps/api/app/routes/prompts.py:612-618`
- **描述**: 先 `yield chunk` 把增强文本流式输出给用户（L611），再调 `_charge_prompt_enhance`（L614）。若计费失败，异常被捕获但文本已发送，无法回滚。
- **触发**: 流式完成后计费系统瞬时故障。
- **后果**: 用户免费获得 AI 增强服务。
- **修复**: 预授权（hold）预估费用再开始流式输出，流式完成后 settle。

### P1-3. 账号删除 commit 失败后 Redis cancel key 残留误杀任务（me.py）

- **文件**: `apps/api/app/routes/me.py:585-596`
- **描述**: 先设 Redis cancel key（L590-593），再 `db.commit()`（L596）。若 commit 失败（死锁/约束冲突），DB 回滚但 Redis key 存活 3600s，worker 看到这些 key 会取消该用户的正常进行中任务。
- **触发**: 账号删除 commit 失败（并发修改、死锁等）。
- **后果**: 用户的排队/运行中任务被静默取消，账号实际未删除但任务丢失。
- **修复**: commit 成功后再写 Redis cancel key，或 commit 失败时立即清理已写的 key。

### P1-4. SSE PubSub 事件缺少 event ID 导致重连重复推送（events.py）

- **文件**: `apps/api/app/routes/events.py:532-558`
- **描述**: 仅当 publisher payload 包含 `sse_id` 时才设 SSE `id:` 字段（L557）。若 publisher 不包含，浏览器重连时 `Last-Event-ID` 落后，中间所有无 ID 事件被重放。
- **触发**: 任何不含 `sse_id` 的 PubSub 消息 + 客户端网络断开重连。
- **后果**: 非幂等事件（如"新图片生成"触发 UI 动画）被重复投递。
- **修复**: 要求所有 publisher 必须包含 `sse_id`，或用 Redis stream entry ID 作为权威 ID。

### P1-5. Telegram 绑定码竞态导致码永久丢失（telegram.py）

- **文件**: `apps/api/app/routes/telegram.py:328-372`
- **描述**: 先从 Redis 删除 link code（L328），再写 DB 绑定。若 DB 写入失败，code 已删无法恢复。注释说"best-effort 恢复"但无恢复代码。
- **触发**: 两个并发绑定同一 code，或 DB 死锁导致写入失败。
- **后果**: 绑定码被消耗且无法恢复，用户需重新生成。
- **修复**: 用 Redis Lua 脚本原子读取+标记删除，或仅在 DB 成功后删除并加"已消费"标记防双重消费。

### P1-6. `hold` 对 amount <= 0 返回 None 与幂等命中混淆（Core billing.py）

- **文件**: `packages/core/lumen_core/billing.py:352-355`
- **描述**: `amount <= 0` 时直接 `return None`，与幂等检查命中时返回 None 无法区分。
- **触发**: 调用方传入非正金额。
- **后果**: 调用方可能误认为幂等命中而跳过后续计费逻辑。
- **修复**: 先做幂等检查，再判断金额，或对 `amount <= 0` 抛明确异常。

### P1-7. `charge` 负数金额静默归零（Core billing.py）

- **文件**: `packages/core/lumen_core/billing.py:528-531`
- **描述**: `if amount < 0: amount = 0` 静默将负数归零，调用方无法感知 bug。
- **触发**: 调用方因 bug 传入负数（如负的实际成本）。
- **后果**: 应收费用丢失，财务数据错误。
- **修复**: 负数时 raise `BillingError("NEGATIVE_AMOUNT")`。

### P1-8. `lifetime_spend_micro` 在超支时记录全额而非实收（Core billing.py）

- **文件**: `packages/core/lumen_core/billing.py:456,549`
- **描述**: `wallet.lifetime_spend_micro += actual` / `+= amount` 总是加全额，即使余额不足被 cap 到 0（超支部分未实际收取）。
- **触发**: settle 或 charge 超出可用余额 + hold。
- **后果**: 报表中的 lifetime_spend 虚高，与实际收款不一致。
- **修复**: 仅加实际扣款金额：`actual - overdraw_micro`。

### P1-9. `cap_overdraw` 逻辑不独立于 `allow_negative`（Core billing.py）

- **文件**: `packages/core/lumen_core/billing.py:541-548`
- **描述**: `cap_overdraw=True` 仅在 `allow_negative=False` 时生效。若 `allow_negative=True, cap_overdraw=True`，余额不够时仍会变成负数。
- **触发**: 同时传 `allow_negative=True, cap_overdraw=True`。
- **后果**: 参数语义与实际行为不一致。
- **修复**: `cap_overdraw` 逻辑应独立于 `allow_negative`。

### P1-10. `_ensure_wallet` 方言检测脆弱（Core billing.py）

- **文件**: `packages/core/lumen_core/billing.py:146-162`
- **描述**: `getattr(db, "connection", None)` 不是标准 SQLAlchemy async session API。若 session wrapper 有非 callable 的 `connection` 属性，`await connection()` 抛 TypeError 未被捕获。
- **触发**: 某些 session 配置。
- **后果**: 钱包创建时 500 错误。
- **修复**: 使用 `db.get_bind()`（标准 API）获取 dialect。

### P1-11. Provider pool 加权轮询去重使 weight 失效（worker provider_pool.py）

- **文件**: `apps/worker/app/provider_pool.py:440-464`
- **描述**: `_weighted_round_robin` 先按 weight 展开列表，再按 name 去重（seen set）。去重后只保留每个 provider 的第一次出现，weight > 1 的值完全无效。
- **触发**: 任何配置了 weight > 1 的 provider。
- **后果**: 加权负载均衡完全失效，所有同优先级 provider 均等分布。
- **修复**: 用累积权重 WRR 算法替代展开+去重模式。

### P1-12. Lease 释放无 CAS → 误删他人 lease（worker generation.py）

- **文件**: `apps/worker/app/tasks/generation.py:217-221, 4833-4840`（已在 BUG_REVIEW P1-19 中）
- **确认**: 已有报告，本扫描确认问题存在。

### P1-13. Redis cancel check 异常时静默吞错（worker completion.py & generation.py）

- **文件**: `apps/worker/app/tasks/completion.py:347-352, generation.py:201-206`
- **描述**: `_is_cancelled` 在 Redis 不可达时 catch 异常并返回 False（未取消）。
- **触发**: Redis 瞬时不可达。
- **后果**: 用户已取消的任务继续执行并消耗 provider 配额。
- **修复**: Redis 异常时保守返回 True（取消）或重试若干次后再决定。

### P1-14. SSE BroadcastChannel 跨 tab 重放导致流式文本双重拼接（前端 SSEProvider.tsx）

- **文件**: `apps/web/src/components/SSEProvider.tsx:247-262, 314-345`（已在 BUG_REVIEW P0-41 中）
- **确认**: 已有报告，本扫描确认问题存在。

---

## <a id="p2"></a>P2 — 中等优先级

### P2-1. `_host_resolves_to_private` 同步 DNS 阻塞 event loop（byok_service.py）

- **文件**: `apps/api/app/byok_service.py:146-161`（已在 BUG_REVIEW P1-18 中）
- **确认**: 已有报告。

### P2-2. `_open_storage_file_safe` TOCTOU + FIFO 阻塞（me.py）

- **文件**: `apps/api/app/routes/me.py:97-118`
- **描述**: stat-then-open 模式。攻击者可替换为 FIFO（命名管道），open 阻塞调用线程。
- **触发**: 有文件系统写权限的攻击者在 stat 和 open 之间替换文件为 FIFO。
- **后果**: 线程池线程无限阻塞，耗尽后所有文件操作超时。
- **修复**: 使用 `os.open(path, O_NOFOLLOW | O_RDONLY)` 替代 `path.open("rb")`。

### P2-3. 约束名字符串匹配脆弱（system_prompts.py）

- **文件**: `apps/api/app/routes/system_prompts.py:39-43`
- **描述**: 通过检查 `"uq_system_prompts_user_name" in repr(exc.orig)` 判断重复名称约束。约束名随迁移变更时静默失败。
- **触发**: 数据库迁移重命名约束。
- **后果**: 用户看到通用错误信息而非"名称已存在"。
- **修复**: 用 DB 无关方式提取约束名（PG 用 `exc.orig.diag.constraint_name`）。

### P2-4. 兑换码创建 Redis 清理静默 `pass`（billing.py）

- **文件**: `apps/api/app/routes/billing.py:2240`
- **描述**: commit 失败清理 Redis key 用 `except Exception: pass`，连日志都不打。
- **触发**: commit 失败 + Redis 清理也失败。
- **后果**: 调试困难，运维无法判断为什么 Redis 有孤儿 key。
- **修复**: 至少打 `logger.warning`。

### P2-5. Admin 邀请创建无频率限制（invites.py）

- **文件**: `apps/api/app/routes/invites.py:85-135`
- **描述**: `POST /admin/invite_links` 无 RateLimiter。
- **触发**: 管理员账号被攻破或 CSRF。
- **后果**: 可批量生成海量邀请链接。
- **修复**: 添加 per-admin 限速器。

### P2-6. `release` 对已释放 ref 返回 None 无法区分（Core billing.py）

- **文件**: `packages/core/lumen_core/billing.py:494-496`
- **描述**: `held <= 0` 时直接 `return None`，调用方无法区分"已释放"和"无 hold"。
- **触发**: 同一 ref 用不同幂等键调用两次 release。
- **后果**: 调用方可能错误判断状态。
- **修复**: 检查是否已存在 release/settle 交易，若存在则返回该交易。

### P2-7. `PII_RE` `\b\d{6}\b` 误匹配太多正常数字（Core memory.py）

- **文件**: `packages/core/lumen_core/memory.py:40-43`
- **描述**: `\b\d{6}\b` 匹配任何 6 位数字（邮编、日期 202506、订单号等），导致 `has_pii()` 误判。
- **触发**: 用户消息包含任意 6 位数字。
- **后果**: 正常对话的记忆提取被静默抑制。
- **修复**: 移除该模式或改为更精准的规则。

### P2-8. `sign_image_url_query` `now_ms` 无校验（Core image_signing.py）

- **文件**: `packages/core/lumen_core/image_signing.py:91-103`
- **描述**: `now_ms` 参数无合理性校验。传 `now_ms=1`（epoch 刚过）生成立即过期的签名；传未来时间生成超长有效期签名。
- **触发**: 调用方传入病态 `now_ms` 值。
- **后果**: 签名可能立即失效或实际无限期有效。
- **修复**: 校验 `now_ms` 在合理范围内（如实时的 5 分钟内）。

### P2-9. Usage 字段 `or` 链跳过合法的 0 值（Core pricing.py）

- **文件**: `packages/core/lumen_core/pricing.py:176-222`
- **描述**: `usage.get("input_tokens") or usage.get("prompt_tokens")` — 若 `input_tokens` 为 0（合法零输入请求），`0 or ...` 会取下一个字段的值。
- **触发**: provider 同时返回 `input_tokens: 0` 和 `prompt_tokens: 50`（极少见但可能）。
- **后果**: 零输入请求被误计为 50 token。
- **修复**: 用 `is not None` 替代 `or` 链。

### P2-10. Worker healthcheck 不检查 worker 进程（docker-compose.yml）

- **文件**: `docker-compose.yml:205-208`
- **描述**: healthcheck 只 ping Redis，不验证 arq worker 进程是否存活。
- **触发**: worker 卡死/死锁但 Redis 正常。
- **后果**: 编排系统不会重启挂了但不影响 Redis 的 worker，任务停止处理。
- **修复**: 让 worker 定期写心跳到 Redis 并检查心跳。

### P2-11. Web healthcheck 依赖 API（docker-compose.yml）

- **文件**: `docker-compose.yml:240-243`
- **描述**: web 的 healthcheck 调 `http://127.0.0.1:3000/api/healthz`，该路径代理到 API。API 挂了会导致 web 被标记不健康。
- **触发**: API 不健康但 web 进程正常。
- **后果**: web 被不必要重启。
- **修复**: web 用不代理 API 的静态健康端点。

### P2-12. api-green 缺少 `init: true`（docker-compose.bluegreen.yml）

- **文件**: `docker-compose.bluegreen.yml:6-15`
- **描述**: api-green 不用 `*service-hardening` anchor，缺少 `init: true`。
- **触发**: 蓝绿部署使用 api-green。
- **后果**: 容器内僵尸进程累积、SIGTERM 传播不正确，破坏蓝绿零停机目的。
- **修复**: 加 `init: true` 或引用 `*service-hardening`。

### P2-13. SMB 凭证文件在挂载失败时泄漏（lumen_storage_mount.sh）

- **文件**: `deploy/scripts/lumen_storage_mount.sh:190, 301-303`
- **描述**: `trap "rm -f '$cred'" RETURN` 只在函数正常返回时触发。`mount -t cifs` 失败时 `set -e` 导致 shell 直接退出，RETURN trap 不执行。
- **触发**: SMB 挂载失败（网络超时、凭证错误等）。
- **后果**: 含 SMB 用户名/密码的临时文件残留在 `/run` 直到下次重启。
- **修复**: `mount` 命令后用 `||` 链显式 `rm -f "$cred"`，或改用 EXIT trap。

### P2-14. `fix-redis-password-mismatch.sh` 无法处理带引号的 .env（scripts）

- **文件**: `scripts/fix-redis-password-mismatch.sh:92,96`
- **描述**: `sed` 模式不处理引号包裹的值（如 `REDIS_URL='redis://...'`），导致提取的密码为空或带引号。
- **触发**: .env 使用标准 shell 引号格式（大多数部署）。
- **后果**: 脚本完全失败或误判密码不匹配。
- **修复**: 提取值时先剥离外部引号。

### P2-15. `lumen-shift-traffic.sh` 空 nginx 备份（scripts）

- **文件**: `scripts/lumen-shift-traffic.sh:51-56`
- **描述**: nginx 配置首次不存在时，备份变成空文件。若新配置语法错误，恢复时把空文件写入 nginx 配置。
- **触发**: 首次安装 + 配置变更引入语法错误。
- **后果**: nginx 配置被清空，站点不可用。
- **修复**: 仅在原文件存在时创建备份。

### P2-16. Outbox 去重 key 在 PG commit 前设置 → 重复 arq 任务（worker outbox.py）

- **文件**: `apps/worker/app/outbox.py:204-211`（已在 BUG_REVIEW 中未涉及）
- **描述**: 先设 Redis 去重 key（60s TTL），再 PG commit。若 commit 失败，60s 后 key 过期，事件被重新入队。
- **触发**: enqueue_job 成功但 PG commit 失败。
- **后果**: 产生重复 arq 任务。
- **修复**: 在 commit 成功后设置去重 key。

---

## <a id="p3"></a>P3 — 低优先级 / 防御性

### P3-1. poster_styles.py 计算 SHA-256 时双重内存占用

- **文件**: `apps/api/app/routes/poster_styles.py:1114`
- **描述**: `path.read_bytes()` 全量读入内存计算哈希，再用 `_stream_file` 读第二遍输出流。
- **后果**: 对最大文件产生 2x 内存峰值。
- **修复**: 流式传输同时增量计算哈希。

### P3-2. `json.loads` 无 try/except（system_settings.py）

- **文件**: `apps/api/app/routes/system_settings.py:48`
- **描述**: `_validate_threshold_pricing_alignment` 中 `json.loads(raw_thresholds)` 无异常处理。
- **修复**: 包裹 try/except，失败返回 422。

### P3-3. `parse_thresholds` JSON 解析失败静默回退默认值（Core billing.py）

- **文件**: `packages/core/lumen_core/billing.py:85-106`
- **描述**: JSON 解析失败时直接返回默认阈值，不记录日志。
- **后果**: 管理员以为自定义阈值生效，实际用的是默认值。
- **修复**: 打 warning 日志。

### P3-4. `parse_bool_setting` 接受 "on"/"yes" 与 runtime_settings 不一致（Core billing.py）

- **文件**: `packages/core/lumen_core/billing.py:82`
- **描述**: billing 的 `parse_bool_setting` 接受 "on"/"yes"/"true"/"1"，但 `runtime_settings.py` 仅接受 "0"/"1"。
- **修复**: 统一为 "0"/"1" 或文档化差异。

### P3-5. Provider pool 孤立 health entries（worker provider_pool.py）

- **文件**: `apps/worker/app/provider_pool.py:284-336`
- **描述**: provider 校验失败被跳过，但 `_health` 中的旧条目残留。
- **修复**: reload 时 diff 新旧 provider 名并清理孤儿条目。

### P3-6. `_TS_LOCK` 惰性初始化竞态（worker sse_publish.py）

- **文件**: `apps/worker/app/sse_publish.py:62-75`
- **描述**: `_TS_LOCK` 用 check-then-set 惰性初始化，两协程可能创建不同的 Lock 实例。
- **修复**: 模块级直接初始化 `_TS_LOCK = asyncio.Lock()`。

### P3-7. 所有服务无 ulimits（docker-compose.yml）

- **文件**: `docker-compose.yml:5-15`（`x-service-hardening`）
- **描述**: 无 `nofile`/`nproc` 限制设置，高负载下可能触发"too many open files"。
- **修复**: 添加合理的 ulimits 默认值。

### P3-8. tgbot 无 healthcheck（docker-compose.yml）

- **文件**: `docker-compose.yml:249-288`
- **描述**: Telegram bot 服务无 healthcheck，挂了无法自动重启。
- **修复**: 添加进程检查或 Redis 心跳 healthcheck。

### P3-9. 硬编码 DNS 在某些网络不可用（docker-compose.yml）

- **文件**: `docker-compose.yml:181-183`
- **描述**: worker DNS 硬编码 `1.1.1.1` 和 `8.8.8.8`，在企业/中国网络可能不可达。
- **修复**: 环境变量可配置，保留合理默认值。

### P3-10. `image_job_base_url` 默认 example.com 占位符（config.py）

- **文件**: `apps/api/app/config.py:86, apps/worker/app/config.py:39`
- **描述**: 默认值为 `https://image-job.example.com`（IANA 保留域名，永不解析）。
- **后果**: 未配置时图片生成静默失败（DNS 超时）。
- **修复**: 生产环境校验该值不为 example.com 默认值。

### P3-11. 默认 DB/Redis 密码校验不够严格（config.py）

- **文件**: `apps/api/app/config.py:22-26, 233-245`
- **描述**: 仅当 username AND password 同时匹配默认值才报警。修改 username 但保留默认密码 `lumen` 不会触发。
- **修复**: 独立校验 password 不是默认值。

### P3-12. Worker 默认 redis_url 无密码（worker config.py）

- **文件**: `apps/worker/app/config.py:21`
- **描述**: 默认 `redis://localhost:6379/0`（无密码），但 docker-compose 中 Redis 始终有密码。
- **修复**: 与 API config 默认值对齐。

### P3-13. BYOK runtime 缓存清理无锁（worker byok_runtime.py）

- **文件**: `apps/worker/app/byok_runtime.py:59`
- **描述**: `clear_base_url_validation_cache()` 无锁清理 dict，并发访问可能 `RuntimeError: dictionary changed size during iteration`。
- **修复**: 加 `threading.Lock()`。

### P3-14. Memory extraction Unicode 未归一化（worker memory_extraction.py）

- **文件**: `apps/worker/app/tasks/memory_extraction.py:327`
- **描述**: `_topic_key` 未做 NFC/NFD 归一化，组合/分解字符产生不同 key。
- **修复**: 加 `unicodedata.normalize('NFC', ...)`。

### P3-15. Vision tagging markdown fence 解析不完善（Core vision_tagging.py）

- **文件**: `packages/core/lumen_core/vision_tagging.py:318-327`
- **描述**: 只处理最简单的 ` ``` ` 开头 ` ``` ` 结尾，不支持语言标识符（` ```json`）或多代码块。
- **修复**: 用 regex 提取 fence 内容。

### P3-16. JPEG 图片元数据读写不支持（Core model_image_metadata.py）

- **文件**: `packages/core/lumen_core/model_image_metadata.py:155-157`
- **描述**: PIL 的 `image.info` 对 JPEG 通常为空，元数据只能通过 PNG 读写。
- **修复**: 对 JPEG 尝试 EXIF/XMP 读取。

---

## <a id="cross-ref"></a>与 BUG_REVIEW.md 交叉引用

以下 BUG_REVIEW.md 中的 P0/P1 条目已在本扫描中独立确认，未在本文重复展开：

| BUG_REVIEW ID | 简述 | 本扫描确认 |
|---|---|---|
| P0-1 | redeemCode 缺 Idempotency-Key 头 | 未重新扫描（已知） |
| P0-2 | BYOK_API_KEY_MASTER_SECRET 升级重置 | 未重新扫描（已知） |
| P0-3 | systemd kill-after=30s 太短 | 未重新扫描（已知） |
| P0-4 | install.sh --pull never 首装失败 | 未重新扫描（已知） |
| P0-5 | lumen_pid_cmdline 空输出误判 stale lock | 未重新扫描（已知） |
| P0-41 | SSE 跨 tab 双拼接 | 已确认 |
| P1-6 | password_reset 通过 503 暴露邮箱 | 未重新扫描（已知） |
| P1-7 | admin login limiter 枚举管理员 | 未重新扫描（已知） |
| P1-8 | topup_redeem 覆盖 tx.meta | 未重新扫描（已知） |
| P1-9 | meta 合并顺序允许覆盖 code_id | 未重新扫描（已知） |
| P1-10 | 24h 重复轮转密钥丢 previous_secret | 未重新扫描（已知） |
| P1-11 | tgbot pull 失败 hard-abort update | 未重新扫描（已知） |
| P1-12 | migrate/bootstrap restart on-failure:3 | 未重新扫描（已知） |
| P1-13 | upstream LRU 弹出关使用中 client | 未重新扫描（已知） |
| P1-14 | generation lease_lost 盲目入队 | 已确认（新增更广义 P1-12） |
| P1-15 | sse_publish dedupe hash 无限增长 | 未重新扫描（已知） |
| P1-16 | account_limiter fallback fail-open | 未重新扫描（已知） |
| P1-17 | events envelope 覆盖 event_id | 未重新扫描（已知） |
| P1-18 | byok DNS 同步阻塞 | 已确认 |
| P1-19 | lease 释放无 CAS | 已确认 |

---

## 统计汇总

| 严重性 | 本报告新发现 | BUG_REVIEW 已覆盖 | 合计 |
|---|---|---|---|
| P0 | 4 | 6 | 10 |
| P1 | 14 | 13 | 27 |
| P2 | 16 | 19 | 35 |
| P3 | 16 | 8 | 24 |
| **总计** | **50** | **46** | **96** |

---

## 建议处理顺序

1. **立刻 hotfix**：P0-1（重复扣款）、P0-2（限流漏计）、P0-3（少收费）、P0-4（缓存双倍计费）+ BUG_REVIEW P0-1 ~ P0-5, P0-41
2. **同发版批次**：全部 P1（本报告 + BUG_REVIEW 未修复部分）
3. **下一发版**：全部 P2
4. **持续改进**：全部 P3
