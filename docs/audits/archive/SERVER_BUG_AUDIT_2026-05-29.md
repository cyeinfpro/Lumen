---
baseline_commit: "b6e4004"
status: archived
resolved_by: null
superseded_by: null
---

> **归档状态**：历史报告，不代表当前代码。
>
> **审计基线**：`v1.1.71` / `b6e4004`。
>
> **记录提交**：`e5776aa53109cf73124ea509383ee54c995bd64a`。
>
> **索引**：[`docs/audits/README.md`](../README.md)。

# 服务端 Bug 审计（非客户端）— 2026-05-29

> 范围：`packages/core`、`apps/api`、`apps/worker`、`apps/web`、部署/脚本/CI、`apps/tgbot`。
> 方法：基于当前 `main @ b6e4004`（v1.1.71）源码**独立深挖新 bug**，不照搬旧文档。
> 性质：**只读审计**，仅记录问题，不改代码。
> 旧文档对照：`ISSUES.md`(2026-05-08, v1.0.43)、`BUG_REVIEW.md`(2026-05-16)、`DEEP_BUG_AUDIT.md`(2026-05-25)。每条标注是否与旧文档重叠。

严重度定义：
- **P0** 资金错误 / 鉴权绕过 / 数据丢失 / 线上崩溃，可被普通用户触发。
- **P1** 安全弱点 / 一致性破坏，触发条件受限（需特定状态或管理员）。
- **P2** 健壮性 / 资源泄漏 / 边界处理缺陷，影响有限。
- **P3** 代码异味 / 防御纵深缺口 / 仅在极端条件出现。

---

## 一、packages/core（计费 / 定价 / BYOK / SSRF / 并发）

> 总体结论：核心资金链路**经过良好加固**，旧文档点名的多条 P0 在当前代码已修复（见末尾“已修复/可排除”）。本轮新发现以中低严重度为主。

### CORE-1 (P3) `BillingCacheService._locks` 无界增长（内存泄漏）
- **位置**：`packages/core/lumen_core/billing_cache.py:82,85-90`
- **现象**：`_lock(key)` 为每个 `user_id` 惰性创建 `asyncio.Lock` 存入 `self._locks`，**永不回收**。
- **触发**：API 进程在 `get_balance`（`apps/api/app/routes/billing.py:917`）每请求按 `user_id` 取锁；进程生命周期内访问过的不同用户数 = `_locks` 条目数。
- **后果**：长生命周期 API 进程随累计独立用户数线性增长的常驻内存泄漏（每用户一个 Lock 对象 + dict 槽）。无上限驱逐。
- **修复方向**：用 `weakref`/LRU 上限，或在 `get_balance` 末尾按引用计数清理；或干脆改用 `redis` 分布式锁/不加锁（仅缓存语义）。
- **旧文档重叠**：未见旧文档记录（NEW）。

### CORE-2 (P3/P1*) SSRF 防护对 DNS rebinding 无效 + 解析失败放行
- **位置**：`packages/core/lumen_core/url_security.py:79-101`（`assert_public_http_target`）
- **现象**：
  1. 校验在**配置/凭证解析时**对 host 解析一次 IP 并判私网，但返回的是 **URL 字符串**（不固定 IP）。实际出站请求在 `apps/worker`/`upstream` 阶段**重新解析 DNS**。攻击者可在校验期把域名解析到公网 IP、在请求期 rebind 到 `169.254.169.254` / `127.0.0.1`（经典 DNS rebinding 绕过）。
  2. `byok_runtime.py:66-86` 还把校验结果**缓存 10 分钟**（`_BASE_URL_VALIDATION_TTL_SECONDS`），进一步拉长 rebinding 可用窗口。
  3. `url_security.py:86-88`：`getaddrinfo` 抛 `socket.gaierror` 时**无条件返回放行**（不看 `allow_unresolved`），与 `TimeoutError` 分支（受 `allow_unresolved` 约束）不一致；可用“校验期 NXDOMAIN、请求期才解析到内网”的域名绕过。
