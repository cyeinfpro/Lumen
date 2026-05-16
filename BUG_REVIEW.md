# 未提交变更 Bug 审查报告

> 范围：当前 working tree 全部 modified / untracked 文件（约 100 个文件，3700+ 增行）。
> 方法：API / Worker / Scripts·Infra / Core·Tests 四个领域并行审查；前端审查触发速率限制未完成，需另行补做。
> 输出：按"严重度 → 模块 → 文件:行号"组织。每条都给了触发条件、后果、最小修复 diff，方便接手者直接 apply。
>
> 已有任务清单：仓库内 `TaskList` 已经按本文件创建了 19 个 Task（id 1‑19）。建议按本文顺序处理。
>
> 未覆盖：`apps/web/**`（前端审查未完成）；下次需要专门跑前端审查 agent。

---

## P0 — 必须在下次发版前修

### P0‑1. `redeemCode` 全部返回 428（线上兑换功能直接断）

- 后端：`apps/api/app/routes/billing.py:174-200`（`_redemption_idempotency_key`），调用点 `:1604`。
  新逻辑要求请求头 `Idempotency-Key`，否则 422/428 `idempotency_key_required`。
- 前端：`apps/web/src/lib/apiClient.ts:2252`（`redeemCode`）未发送该头。
- 触发：发版后，登录用户在 `/me/wallet` 点击"兑换"。
- 后果：所有钱包兑换 100% 失败。
- 修复（二选一，推荐 A + B 同时）：
  - **A（前端必做）**：在 `redeemCode` 中生成 `crypto.randomUUID()` 作为 `Idempotency-Key` 请求头。
  - **B（后端兜底，向后兼容旧客户端）**：`_redemption_idempotency_key` 在缺头时回退派生：
    ```python
    if raw is None:
        digest = hashlib.sha256(f"{user_id}:{normalized_code}".encode()).hexdigest()[:32]
        return f"derived:{digest}"
    ```

---

### P0‑2. `BYOK_API_KEY_MASTER_SECRET` 在升级时被静默重置 → BYOK 密文永久不可读

- 文件：`scripts/install.sh:672-679, 1384`。
- 触发：旧部署在 `shared/.env` 里没有 `BYOK_API_KEY_MASTER_SECRET`（升级到引入 BYOK 之前的版本就是这种情况），运行 `install.sh` 走"已存在 env 跳过密钥生成"分支，但 `ensure_required_env_secrets` 仍会对该 key 调 `ensure_env_secret`，发现为空就 `generate_hex_secret 48` 生成新值。
- 后果：DB 里 `byok_*` 表所有密文都用旧 key 加密，新 key 永远解不开 → 客户的 BYOK 凭证永久丢失，仅 `log_info "已补齐随机密钥"` 提示。
- 修复（`scripts/install.sh` 中 `ensure_env_secret` 内部）：
  ```bash
  if [ -z "${value}" ]; then
      if [ "${key}" = "BYOK_API_KEY_MASTER_SECRET" ] && [ "${LUMEN_ALLOW_BYOK_KEY_GEN:-0}" != "1" ]; then
          log_error "BYOK_API_KEY_MASTER_SECRET 缺失，且数据库可能已有 BYOK 密文。"
          log_error "  - 新部署：export LUMEN_ALLOW_BYOK_KEY_GEN=1 再重跑安装。"
          log_error "  - 升级：从备份恢复原始 BYOK_API_KEY_MASTER_SECRET，不要让脚本随机生成。"
          return 1
      fi
      value="$(generate_hex_secret "${bytes}")"
      ...
  fi
  ```

---

### P0‑3. systemd `timeout --kill-after=30s` 在 rollback 中 SIGKILL → 留陈旧 lock 永久阻塞

- 文件：`deploy/systemd/lumen-update-runner.service:21-29`。
- 触发：`update.sh` 跑到 7200s 超时（或操作员 systemctl stop），SIGTERM 后 30s 强杀。`update.sh` 的 EXIT trap（解锁 / 回滚镜像 tag / 恢复 symlink）在 docker compose 状态下经常 >30s。
- 后果：rollback 中断、release symlink 不一致、`.lumen-maintenance.lock.d` 残留，后续所有 update 被 lock 卡死要人工 `rm`。
- 修复：把 `--kill-after` 提到 300s（与 `TimeoutStopSec=300` 对齐），或直接去掉让 systemd 自己管理：
  ```diff
  -ExecStart=/usr/bin/env timeout --preserve-status --kill-after=30s 7200s bash /opt/lumen/current/scripts/update.sh
  +ExecStart=/usr/bin/env timeout --preserve-status --kill-after=300s 7200s bash /opt/lumen/current/scripts/update.sh
  ```

