# 未提交修改 Code Review — 2026-05-30

本次 review 覆盖所有未提交的工作区改动。这批改动是一组配套的 audit 修复，对应
`DESKTOP_BUG_AUDIT_2026-05-29.md` 与 `SERVER_BUG_AUDIT_2026-05-29.md`。整体方向正确，
以下记录复核中发现的遗留缺陷、风险提示，以及已确认安全的项。

review 级别：max effort（recall 导向，宁可多报）。每条结论均已对照源码二次验证，
标注 CONFIRMED（可指出触发输入与错误结果）或 PLAUSIBLE（机制成立、触发条件不确定）。

---

## 一、确认的缺陷（按严重度排序）

### 1. Windows 自动更新分发未签名安装包 — CONFIRMED

**文件**：`apps/desktop/packaging/scripts/build-win.ps1` / `.github/workflows/desktop-release.yml`
/ `apps/desktop/packaging/scripts/sign-win.ps1`

**机制**：
- `cargo tauri build`（build-win.ps1:315）先用**未签名**的 `.exe` 打出 `.nsis.zip`。
- 签名步骤 `sign-win.ps1`（build-win.ps1:318-337）在打包**之后**才运行。
- `sign-win.ps1:13` 只对 `*.exe,*.dll` 签名：`Get-ChildItem -Recurse $Path -Include *.exe,*.dll`。
- `tauri.conf.json` 没有配置 `signCommand`。

**后果**：独立下载的 `.exe` 是签名的，但**自动更新走的 `.nsis.zip` 内部装的是未签名安装包**，
更新时仍会触发 UAC / SmartScreen 警告，部分抵消了 P0-3 签名加固对自动更新场景的效果。

**修复方向**：在打 `.nsis.zip` 之前先签 `.exe`，或在 `tauri.conf.json` 配 `signCommand`
让 Tauri 在打包阶段对内嵌 `.exe` 签名。

---

### 2. 成功路径的 rollback 数据库永不回收 — CONFIRMED（泄漏）/ PLAUSIBLE（无 active 库边界）

**文件**：`scripts/restore.sh:385-387`、`scripts/restore.sh:104-106`

**机制**：
- 成功分支清理了 `PG_TEMP_DB` 和 `PG_SWAP_IN_PROGRESS`，但**没有清理 `PG_ROLLBACK_DB`**
  （`PG_TEMP_DB=""; PG_SWAP_IN_PROGRESS=0; log "...retained as $PG_ROLLBACK_DB"`）。
- `cleanup()` 只在 `PG_SWAP_IN_PROGRESS=1` 时才回收/恢复：
  `if [ "${PG_SWAP_IN_PROGRESS:-0}" = "1" ] && [ -n "${PG_ROLLBACK_DB:-}" ]; then ...`。

**后果**：每次成功 restore 都会留下一个 `lumen_rollback_<TS>_<pid>` 数据库无人回收，
磁盘无限增长；且仅在日志里提示一句，运维不一定会注意。

**附带边界（PLAUSIBLE）**：若 staged→active 与 inline rollback 同时失败，cleanup 重试
可能导致**没有任何 active 数据库**。

---

### 3. BYOK base_url 校验缓存变成只写死状态 — CONFIRMED

**文件**：`apps/worker/app/byok_runtime.py:68-87`

**机制**：早返回的读缓存逻辑已被移除，`_validate_supplier_base_url` 现在每次都调用
`resolve_public_http_target`（76），然后照样写 `_BASE_URL_VALIDATION_CACHE`（82-86），
**但再没有任何地方读取它**。

**后果**：每次调用白白多一次加锁 + dict 写入；缓存纯属死状态，且会误导后来的维护者
（看上去像有缓存语义，实际没有）。功能本身正确（不再有 rebinding 旁路），
应直接删除该缓存及其锁。

---

## 二、可能的缺陷（PLAUSIBLE）

### 4. AOF "恢复失败" 启发式可能误判触发破坏性隔离 — PLAUSIBLE

**文件**：`apps/desktop/src/sidecar.rs`

**机制**：`mentions_redis_aof_failure` 使用了 `"failed"`、`"invalid"`、`"unable"`、
`"[error]"` 等通用错误标记，且在 `RecoverReplay` 帧的 ±4 行窗口内匹配（`CONTEXT_LINES=4`）。

**后果**：
- **误报**：上下文行或邻近 4 行内出现任意通用错误词 → 误判恢复失败 → 破坏性隔离 AOF。
- **漏报**：真正的失败若距 `RecoverReplay` 帧超过 4 行则匹配不到。