- **触发约束（关键）**：`base_url` 仅由**管理员**经 `apps/api/app/routes/byok.py:191/241`（`router_admin` + `AdminUser`）创建/修改；普通 BYOK 用户只绑定 key，不能设 base_url。故**非普通用户可触发**。
- **后果**：拥有 admin 的人可让 worker 出站打到云元数据/内网（admin 本就高权限，故实际为防御纵深缺口）。若未来开放用户自定义 supplier URL，则升级为可被普通用户触发的 P1 SSRF。
- **修复方向**：校验后**固定已解析 IP**并在出站时 pin（或用校验过的 IP 直连 + Host 头）；`gaierror` 分支也应遵循 `allow_unresolved`；缩短/取消校验缓存或缓存解析到的 IP。
- **旧文档重叠**：SSRF 类问题在 `ISSUES.md` 有泛化记录；本条的 **DNS-rebinding/解析失败放行的具体机制**为本轮独立定位。

### 已修复 / 可排除（独立复核旧文档点名的 P0，均已不成立）
- **DEEP_BUG_AUDIT P0-2「billing_cache `_apply_window_increment` Redis 读写竞态」** → 当前 `billing_cache.py:16-46,146-175` 已改为**单条 Lua 脚本 `redis.eval` 原子执行**（HGET/HSET/HINCRBY 全在脚本内），竞态消除。**不成立**。
- **DEEP_BUG_AUDIT P0-3「pricing 整数截断系统性少计费」** → 当前 `pricing.py:274-275` `_cost = (tokens*rate + 500)//1000` 为**四舍五入**（+500），非截断。**不成立**。
- **DEEP_BUG_AUDIT P0-1「settle 跨不同 idempotency_key 重复扣费」** → 当前 `billing.py:_existing_ref_consumption_tx`（core）+ `apps/worker/app/billing.py:194-214 _existing_fingerprint_tx` 双重防护；worker `charge_completion` 先 `complete:{ref}` 幂等预检、`settle` 内再持锁复检。**不成立**。
- BYOK 加解密（`byok.py`）：AES-GCM + HMAC 派生密钥、master_secret ≥32 字符校验、版本前缀、随机 12B nonce、`hmac.compare_digest` 验签均正确，未见可利用缺陷。
- 图片签名（`image_signing.py`）：HMAC-SHA256 截 96bit、`compare_digest`、UUID/variant 白名单、TTL≤30天、时钟偏移≤5min 校验完整，未见缺陷。

---

## 二、apps/api（路由 / 鉴权 / 中间件）

> 总体结论：鉴权与会话层**实现扎实**，未发现可被普通用户触发的越权 / CSRF / IDOR。以下为防御纵深观察。

### API-1 (P3) CSRF 为**逐路由 opt-in**，存在“新增写路由忘记加保护”的隐患
- **位置**：`apps/api/app/deps.py:162-203`（`verify_csrf`/`verify_csrf_session`），各路由 `dependencies=[Depends(verify_csrf)]`。
- **现象**：CSRF 不是全局中间件，而是每个写端点手动挂依赖。统计：写路由装饰器 **139** 个，`Depends(verify_csrf)` **123** 处——约 16 个写路由未显式挂 CSRF。
- **评估**：抽查未挂的多为 **auth 引导**（登录/注册/找回密码，尚无 session）、**bot 路由**（`X-Bot-Token` 鉴权）、**desktop 路由**（本地单用户）——这些**本就应豁免**。且生产环境 session cookie 为 `SameSite=Strict`（`auth.py:172`），跨站 POST 根本带不上 cookie，CSRF 在 cookie 层已被挡住；double-submit 为额外纵深。
- **后果**：当前配置下风险低；隐患在于**未来新增写路由若忘记挂 `verify_csrf` 且 SameSite 策略放宽**，会静默失去 CSRF 保护。
- **修复方向**：改为“默认全局 CSRF + 显式 opt-out 白名单”，或加 CI 测试断言所有非豁免写路由都挂了 `verify_csrf`。
- **旧文档重叠**：未见明确记录（NEW，低危）。