---

### P0‑4. `install.sh` 多处 `up --pull never` 在首装路径会硬失败

- 文件：`scripts/install.sh:1591, 1698, 1710`。
- 触发：`pull_or_build_images` 先跑 pull 再 fallback build。若 pull 不稳定且 build 失败 + 镜像本地不在 → 后续 `up --pull never` 直接报 "Image not found locally"。`tgbot` 在首装时常常未被预拉取，几乎必踩。
- 修复：把 `--pull never` 改成 `--pull missing`：
  ```diff
  -_install_compose up --pull never -d --wait postgres redis
  +_install_compose up --pull missing -d --wait postgres redis
  ```
  其余两处同改。`scripts/update.sh:1591` 之类回滚路径若同样有就一并改。

---

### P0‑5. `lumen_pid_cmdline` 空输出 + 退出码 0 → `stale lock` 误判 → 并发 update

- 文件：`scripts/lib.sh:1549-1565`，调用方 `scripts/lib.sh:502 lumen_lock_dir_stale`。
- 触发：macOS / 无 `/proc` 容器内，`ps -o command= -p PID` 在 PID 是僵尸 / 短暂消失时返回 0 但无输出。
- 后果：`owner_cmd` 为空 → 命中 fallthrough → `log_warn` 后判 lock 陈旧 → 删除存活 lock → 两个 `update.sh` 同时跑 → release 状态损坏。
- 修复（`lumen_pid_cmdline`）：
  ```bash
  if command -v ps >/dev/null 2>&1; then
      local out
      out="$(ps -o command= -p "${pid}" 2>/dev/null)"
      if [ -n "${out}" ]; then printf '%s' "${out}"; return 0; fi
      out="$(ps -o args= -p "${pid}" 2>/dev/null)"
      if [ -n "${out}" ]; then printf '%s' "${out}"; return 0; fi
      if [ "${EUID:-$(id -u)}" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
          out="$(lumen_run_as_root ps -o command= -p "${pid}" 2>/dev/null)"
          if [ -n "${out}" ]; then printf '%s' "${out}"; return 0; fi
      fi
  fi
  return 1
  ```
  同时 `lumen_lock_dir_stale` 在 `lumen_pid_cmdline` 失败时**不要**判 stale，保守保留 lock。

---

## P1 — 数据完整性 / 真实功能 bug，必须修

### P1‑6. `password_reset_request` 通过 503 暴露邮箱是否注册

- 文件：`apps/api/app/routes/auth.py:735-787`。
- 旧逻辑：Redis 异常吞掉返回 200，与 "邮箱不存在"路径无差异。
- 新逻辑：`user` 不存在直接 200；`user` 存在且 Redis SET 失败 → 抛 503 `reset_unavailable`。
- 触发：Redis 短暂抖动；攻击者批量请求。
- 后果：能枚举所有注册邮箱（200=未注册，503=注册）。
- 修复：
  ```python
  try:
      await redis.set(key, user.id, ex=_PASSWORD_RESET_TTL_SECONDS)
  except Exception:
      logger.exception("password_reset_token_store_failed", ...)
      return OkOut(ok=True)
  background_tasks.add_task(...)
  ```
  另外 `resolve_public_base_url` 也应放在 `user is None` 分支之前（或同样吞错），避免它失败时产生差异响应。

---

### P1‑7. `AUTH_ADMIN_LOGIN_LIMITER` 只在 `user.role=='admin'` 才触发 → 管理员邮箱枚举