新增测试未覆盖这两种边界；audit 本身也要求先用真实 Garnet 日志验证启发式。

---

### 5. keychain 写入成功后残留 Deleted 标记会永久遮蔽密钥 — PLAUSIBLE

**文件**：`apps/desktop/src/secrets.rs:259-260` + `select_secret_value`

**机制**：`set_secret` 先写 keychain（259）再 `remove_fallback_secret`（260）；
若移除失败直接返回 Err（262-270），此前的 Deleted 标记被保留。而 `select_secret_value`
对 `Deleted` 无条件返回 `None`。

**后果**：keychain 中其实已写入成功，但因 fallback 的 Deleted 标记未清除，读取时永远
返回 `None`，密钥被"幽灵删除"遮蔽。

---

### 6. 明文文件 write-then-chmod 的 TOCTOU 窗口 — PLAUSIBLE（低危）

**文件**：`apps/desktop/src/secrets.rs:192-194`、`apps/desktop/src/docker_import.rs`

**机制**：
- `write_fallback_map` 先把明文写入 `.tmp`（~192）再 `harden_private_file` chmod 0o600（~194）。
- harden 失败会留下未加固的 tmp 文件。
- Windows 走 `remove_file` + `rename`（非原子）。
- `write_json_private` 在 Windows 上完全没有设置 ACL。

**后果**：本地攻击者在短窗口内可能读取明文密钥；属低危，但应先 chmod 再写入内容，
或用更严格的创建权限。

---

### 7. 关闭期 cancel-mid-aclose 可能泄漏连接 — PLAUSIBLE（仅关闭期，影响小）

**文件**：`apps/worker/app/upstream.py:~741`

**机制**：`_close_retired_clients_now` 先 cancel 延迟 aclose 任务再直接 aclose；
若 cancel 命中 httpx 已置 CLOSED 但传输尚未拆解的瞬间，后续 aclose 变成 no-op。

**后果**：连接泄漏，仅在关闭期发生，影响有限。

---

## 三、风险提示（非 bug）

### A. DNS rebinding 仅收窄未关闭 — `packages/core/lumen_core/url_security.py`

`PublicHttpTarget.resolved_ips` 已计算但**未用于 outbound IP pinning**（全仓仅在
url_security 与测试中被引用），因此 DNS rebinding 只是**窗口收窄、未真正关闭**，
与 audit 中 CORE-2 "pin IP" 的意图有差距。该路径为 admin-only，风险可接受。

### B. gaierror 现在生产环境直接 raise — `apps/worker` BYOK 保存路径

这是有意的 fail-closed 修复，但副作用是**瞬时 DNS 故障时会拒绝保存**凭证，属预期行为，
留意即可。

---

## 四、已复核确认安全的项

| 项 | 文件 | 结论 |
| --- | --- | --- |
| P0-1 capabilities / permissions | `apps/desktop/capabilities/*`、`apps/desktop/permissions/*` | 注册命令 ↔ permissions ↔ capability 授权 ↔ 实际 invoke 点全部对齐（runtime.ts 17 个、startup 页 5 个），完整 |
| FastAPI router lifespan | `apps/api/app/routes/admin_update.py`、`admin_release.py` | marker cleanup 的 lifespan 确实会执行 |
| billing_cache 引用计数 | `packages/core/lumen_core/billing_cache.py` | `_LockEntry` 引用计数正确（先自增再 await；仅在 users<=0 且仍是同一 entry 时弹出） |
| Windows keep-awake | `apps/desktop/src/power.rs` | 改用专用 worker 线程 + mpsc，active 标志不会失步，无 Drop 死锁 |
| JWT 脱敏 | `apps/desktop/src/diagnostics.rs` | `redact_jwt_tokens` 用 char_indices 重写，UTF-8 边界安全 |
| mac 发布工作流 | `.github/workflows/desktop-release.yml` | notarization / stapler / spctl 校验、darwin-x86_64 manifest、移除 `*.exe.sig` 改用 `.nsis.zip.sig` 均正确 |

---

## 五、优先级建议

1. **#1 Windows 更新链未签名** —— 实质削弱了本次 P0-3 对自动更新场景的效果，建议优先修。
2. **#2 rollback 库泄漏** —— 运维侧磁盘隐患，且无 active 库边界涉及数据可用性。
3. **#4 AOF 启发式** —— 误判会触发破坏性隔离，建议先用真实 Garnet 日志验证再放行。
4. 其余按上表顺序处理。