### API-2 (P3) `github_releases.py` 使用已弃用的 `datetime.utcnow()`（功能正确，仅弃用告警）
- **位置**：`apps/api/app/services/github_releases.py:104`
- **现象**：`published_at=datetime.utcnow().isoformat(timespec="seconds") + "Z"`。`datetime.utcnow()` 返回 naive datetime，在 Python 3.12+ 触发 `DeprecationWarning`，未来版本可能移除。
- **后果**：当前输出字符串（如 `2026-05-29T12:00:00Z`）**UTC 取值正确**，仅用于 channel=="main" 滚动 tag 的展示，**无功能 bug**；属代码异味 / 前向兼容隐患。
- **修复方向**：改用 `datetime.now(timezone.utc)` 并相应处理 isoformat 的 `+00:00`/`Z`。
- **旧文档重叠**：未见记录（NEW，最低危）。

> **跨模块同源**：`admin_update.py:1827`、`admin_release.py:323` 的 fire-and-forget `create_task`（marker 清理）与 worker 侧同根，详见 **WORKER-1**。

### 已复核为**安全**（非 bug，记录正面结论）
- **会话/Cookie**（`security.py` + `auth.py:167-217`）：argon2id 口令；session cookie 用 HMAC-SHA256 签 `{sid}.{exp}.{sig}`、exp 封顶 2³¹-1 防 Y2038 退化、`compare_digest` 防时序；生产 `Secure=True`+`SameSite=Strict`+`HttpOnly`；CSRF token 绑定 sid（强于裸 double-submit）；session 失效/吊销/用户删除均校验；失败有 IP 限流。
- **图片 IDOR / 签名端点**（`images.py`）：登录态查询一律带 `Image.user_id == user.id`；无登录的 `/_/sig/` 端点除 HMAC 验签外，**纵深要求该图必须挂在未吊销未过期的 Share 上**（`images.py:873-893`），signing secret 泄漏也无法拉取从未公开分享的私图；variant 绑定在签名内，无越权升级。
- **BYOK supplier base_url**：仅 `router_admin`+`AdminUser` 可建/改（见 CORE-2）。
- **Alembic 迁移链**：25 个迁移，单一 head（`0025_users_active_email_unique`），无分叉/无重复 revision，链完整。
- **admin 子进程**（`admin_update.py`/`admin_release.py`/`admin_backups.py`、`desktop_import.py`）：全部 **list 形式 argv**（非 `shell=True`），inline_script 为服务端构造、非用户输入；且全部 admin 限定。未见命令注入。
- **本轮新增全读的业务路由**（独立逐行复核，均未见越权 / IDOR / 注入）：
  - `invites.py`：建/列/吊销邀请均 admin 限定 + CSRF + 限流；`secrets.token_urlsafe(32)`；公开 preview 端点限流并校验有效性。
  - `generations.py`：feed 全部 `Generation.user_id`/`Conversation.user_id` 过滤；`_escape_like_pattern` 以 `escape="\\"` 转义 `\ % _`；游标解码有异常兜底。
  - `shares.py`：`_fs_path`（resolve + `relative_to(root)` + 拒绝绝对路径/`\x00`）+ `_open_storage_file_safe`（`O_NOFOLLOW` + stat/fstat dev/ino + `S_ISREG` 抗 TOCTOU）；公开端点强制 `_share_image_ids` 成员校验 + `Image.user_id` 同租户。
  - `regenerate.py`：归属链（conv→assistant msg→parent user msg）+ `_lookup_idempotent_regenerate` 预检 + `except IntegrityError` 重查处理并发竞态 + 附件 `Image.user_id` 过滤 + `with_for_update` 取消并释放 hold。
  - `me.py`：`export_my_data` 经 `asyncio.to_thread` 流式 zip + keyset 分页 + 归属过滤；`delete_my_account` 软删 + `with_for_update` + 释放 hold + 仅 commit 后写 Redis；`_open_storage_file_safe` 全程护栏。
  - `events.py`（SSE Hub）：`_validate_channels` 强制 user/conv/task 归属（跨用户 403）；`_sanitize_last_event_id` 校验攻击者头（长度/形态/时窗）；`try/finally` 包裹 pubsub，client 断开**故意不**写 cancel key（避免误杀在途生成）。

---

## 三、apps/worker（任务流水线 / 计费一致性 / 并发）