- 文件：`apps/api/app/routes/auth.py:679-693`。
- 触发：攻击者用候选邮箱列表喷登录接口。
- 后果：5 次后 429 *只* 出现在管理员邮箱上，攻击者据此识别管理员账号。
- 修复：基于提交邮箱无条件跑限速器：
  ```python
  email_hash = _log_hash(email) or "unknown"
  admin_key = f"rl:auth:admin_login:{require_client_ip(request)}:{email_hash}"
  await AUTH_ADMIN_LOGIN_LIMITER.check(get_redis(), admin_key)
  ```
  放在 `user` 查询之前，并保留普通登录限速器（用 `verify_password` dummy hash 保持时序对称）。

---

### P1‑8. `topup_redeem` replay 路径会覆盖既有 `tx.meta`（"修改历史"）

- 文件：`apps/api/app/routes/billing.py:1662-1676`。
- 触发：并发兑换；或 Redis 缓存被清空但 DB 已有 `idempotency_key=redeem:{usage_id}` 的 tx。
- 后果：旧账本 `meta.redemption_request_hash` 被新请求覆盖，对账丢失原始上下文；如果 `normalize_redemption_code` 演化，未来比对会失败。
- 修复（两选一）：
  - **A**：让 `topup_redeem` 返回 `(tx, created)`，调用方只在 `created` 时设 `meta`。
  - **B**：直接把 meta 通过 `topup_redeem(meta=...)` 传入，由 core 决定是否合并到新建行（推荐）：
    ```python
    tx = await billing_core.topup_redeem(
        db, user.id, code.amount_micro,
        usage_id=usage_id, code_id=code.id,
        meta={
            "client_idempotency_hash": hashlib.sha256(idempotency_key.encode()).hexdigest()[:16],
            "redemption_request_hash": request_hash,
        },
    )
    ```
    并删除调用方的 `tx.meta = {...}`。

---

### P1‑9. `topup_redeem` `meta` 合并顺序允许调用方覆盖 `code_id`

- 文件：`packages/core/lumen_core/billing.py:636`。
- 当前：`meta={"code_id": code_id, **(meta or {})}` → 调用方传 `meta={"code_id": "fake"}` 会盖掉权威值。
- 修复：
  ```diff
  -        meta={"code_id": code_id, **(meta or {})},
  +        meta={**(meta or {}), "code_id": code_id},
  ```

---

### P1‑10. 24h 内重复轮转密钥会丢掉最早 previous_secret

- 文件：`apps/api/app/routes/billing.py:359-414, 1559-1583` 与 `apps/api/app/routes/system_settings.py:71-95, 185-203`。
- 触发：管理员在 24h 窗口内连续两次轮转（误点 / 故意）。
- 后果：第二次轮转把"第一次的 old_secret"写入 previous，原始 secret 永远丢失 → 用原始 secret 哈希的兑换码全部 `CODE_NOT_FOUND`，违反 24h grace 承诺。
- 修复（`_remember_previous_redemption_secret`）：
  ```python
  current = _parse_previous_redemption_secret(
      await _system_setting_raw(db, _PREVIOUS_REDEMPTION_SECRET_KEY)
  )
  if current and current != old_secret:
      raise _http(
          "previous_secret_locked",
          "another rotation is still inside the 24h transition window",
          409,
      )
  ```
  同时把这段 helper 与 `_previous_redemption_secret_payload` / `_parse_previous_redemption_secret` 抽到 `app/services/redemption_secret.py` 单一源，billing.py 与 system_settings.py 共享 import。

---

### P1‑11. `tgbot` pull 失败 hard-abort 整次 `update`

- 文件：`scripts/update.sh:1128-1135`。
- 触发：GHCR 对某个 tag/平台短暂 503 / publish 延迟。
- 后果：api/worker/web 关键安全更新被一个非核心组件阻塞。
- 修复：默认 warn-only，仅在 `LUMEN_UPDATE_REQUIRE_TGBOT=1` 时硬失败：
  ```bash
  if ! lumen_retry 2 5 "docker compose pull tgbot" \
          lumen_compose_in "${NEW_RELEASE}" --profile tgbot pull tgbot; then
      if [ "${LUMEN_UPDATE_REQUIRE_TGBOT:-0}" = "1" ]; then
          log_error "[pull_images] tgbot pull 失败，已配置 REQUIRE_TGBOT，终止更新。"
          emit_fail pull_images 1
          exit 1
      fi
      log_warn "[pull_images] tgbot pull 失败，跳过 tgbot 更新（不影响 api/worker/web）。"
  fi
  ```

---

### P1‑12. `docker-compose.yml` 一次性 profile `restart: on-failure:3`

- 文件：`docker-compose.yml:299`（`migrate`）、`:326`（`bootstrap`）。
- 后果：alembic 部分失败被 docker 自动重试 3 次，遮蔽真实错误；bootstrap 非幂等时产生混乱状态。
- 修复：
  ```diff
  -    restart: "on-failure:3"
  +    restart: "no"
  ```

---

### P1‑13. `apps/worker/app/upstream.py` LRU 弹出立即关闭使用中 client

- 文件：`apps/worker/app/upstream.py:603-659`（`_cache_proxied_client`）。
- 触发：活跃 `(timeout_config, proxy_url)` 组合 > 32；或 runtime 改 timeout_config 导致 key 不命中。
- 后果：LRU 弹出旧 client 后立即 `await aclose()`，他处仍持有的请求 / 流被 `RuntimeError` / `ReadError` 截断。
- 修复（任一）：
  - 弹出时不主动关闭，依赖 GC + httpx 连接池随对象释放；
  - 或加 refcount，到 0 才关；
  - 或调度延迟关闭：
    ```python
    asyncio.create_task(_delayed_aclose(evicted_client, delay=30.0))
    ```

---

### P1‑14. `generation` lease_lost 在 attempt >= `_MAX_ATTEMPTS` 时仍盲目入队 → 任务静默卡死

- 文件：`apps/worker/app/tasks/generation.py:2304-2381`（`_mark_generation_attempt_retrying`）+ `:4457-4475`（lease_lost handler）。
- 触发：Redis 抖动导致 lease 持续丢失，已到 `_MAX_ATTEMPTS`。
- 后果：传给 arq `_job_try=attempt+1 > max_tries(默认 5)`，arq 拒绝执行并送 DLQ，但 DB 仍 `QUEUED`，前端无终态。
- 修复（lease_lost handler 入口处）：
  ```python
  if attempt >= _MAX_ATTEMPTS:
      await _mark_generation_attempt_failed(
          redis, task_id=..., attempt=attempt,
          error_code="lease_lost_max_attempts",
          error_message="lease lost after max attempts",
          retriable=False,
      )
      return
  ```

---

### P1‑15. `sse_publish` dedupe hash 无 per-field TTL → 高频用户内存无限增长

- 文件：`apps/worker/app/sse_publish.py:31-53`（`_XADD_IDEMPOTENT_LUA`）。
- 触发：单用户持续高频事件流；Lua 每次 `EXPIRE` 把整个 hash 续到 24h，hash 永不过期。
- 后果：单 key 持续累积 → 数十 MB → Redis fork / migration 性能恶化。
- 修复：丢弃"hash + field" 思路，每条 event 自己一个 string key：
  ```
  SET events:user:{uid}:dedupe:{event_id} <stream_id> EX 86400 NX
  ```
  Lua 改成 `SETNX + GET` 模式即可。

---

### P1‑16. `account_limiter._check_window_fallback` 在 Redis 异常时仍 fail-open

- 文件：`apps/worker/app/account_limiter.py:151-160, 300-306`。
- 后果：注释 / `_check_window` 改成 fail-closed（异常返回固定 5s retry_after），但无 `eval` 的 Redis 客户端走 fallback 仍 fail-open（返回 `(0, None)`）。生产里若 Redis 代理不支持 `eval`，限流形同虚设。
- 修复（`_check_window_fallback` except 分支）：
  ```python
  except Exception:
      return count_limit, _make_redis_blip_retry_after(cur_now, window_s)
  ```

---

### P1‑17. `events.py` envelope `event_id` 覆盖调用方自带字段

- 文件：`apps/api/app/routes/events.py:438-442`。
- 触发：publisher payload 自带 `event_id`。
- 后果：被 envelope 静默覆盖。
- 修复：
  ```python
  if "event_id" not in payload:
      payload = {**payload, "event_id": envelope_event_id}
  ```
  或者把 envelope 字段加 `_` 前缀名空间。

---

### P1‑18. `byok_service._host_resolves_to_private` 在 event loop 内同步 DNS + 失败误判