> 总体结论：资金链路与分布式锁均正确，且有审计回归测试守护；本轮仅新增 **1 条 P3**（任务生命周期，见 WORKER-1），无新 P0/P1/P2。

### WORKER-1 (P3) 多处 fire-and-forget `asyncio.create_task` 未保存引用（任务生命周期 / 优雅停机隐患）
- **位置**：`apps/worker/app/upstream.py:730,739,757,769`（`_delayed_aclose(old_client)`）；**跨模块同源**：`apps/api/app/routes/admin_update.py:1827`、`apps/api/app/routes/admin_release.py:323`（`_cleanup_marker_when_done(proc)`）。
- **现象**：这 6 处 `asyncio.create_task(...)` 的返回值**未被任何变量/集合持有**，也未挂 `add_done_callback`。Python 官方文档明确建议保存对 `create_task` 结果的引用——否则事件循环只持弱引用（`tasks._all_tasks` 为 `WeakSet`），任务理论上可能在执行中途被 GC。
- **独立复核（关键，避免高估）**：本仓其余**全部** `create_task`/`ensure_future` 站点均已正确持有引用（赋值给变量、加入 `set`/`list` 后经 `asyncio.wait`/`gather` 管理，或 `append` 进 `self._workers`）——已逐一核验 `generation.py:3637-3640`、`completion.py:1429-1433`、`context_image_caption.py:409-414`、`upstream.py:5912/6145/6380`、`billing_cache.py:102`。**仅上述 6 处**是真正的"发射后不管"。
- **真实后果（已收敛为 P3，非 P2）**：
  1. **"GC 中途回收"这一经典失败模式在此基本不成立**：三处协程体在其整个生命周期都挂起在"事件循环强持有"的 awaitable 上——`_delayed_aclose` 先 `await asyncio.sleep`（计时器进 `loop._scheduled`，强引用链 TimerHandle→future→task）再 `await client.aclose()`（IO）；`cleanup_marker_when_done` 全程挂在 `await asyncio.to_thread(proc.wait)`（executor future）。故任务不会在 sleep/wait 期间被回收。
  2. `_delayed_aclose` 自身 `try/except Exception` 兜底并 `logger.warning`，异常不会被静默吞掉。
  3. **残余真实隐患**：这些游离任务**无句柄供优雅停机时 await/cancel**。进程在任务 pending 期间 teardown，则被驱逐的 httpx client 可能未及关闭（`close_client()` 停机钩子也只管 `_client`/缓存中的 client，**管不到已驱逐、仅被游离任务持有的 client**；进程退出时 OS 回收 socket，影响很小）；admin 的 marker 清理任务若被"更新脚本重启 API 进程"打断则不执行（但 marker 清理是 pid 受限、幂等、best-effort，新进程会重读，影响很小）。
- **修复方向**：把任务存入模块级 `set` 并在完成回调里 `discard`（官方推荐写法），或纳入受 shutdown 管理的 registry/TaskGroup，使 `close_client()` 等停机钩子能 await 它们。
- **旧文档重叠**：未见记录（NEW，低危 / 防御纵深）。