- 文件：`apps/api/app/byok_service.py:146-161`，被 `normalize_base_url:114` 调用。
- 后果：
  1. 同步 `socket.getaddrinfo` 阻塞整个 FastAPI worker（DNS 失败时阻塞到解析超时，5–30s）。
  2. `socket.gaierror` → 直接 raise `ValueError`，合法 URL（如临时 DNS 抖动）被当成 SSRF 拒绝。
- 修复：
  ```python
  async def _host_resolves_to_private(host: str) -> bool:
      loop = asyncio.get_event_loop()
      try:
          infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
      except socket.gaierror:
          return False  # 让真正的 HTTP 调用去失败，不要在校验阶段拒绝合法 URL
      ...
  ```
  注：这只挡明显 SSRF，**不是** DNS rebinding 防护；要真挡需要在 httpx custom transport 里再次解析并校验实际连接 IP。

---

### P1‑19. `generation._release_lease` 无 token CAS → 误删他人 lease

- 文件：`apps/worker/app/tasks/generation.py:217-221, 4833-4840`。
- 触发：旧 worker lease_lost 后 finally 路径无条件 DELETE；新 worker 已在原 TTL 内重新拿到 lease。
- 后果：新 worker 的 lease 被旧 worker 删；第三个 worker 又能拿到 lease → 多 worker 并发处理同任务 → 状态机反复跳 / 双扣 / 双事件。
- 修复：lease set 时写入 worker token（uuid），release 用 Lua CAS：
  ```lua
  if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
  end
  return 0
  ```

---

## P2 — 真实但影响有限的 bug

### P2‑20. Alembic `0025_users_active_email_unique` 非 CONCURRENTLY，且 downgrade 不安全

- 文件：`apps/api/alembic/versions/0025_users_active_email_unique.py`。
- 后果：大表（>1M 行）+ `lock_timeout=5s` 时 `CREATE INDEX`（非 CONCURRENTLY）会被中断；downgrade 在已有重复邮箱（partial unique 允许）时直接失败。
- 修复：
  ```python
  with op.get_context().autocommit_block():
      op.create_index(..., postgresql_concurrently=True, postgresql_where=text("deleted_at IS NULL"))
  ```
  downgrade 需先 dedupe 或显式失败提示。

### P2‑21. `password_reset_confirm` 非原子，限速器先消耗后才 `getdel`

- 文件：`apps/api/app/routes/auth.py:805-837`。
- 修复：先 `getdel`，命中后再 check per-user 限速器（token 已被销毁，限流应用在 user_id 上更合理）。

### P2‑22. `_lock_redemption_idempotency_key` 用 `func.hashtext`（32-bit）

- 文件：`apps/api/app/routes/billing.py:218-228`。
- 修复：Python 端 SHA-256 取 8 字节做 `pg_advisory_xact_lock(bigint)`。

### P2‑23. `list_invite_links` / `revoke_invite_link` 改成 per-admin scope

- 文件：`apps/api/app/routes/invites.py:140-188`。
- 后果：管理员 B 看不到 A 的邀请、无法吊销；若创建者被软删除则邀请永远不可管理。
- 修复：scope 改成租户/角色（super_admin 可吊销任何人），或者至少对 `creator.deleted_at != NULL` 的邀请做 fallback。

### P2‑24. background email task 仅 catch `EmailDeliveryError`

- 文件：`apps/api/app/routes/auth.py:862-882`。
- 修复：`except Exception` 全捕；保证 SMTP 任意异常都会删除 reset token。

### P2‑25. `config.smtp_password` 空白未 strip

- 文件：`apps/api/app/config.py:174-179`。
- 修复：先 strip 再判 truthy。

### P2‑26. worker `start_metrics_server` 失败后不释放端口

- 文件：`apps/worker/app/main.py:34-63`。
- 后果：容器重启循环时 EADDRINUSE。
- 修复：保存 server 实例，在 except 分支主动 close。

### P2‑27. worker `_validate_resolved_size` 对显式 fixed_size 仍跑 aspect_ratio 漂移校验

- 文件：`apps/worker/app/tasks/generation.py:1475-1499`。
- 后果：历史任务 (size_requested, aspect_ratio) 不严格匹配时被新校验杀掉，无法回放。
- 修复：仅在 fixed_size 为空（靠 preset 推断）时跑漂移校验。

### P2‑28. `BillingCacheService.deduct_sync` 不限 floor

- 文件：`packages/core/lumen_core/billing_cache.py:176-201`。
- 后果：写出负余额到缓存。
- 修复：`row.balance_micro = max(0, row.balance_micro - int(micro))`，或显式 raise。

### P2‑29. `topup_redeem` 缺统一的"先 cheap check `_existing_tx` → 后取锁"模式

- 文件：`packages/core/lumen_core/billing.py:617-637`。
- 后果：与同模块其它 mutator 风格不一致；锁等待期间徒增串行化。
- 修复：在 `get_wallet(lock=True)` 之前先 `_existing_tx` 一次。

### P2‑30. `_remember_previous_redemption_secret` 在 billing.py 与 system_settings.py 重复实现

- 见 P1‑10。即使不修语义也至少要去重，避免未来单边漂移。

### P2‑31. nginx `_internal_storage` CSP 被一并删除，未在 server 级补回

- 文件：`deploy/nginx.conf.example:72-77, 184-189`。
- 后果：直接命中 `/internal_storage/...` 的响应失去 `default-src 'none'` 防护；polyglot HTML/JPG 上传无 CSP 兜底。
- 修复：server 级添加 `add_header Content-Security-Policy "default-src 'self'; frame-ancestors 'none';" always;`，或在 `_internal_storage` location 重复全部 header（nginx `add_header` 不继承）。

### P2‑32. `docker-compose.yml` `web` healthcheck 用 `wget`（依赖镜像基底）

- 文件：`docker-compose.yml:241`。
- 修复：参考 `api` 的 `python -c "urllib.request..."` 写法。

### P2‑33. `scripts/version.py` `CURRENT_RELEASE_JSON` 路径双 `current`

- 文件：`scripts/version.py:18, 31`。
- 后果：在 `/opt/lumen/current/scripts/version.py` 调用时实际是 `/opt/lumen/current/current/.lumen_release.json`，文件不存在，校验 silently 跳过。
- 修复：先尝试 `ROOT / ".lumen_release.json"`，不存在再回退 `ROOT / "current" / ".lumen_release.json"`。

### P2‑34. `scripts/version.py` 允许 `image_tag == "main"` 无门控

- 文件：`scripts/version.py:143`。
- 修复：用 `LUMEN_ALLOW_ROLLING_TAG=1` 等环境变量门控。

### P2‑35. worker `provider_pool` 读 `account_limiter._REDIS_ERROR_RETRY_AFTER_S` 私有常量

- 文件：`apps/worker/app/provider_pool.py:690-708`。
- 修复：在 `account_limiter` 把该常量去掉前导下划线对外公开，并被 pool 显式 import。

### P2‑36. worker `sse_publish._envelope` 用 falsy 判断 `data.get("event_id")`

- 文件：`apps/worker/app/sse_publish.py:78-84`。
- 修复：`if raw not in (None, "")` 而非 `if raw`，避免 `0`/`False` 被替换。

---

## P3 — 测试覆盖不足或脆弱

### P3‑37. `test_signup_email_check_ignores_soft_deleted_users` mock 让真行为绕过

- 文件：`apps/api/tests/test_auth_security.py:230-277`。
- 后果：实际 SQL 是否包含 `deleted_at IS NULL` 仅靠字符串检查；若 fixture 顺序变了字符串检查会误匹配前置 statement。
- 修复：`_Db.execute` 解析 SQL 文本，只有命中 `deleted_at is null` 才返回种子行；否则 None。这样删过滤条件 `pytest.raises(Exception)` 真的会失败。

### P3‑38. `test_redeem_code_requires_request_idempotency_key` 仅做源码字符串扫描

- 文件：`apps/api/tests/test_billing_route.py:546-559`。
- 修复：用 FakeRequest 真调 `redeem_code`，断言不带 `Idempotency-Key` 时抛 `idempotency_key_required`。

### P3‑39. `test_record_image_call_daily_expireat_is_future_at_utc_boundary` 实际走 fallback 不走 Lua