### 已复核为**正确**（非 bug，记录正面结论）
- **结算/扣费幂等**（`apps/worker/app/billing.py`）：`charge_completion`/`settle_generation` 先按 `complete:{ref}`/`settle:{ref}` 幂等预检，`billing_core.settle` 内再**持 `with_for_update` 锁复检**幂等；另有 `_existing_fingerprint_tx` 跨 idempotency_key 指纹去重。并发双跑只会让第二次变 replay，不会重复扣费。
- **余额/窗口缓存回写**：`flush_balance_cache_refreshes` 在 completion/generation/outbox 共 **13 处** commit 后调用，且 `test_bug_audit_worker_regressions.py` 断言关键分支都挂了 flush——窗口限流不会静默失效。
- **分布式锁**（`generation.py:265-308,429-432`）：`_RELEASE_LEASE_LUA`/`_RENEW_LEASE_LUA` 均为 **owner-token CAS**（`if GET==token then DEL/EXPIRE`），不会误删他人锁；`_RESERVE_IMAGE_SLOT_LUA`、`_ACQUIRE_LUA` 信号量为单脚本原子操作。
- **BYOK 出站**：decrypt 失败专属 error_code，不污染 credential 状态；rate_limited 与 invalid 分类清晰。
- **限流**（`account_limiter.py`，本轮全读）：`_CHECK_WINDOW_LUA`+`_RECORD_IMAGE_CALL_LUA` 均单脚本原子；Redis 异常 **fail-closed**；check/record 分离存在**已知且受控**的 TOCTOU（超额上限 ≤1，设计权衡）；`parse_rate_limit` 仅支持整数系数（已注释）。
- **Outbox 对账**（`tasks/outbox.py`，本轮全读）：SETNX 批锁 + `FOR UPDATE SKIP LOCKED` 行锁；去重键**仅在 commit 后**写；`_INCR_FAIL_COUNT_LUA` 原子 `HINCRBY`+`EXPIRE`；DLQ + fail-count；reconciler 释放 hold（worker 猝死不漏钱）+ `_cleanup_terminal_sentinels`（防容量死锁）。
- **重试判定**（`retry.py`，本轮全读）：`is_retriable` 为纯函数，无状态 / 无并发面。
- **PK 直加载无 IDOR**：worker/路由侧 `db.get(Generation/Completion, id)` 的 id 均来自**同一请求内服务端新建的行**（`result.generation_ids`/`completion_id`），非客户端 path 参数；`messages.py:964` 加载 `UserMemory` 后立即复核 `memory.user_id != user.id`。

---

## 四、apps/web（前端）

> 总体结论：**未发现 XSS / 危险渲染**。

### 已复核为**安全**（非 bug，记录正面结论）
- **唯一 `dangerouslySetInnerHTML`**（`components/markdown/MarkdownPreview.tsx:18`）只被 admin 的 `UpdateAvailableCard.tsx:215` 使用，渲染 `check.release.body_html`；该 HTML 由服务端 `update_check.py:108-126 _release_html` 生成——**每行先 `html.escape` 再套固定 `<h1/h2/h3/li/p>` 标签**，内容无法注入脚本。
- **聊天 Markdown**（`components/ui/Markdown.tsx`）用 `react-markdown` + `remark-gfm` + `rehype-highlight`，**未引入 `rehype-raw`**（依赖里也没有），原始 HTML 默认被转义，不渲染 `<script>`。

---

## 五、部署 / 脚本 / CI

### DEPLOY-1 (P3) `restore.sh` Postgres 恢复失败无回滚（与 Redis 路径不对称）
- **位置**：`scripts/restore.sh:386-417`
- **现象**：PG 恢复先 `DROP DATABASE`（396）再 `pg_restore`（406）；若 `pg_restore` 致命失败（rc≥2，410），活库已被 drop，**无备份回滚**。而紧邻的 Redis 路径（317-353）**刻意**设计成 mv-旧数据到备份 → cp 新数据 → 失败回滚，注释明确写“避免清空但没装回的损毁状态”。
- **缓解（关键，已大幅降低风险）**：脚本停服前已做 **`gzip -t "$PG_FILE"`（295）+ `tar -tzf`（296）+ 文件存在性（287）** 预检（注释：“验证文件完整性再停服，避免坏备份导致恢复空档”）；`validate_redis_host_dir`（239-261）拒绝 `/`、`/var`、`/opt` 等宽目录。残余窗口仅“gzip 完好但 pg_dump 归档逻辑损坏 / 跨版本不兼容”导致 restore 中途失败。
- **后果**：上述残余窗口下，恢复失败会留下空库且无自动回滚（需人工从其它备份补救）。
- **修复方向**：drop 前 `pg_restore --list` 校验归档可读；或恢复到临时库成功后再 rename；或 drop 前先 `pg_dump` 当前库到回滚文件。
- **旧文档重叠**：未见记录（NEW，低危且已被预检大幅缓解）。

### 已复核为**安全**（非 bug，记录正面结论）
- 全仓 shell 脚本 `rm -rf` 绝大多数作用于受控 `${tmp_dir}`/`${LOCKDIR}` 或仅出现在告警文案；`restore.sh`/`lumenctl.sh` 的删除路径均经存在性/目录白名单校验，`set -euo pipefail` 全开。
- 真实源码（排除 `target/`、`dist/`、bundled `node_modules/`）中**无** `eval`/`exec`/`pickle.loads`/`yaml.load`/`shell=True`/`os.system`，**无** `verify=False`/TLS 关闭，**无** f-string 拼接 SQL（ORM 化）。