- 文件：`apps/worker/tests/test_account_limiter.py:229-239`。
- 修复：在 `FakeRedis` 上加最小 `eval` 实现，覆盖 Lua 路径。

### P3‑40. `_FirstDb` 与真实 `Result.first()` 行为差异

- 文件：`apps/api/tests/test_billing_route.py:163-170`。
- 修复：返回 `Row` 风格对象，或在 mock 上加 `_mapping`。

---

## P4 — 风格 / 防御性，可低优处理

- `apps/api/app/routes/workflows.py`：`logger.warning` → `logger.exception` 行为正确。
- `apps/api/app/routes/images.py`：`MAX_IMAGE_PIXELS` 收紧到 64M、variant key 改用 `img.id` 都安全。
- `Dockerfile.python`：`pip install --no-cache-dir` 已是常态；可考虑 `--mount=type=cache,target=/var/cache/apt,sharing=locked` 加速 CI。
- `docker-compose.yml`：`tgbot` 无 healthcheck，会出现 `Restarting` 静默。
- `apps/worker/app/observability.py`：metric 重注册分支可加 `isinstance` 校验防类型冲突 crash。
- `apps/worker/app/account_limiter.py _daily_expire_at`：NTP 大跳变时 TTL 退化为 `int(now)+1` 秒，影响轻微。

---

## 前端审查（已补做）

> 范围：`apps/web/**` 全部 modified / untracked 文件；重点是 state / 副作用 / SSE / SW。
> 已确认无 bug（不再展开）：`ServiceWorkerRegister`、`QueryProvider`、`queryClient`、`layout.tsx` ErrorBoundary 重构、`useComposerAttachmentDnd` / `useMaskInpaint`（AbortController 管理完备）、`check-ui-governance.mjs`（已在 `npm run lint` 链路里、CI 真会触发）、`lib/email.ts`、`admin/page.tsx` 401/403 重定向。

### P0‑41. SSE BroadcastChannel 跨 tab 重放导致流式文本双重拼接（critical）

- 文件：`apps/web/src/components/SSEProvider.tsx:247-262, 314-345`，下游 `apps/web/src/store/useChatStore.ts:1239-1250`。
- 触发：两个 tab 同时打开同一会话。后端 `completion.delta` / `completion.thinking_delta` 多数情况下不带 `event_id`（`Last-Event-ID` 头也不下发），`payloadEventId()` 返回 `null`，`markEventSeen()` 直接 `return true` 但**不写入** seen set。本 tab 应用 patch 后通过 BroadcastChannel 广播；另一 tab 自己刚通过 SSE 收到一份，又从 broadcast 收到一份并第二次 `queueCompletionStreamPatch()`。
- 后果：流式回答助手 `text` / `thinking` 被双倍拼接（"你好你好我能帮…"）。新加的 `endsWith` 守卫只在 `isTerminal` 生效，对增量无效。`account_settings_updated` 等无 id 事件副作用是 `invalidateQueries`、幂等没事，唯独 delta 类直接破坏状态。
- 修复（最小）：
  ```ts
  // markEventSeen(): 缺 id 的事件不允许跨 tab 广播
  if (!id) return null;  // 旧值 true 改成 null（或新增枚举）
  ```
  ```ts
  // deliverSSEEvent(): 根据 markEventSeen 结果决定 broadcast
  const accepted = markEventSeen(parsed);
  if (accepted === false) return;
  const allowBroadcast = accepted !== null && !opts?.fromBroadcast;
  applySSEEventWithSideEffects(parsed, { broadcast: allowBroadcast });
  ```
- 严重度：critical（多 tab 用户必现）

### P1‑42. `SystemUpgradeBanner` 错误后 `refetchInterval=false` 永久停止（medium）

- 文件：`apps/web/src/components/SystemUpgradeBanner.tsx:13-16`。
- 触发：后端维护期间间歇 5xx → query 出错 → `refetchInterval` 返回 `false`，后续没有任何回退路径恢复轮询（`refetchOnWindowFocus` 默认关、组件无 manual refetch）。
- 后果：banner "升级中" 永不消失，必须手动刷新页面。`retry: 2` 只覆盖单次 attempt，不会重启 interval。
- 修复：
  ```ts
  refetchInterval: (query) => (query.state.error ? 30_000 : 5_000),
  ```
- 严重度：medium

### P1‑43. `cloneComposerState` 浅拷贝 attachments / 不校验 mask 引用（medium）

- 文件：`apps/web/src/store/useChatStore.ts:338-345`，使用点 `:2069`（`restoreComposerOnFailure` 路径）。
- 触发：`sendMessage` 失败 + `restoreComposerOnFailure !== false` → 用 `cloneComposerState(composerToSend)` 回填草稿；`attachments` 是新数组但元素 `AttachmentImage` 仍是同一对象引用；`mask.target_attachment_id` 可能指向已被 `removeAttachment` 删掉的元素。
- 后果：还原后的草稿被外部代码就地修改字段时污染共享对象；mask 出现孤悬引用，后续 generation 路径下空指针 / 失败。
- 修复：
  ```ts
  attachments: composer.attachments.map((a) => ({ ...a })),
  // 同时校验 mask 引用：
  if (composer.mask && !composer.attachments.some((a) => a.id === composer.mask.target_attachment_id)) {
    cloned.mask = undefined;
  }
  ```
- 严重度：medium

### P2‑44. `LazyInpaintModal` dynamic 缺 loading fallback（low）

- 文件：`apps/web/src/components/ui/inpaint/LazyInpaintModal.tsx:1-12`。
- 触发：首次打开 inpaint modal，弱网下 chunk 加载 100-300ms。
- 后果：无任何视觉反馈，用户可能二次点击。
- 修复：
  ```ts
  dynamic(() => import("./InpaintModal"), {
    ssr: false,
    loading: () => <div className="fixed inset-0 z-[var(--z-dialog)] bg-black/60" />,
  });
  ```
- 严重度：low

### P2‑45. `uploadLimits.ts` 硬编码 60MB 与后端 settings 不同步（low）

- 文件：`apps/web/src/lib/uploadLimits.ts:1-2`。
- 触发：运维下调 `IMAGE_UPLOAD_MAX_BYTES` 到例如 32MB，用户上传 50MB 通过前端检查后被后端 413 拒。
- 修复：从 `RuntimeDefaults` / `system_settings` 拉取运行时值进 store；硬编码作 fallback。
- 严重度：low

### P2‑46. `SSEProvider` StrictMode 下短暂窗口本地事件不广播（low）

- 文件：`apps/web/src/components/SSEProvider.tsx:230, 327-345`。
- 触发：仅 React StrictMode / dev double-effect 时的 cleanup 期间。
- 后果：可感知一次本地事件未跨 tab 同步。生产环境 (`reactStrictMode: false` 或 prod build) 不可见。
- 修复（可选）：channel 设置 / cleanup 用 ref + 立即同步赋值，避免 cleanup 把还没替换的 ref 置 null。
- 严重度：low（dev-only）

### P3‑47. `useChatStore.reset` 不清 `SEEN_EVENT_IDS`（low）

- 文件：`apps/web/src/store/useChatStore.ts:3997-4002` + `SSEProvider.tsx` 内 `seenEventIdsRef`。
- 触发：登出 → reset；新账户登录后理论上若与旧账户最近 2000 条 UUID 之一冲突，事件被丢。UUID 概率几乎为 0。
- 修复（可选）：reset 同时通过事件 / store action 通知 `SSEProvider` 清 seenEventIds。
- 严重度：low（理论性）

---

## 处理顺序建议

1. **立刻 hotfix**（6 条）：P0‑1 redeemCode 头、P0‑2 BYOK 主密钥保护、P0‑3 systemd kill-after、P0‑4 `--pull never` → `missing`、P0‑5 `lumen_pid_cmdline` 空输出、**P0‑41 SSE 跨 tab 双拼接**。
2. **同发版批次**：P1 全部 16 条（含新增 P1‑42、P1‑43）。建议顺序：6 → 7 → 8 → 9 → 10 → 14 → 19 → 13 → 15 → 16 → 11 → 12 → 17 → 18 → 42 → 43。
3. **下一发版**：P2 全部 21 条（含 44、45、46）+ P3 测试加固（含 47）。
4. **整体**：本文件总计 47 条 finding，对应 `TaskList` task #1‑#24（合并同源条目，部分 P2/P3 未单独建 task）。