---

## 六、apps/tgbot

> 总体结论：**未发现 bug**。

### 已复核为**安全**（非 bug，记录正面结论）
- **双因子准入**（`middlewares.py:1-9` `AccessGate`）：仅放行 `chat.type=="private"`（防群聊绑定越权）+ 可选 `TELEGRAM_ALLOWED_USER_IDS` 白名单；与服务端 `telegram_bindings`（`deps.py:get_bot_user`，`X-Bot-Token` 用 `compare_digest` + chat_id 绑定）构成纵深。
- `api_client.py` 出站 httpx 带超时；无危险调用。

---

## 总评

本轮在**全部四个模块组**对最高风险面（资金/幂等、鉴权/会话/CSRF、SSRF、XSS、数据丢失、IDOR、分布式并发、迁移链、命令注入、**异步任务生命周期**）做了独立深挖。**结论：服务端整体加固良好，未发现可被普通用户触发的 P0/P1，亦无新增 P2**；新发现全部为 **P3 防御纵深 / 资源管理 / 代码异味**（CORE-1 锁字典泄漏、CORE-2 admin 限定的 SSRF rebinding、API-1 CSRF opt-in、API-2 弃用 `utcnow`、WORKER-1 fire-and-forget `create_task`、DEPLOY-1 PG 恢复回滚不对称）。多条旧文档点名的 P0（pricing 截断、settle 重复扣费、billing_cache Redis 竞态）经独立复核**已在当前代码修复**，且仓库维护 `test_bug_audit_worker_regressions.py` 等回归测试持续守护。

> **一处自我纠偏（方法论留痕）**：WORKER-1 初判为"P2 socket 泄漏（任务在 `sleep` 期间被 GC）"，逐行复核 CPython 任务引用链后**下修为 P3**——协程体全程挂起在事件循环强持有的 awaitable（`sleep` 计时器 / `to_thread` future / httpx IO）上，GC 中途回收的失败模式在此不成立，残余仅"无优雅停机句柄"。结论以核实为准、不照搬直觉。

### 方法与覆盖说明（便于复核盲区）
- 采用"风险模式 grep 扫描 + 高风险文件全读 + 调用链溯源验证"。**累计完整读**：`billing.py`(core/worker)、`pricing.py`、`billing_cache.py`、`byok.py`、`url_security.py`、`image_signing.py`、`security.py`、`deps.py`、`restore.sh`、`update_check.py`、`MarkdownPreview.tsx`+`UpdateAvailableCard.tsx`、tgbot `middlewares.py`、各分布式锁 Lua；**本轮新增全读**：`invites.py`、`generations.py`、`shares.py`、`regenerate.py`、`me.py`、`events.py`、`account_limiter.py`、`retry.py`、`tasks/outbox.py`。
- **本轮新增的横切复核**：(1) 枚举全仓 **全部 `create_task`/`ensure_future` 站点**，逐一判定引用持有情况（仅 WORKER-1 的 6 处未持有）；(2) 对 `workflows/conversations/messages/poster_styles` 的 **`db.get()` PK 直加载**逐处溯源，确认 id 均来自服务端新建行或带 `user_id` 复核——无 IDOR；(3) 复核 `apps/web` 唯一 `dangerouslySetInnerHTML`（`MarkdownPreview.tsx:18`）的喂入链：`releaseHtml←check.release.body_html←_release_html()`，服务端逐行 `html.escape`（含 `quote=True`）后才套固定标签，XSS 链完整闭合。
- **仍未逐行精读**（采样/grep + 模式复核覆盖，可能存在盲区）：`workflows.py`(11135 行)/`conversations.py`/`messages.py`/`poster_styles.py` 在**归属过滤与 PK 加载之外**的业务主体；`apps/worker/app/tasks/completion.py|generation.py|provider_pool.py` 在**计费与异步任务生命周期之外**的主体；前端绝大多数组件状态逻辑。如需对其中某块做逐行精读，请指定。
