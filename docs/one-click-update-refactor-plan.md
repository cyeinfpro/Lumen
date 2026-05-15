# 一键更新 & 计费系统重构方案（Lumen × sub2api 最佳实践融合）

> 作者：cyeinfpro  最后修订：2026-05-15
> 状态：设计稿，待审

本方案对照 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的两个核心子系统——**一键更新 UX** 与 **token 计费 + 缓存**——对 Lumen 做结构性重构：

1. **Part Ⅰ（§0~§9 + 附录 A）**：让运维在点「更新」之前就看到当前版本、目标版本、变更内容、来源可信度、是否处于可更新状态；同时保留 Lumen 已有的 release‑tree、SSE 阶段流、双重回滚、代理池等长板。
2. **Part Ⅱ（§10~§17 + 附录 B）**：引入 prompt‑cache 感知的多档定价模型、Redis 余额/用量缓存（singleflight + 异步 worker pool）、网关粒度幂等（usage billing fingerprint）、按账户倍率（rate multiplier），消除现状下「命中 prompt cache 仍按 input 全价扣钱」「每次 completion 都打 PricingRule 表」等扣费失真与热路径压力。

两部分共用相同的运维侧基础：Redis 锁、Idempotency‑Key、`runtime_settings`、审计日志、Prom 指标 —— 因此放在同一份文档里串讲。

---

## 0. TL;DR

| 维度 | 现状 | 重构后 |
| --- | --- | --- |
| 「目标版本是什么」 | 仅在 `update.sh` 启动后由 shell 调 GitHub API 解析，UI 不可见 | 新增 `GET /admin/update/check`，Redis 缓存 20 min，返回 `current / latest / release_notes / channel / build_type / cached`；运维点击「更新」前即可看到 |
| 「现在该不该点」 | UI 永远显示「一键更新」按钮，没有 up‑to‑date 状态 | 三态卡片：`UP_TO_DATE` / `UPDATE_AVAILABLE` / `UNKNOWN`，按钮文案与启用态随状态变化 |
| 重复触发保护 | 仅文件 marker `.update.running`，单实例 API 才有效 | Redis SETNX + 心跳 + Idempotency‑Key（POST 维度），多副本 API 同样安全 |
| GitHub API 调用次数 | 每次更新走一次；UI 检查更新会再走一次 | 单一缓存源，update.sh 直接消费 API 解析好的 `LUMEN_IMAGE_TAG`，零额外调用 |
| 来源校验 | 依赖 GHCR + docker pull 自带签名/digest | 同左 + 显式 URL 白名单 + tag 形态白名单（拒绝 `latest`/空串/路径穿越） |
| 回滚 | 既有 `/admin/release/rollback`（多 release 任选） | 不变，新增 sub2api 风格「一键回滚到上一版本」快捷入口 |
| 阶段可视化 | 已有 `::lumen-step::` SSE | 不变（继续作为差异化优势保留） |

---

## 1. 现状盘点

> 索引：`apps/api/app/routes/admin_update.py:1` · `apps/api/app/routes/admin_release.py:1` · `scripts/update.sh:1` · `scripts/lib.sh:2306` · `apps/web/src/app/admin/_panels/SettingsPanel.tsx:2393`

### 1.1 后端接口

| Method | Path | 作用 |
| --- | --- | --- |
| `POST` | `/admin/update` | 触发 `scripts/update.sh`；三路执行器：path‑unit → systemd‑run（system → sudo → user）→ detached subprocess |
| `GET`  | `/admin/update/status` | 进程 marker + 日志尾 + `::lumen-step::` 解析 + releases 列表 |
| `GET`  | `/admin/update/stream` | SSE：`state` / `step` / `info` / `log` / `ping` / `done` |
| `GET`  | `/admin/release` | release 列表（含 `is_current` / `is_previous`） |
| `POST` | `/admin/release/rollback` | 切换 `current/previous` 软链 + restart |

### 1.2 update.sh 阶段

`lock → self_update_scripts → check → preflight → backup_preflight → fetch_release → set_image_tag → pull_images → migrate_db → check_storage → switch → restart_services → health_check → cleanup`

每个阶段都通过 `lumen_emit_step` / `lumen_emit_info` 输出协议行；管理端 SSE 解析并渲染 checklist + progress bar。

### 1.3 目标 tag 解析

`scripts/lib.sh:2313` 的 `lumen_image_tag_resolve` 内部直接调用 `https://api.github.com/repos/cyeinfpro/Lumen/releases/latest`。失败时回退 `main`。**API 层完全不知道这个解析过程，UI 也无法在点击之前展示「目标版本是什么」。**

### 1.4 前端

`SettingsPanel.tsx` 的 `LumenUpdateBlock` 已经有完整的 checklist / 日志面板 / 倒计时刷新；但顶部只有「一键更新」按钮，没有「检查更新」按钮，也没有目标版本/变更说明区域。

---

## 2. sub2api 设计借鉴

> 索引（公开仓库 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api)）：
> `backend/internal/handler/admin/system_handler.go` · `backend/internal/service/update_service.go`

### 2.1 关键接口

| Method | Path | 关键字段 |
| --- | --- | --- |
| `GET`  | `/api/v1/admin/system/version` | `{version}` |
| `GET`  | `/api/v1/admin/system/check-updates?force=true|false` | `{current_version, latest_version, has_update, release_info{name,body,published_at,html_url,assets}, cached, warning, build_type}` |
| `POST` | `/api/v1/admin/system/update` | 幂等（`Idempotency-Key` header）+ 系统操作锁 |
| `POST` | `/api/v1/admin/system/rollback` | 单步回滚到 `.backup` |
| `POST` | `/api/v1/admin/system/restart` | 延迟 500 ms 后异步重启 systemd 单元 |

### 2.2 借鉴要点

1. **检查 ≠ 触发**。`CheckUpdate` 与 `PerformUpdate` 是两个独立动作。运维先看「有没有更新 + 改了什么」，再决定要不要点。
2. **缓存兜底**。Redis `update_check_cache` TTL 20 min；GitHub 5xx / 限流时**返回最近一次缓存并打 warning**，而不是直接报错。
3. **强制刷新**。`?force=true` 显式绕过缓存。
4. **Idempotency‑Key**。`POST` 用 `executeAdminIdempotentJSON` 包裹，重复 key 直接返回上次结果，规避双击/重连/网络重试导致的双重执行。
5. **系统级互斥锁**。`SystemOperationLockService` 是跨 update / rollback / restart 的单一互斥点。
6. **build_type 区分**。CI 构建（`release`）与本地编译（`source`）走不同分支，本地编译禁止自动覆盖二进制。
7. **下载安全闭环**。URL 白名单（`github.com` / `objects.githubusercontent.com`）+ HTTPS 强制 + `checksums.txt` SHA‑256 校验 + tar entry 限制（拒绝路径穿越 / 限制 entry name / 限大小防 zip‑bomb）。
8. **need_restart 信号**。完成后返回 `{need_restart: true}`，由前端引导用户/调度器重启服务，业务进程不自杀。

> Lumen 是 Docker 镜像分发，不直接下载二进制，故 §2.2.7 的「文件校验」由 GHCR 拉取链 + image digest 等价覆盖；但「URL/tag 形态白名单」仍需在 API 层做（防止 `LUMEN_IMAGE_TAG` 被注入恶意值）。

---

## 3. 目标设计

### 3.1 设计原则

1. **状态单一可见**：所有 UI 显示的字段（current / latest / cache age / channel / has_update / build_type）都来自同一个 `/admin/update/check` 响应；前端不在多个地方拼合版本。
2. **检查只读、更新写**：`/admin/update/check` 必须可在更新进行中并发调用，不持有锁；`/admin/update` 才取锁。
3. **GitHub API 单点**：仅 API 进程调 `api.github.com`；`update.sh` 通过 env 接收解析结果，shell 内 `lumen_image_tag_resolve` 仅在 API 未注入时作为兜底（兼容手动 `bash update.sh`）。
4. **多副本安全**：marker 升级为 Redis 锁（含 owner / TTL / 心跳），文件 marker 降级为「跨进程可观察的状态镜像」，不再作为互斥源。
5. **可降级**：Redis 不可达 → 回退到文件 marker（保留当前能力，但仅允许单 API 副本）。
6. **零行为回归**：现有的 `POST /admin/update` / `GET /admin/update/status` / `GET /admin/update/stream` 行为兼容，只新增字段；废弃字段保留两个小版本再删。

### 3.2 接口契约

#### `GET /admin/update/version`（新增）

最小化版本探针，留给状态栏 / 心跳轮询用。**不打 GitHub，不读锁，<5 ms。**

```jsonc
{
  "version": "1.1.21",        // 来自 VERSION 文件（lumen_core_version）
  "image_tag": "v1.1.21",     // 来自 LUMEN_IMAGE_TAG env / shared/.env
  "release_id": "2026-05-13-1730-abc1234", // 来自 current/.lumen_release.json
  "sha": "abc1234…",          // 同上
  "channel": "stable",        // 来自 settings: update.channel
  "build_type": "docker"      // docker / source / unknown
}
```

#### `GET /admin/update/check?force={true,false}`（新增）

检查 GitHub Releases，返回**对比结果**。**绝不触发更新。**

```jsonc
{
  "current_version": "1.1.21",
  "latest_version":  "1.2.0",
  "has_update": true,
  "release": {
    "tag": "v1.2.0",
    "name": "v1.2.0 — Wallet Redemption GA",
    "body_md": "## What's new\n- ...",           // 直接转发 GitHub release.body
    "body_html": "<h2>What's new</h2>...",       // 服务端 markdown 渲染，前端直接挂载
    "html_url": "https://github.com/cyeinfpro/Lumen/releases/tag/v1.2.0",
    "published_at": "2026-05-14T08:00:00Z",
    "is_prerelease": false
  },
  "cache": {
    "cached": true,
    "fetched_at": "2026-05-15T03:12:00Z",
    "stale": false,
    "ttl_remaining_sec": 1140
  },
  "channel": "stable",         // resolution input
  "resolved_image_tag": "v1.2.0", // 待 update.sh 消费
  "build_type": "docker",
  "warning": null              // GitHub 不可达 + 命中缓存时填 "Using cached data: ..."
}
```

错误模型：
- GitHub 不可达 + **无缓存** → `200 OK` 且 `has_update=false`, `warning="..."`, `cache.cached=false`（与 sub2api 一致；保持 UI 可渲染，不出 5xx）；
- 上游 5xx + **命中缓存** → `200 OK` 且 `cache.stale=true`, `warning="upstream 503, using stale cache"`；
- 鉴权失败 / channel 非法 → `400 / 401 / 403`。

#### `POST /admin/update`（既有，扩展）

新增 header：`Idempotency-Key: <client-uuid>`（可选；缺省则按 `admin_id + url + body_hash + 30s` 自动生成）。

新增 body 字段（可选）：

```jsonc
{
  "target_tag": "v1.2.0",     // 显式覆盖 check 解析；省略则用 check 缓存
  "force_redeploy": false,    // 当 has_update=false 时仍要重跑（用于配置变更）
  "channel": "stable"         // 覆盖 settings.update.channel
}
```

响应保持现有 `UpdateTriggerOut`，新增 `target_tag` / `idempotency_key` 字段。

#### `POST /admin/update/rollback-previous`（新增便捷入口）

等价于 `POST /admin/release/rollback` 且 `release_id = current.previous`。失败语义：`{"error":{"code":"no_previous"}}` 409。

> 既有 `/admin/release/rollback` 保留，用于「跳过中间版本回到 N 步前」的高级用法。

### 3.3 数据模型

新增 `runtime_settings` key：

| Key | Type | Default | 说明 |
| --- | --- | --- | --- |
| `update.channel` | enum (`stable`,`main`,`pinned`,`minor`,`major`) | `stable` | 与 `LUMEN_UPDATE_CHANNEL` 等价；UI 优先级高于 `shared/.env` |
| `update.check_ttl_sec` | int | `1200` | check 缓存 TTL，0 表示禁用缓存 |
| `update.allow_prerelease` | bool | `false` | 是否把 `prerelease=true` 也当成「有更新」 |
| `update.use_proxy_pool` | bool | 沿用 | — |
| `update.proxy_name` | str  | 沿用 | — |

Redis keys：

| Key | TTL | 说明 |
| --- | --- | --- |
| `lumen:update:check:v1` | `update.check_ttl_sec` | check 缓存（JSON） |
| `lumen:update:lock` | 30 min + 心跳 | 系统级互斥锁（owner = api pod id + started_at） |
| `lumen:update:idempotency:{key}` | 24 h | 幂等键 → 响应快照 |

### 3.4 锁与幂等

```
┌─────────────────────────────────────────────────────────────┐
│  POST /admin/update                                         │
│  1. resolve idempotency_key (header | derive)               │
│  2. Redis GET lumen:update:idempotency:{key}                │
│     → 命中：直接返回缓存的 UpdateTriggerOut（200 + Replayed） │
│  3. Redis SET lumen:update:lock NX EX 1800                  │
│     → 失败：返回 409 update_running                          │
│  4. 写文件 marker（保留 SSE / Python 老路径的可观察性）         │
│  5. 触发 update.sh（三路执行器不变）                          │
│  6. Redis SETEX lumen:update:idempotency:{key} 86400 <resp>  │
└─────────────────────────────────────────────────────────────┘
```

锁释放：update.sh 结束 → `_cleanup_marker_when_done` / path‑unit 完成钩子 → `DEL lumen:update:lock`。心跳：每 60 s `EXPIRE … 1800`，避免长时间镜像拉取超时被误释放。

Redis 不可达：降级到文件 marker；`/admin/update/check` 直接打 GitHub（无缓存）。在 `/admin/update/version` 的响应里追加 `degraded: ["redis"]` 让运维知晓。

### 3.5 update.sh 改动

- 新增 env：`LUMEN_UPDATE_RESOLVED_TAG` —— 由 API 写入 `.update.env`；非空时 `lumen_image_tag_resolve` 直接 `printf "$LUMEN_UPDATE_RESOLVED_TAG"` 返回。
- 新增 env：`LUMEN_UPDATE_IDEMPOTENCY_KEY` —— 仅写入 `::lumen-info::` 便于审计。
- check phase 失败时（current==target 且非 `force_redeploy`），输出 `::lumen-info:: phase=check key=action value=noop_already_latest` —— 既有行为，本次重构不变。
- 删除 `update.sh` 内任何二次调用 GitHub API 的路径（除非 `LUMEN_UPDATE_RESOLVED_TAG` 缺失，作为兜底）。

### 3.6 前端 UX

```
┌──────────────────────────────────────────────────────────────┐
│  当前版本：v1.1.21  · stable channel · docker · 2 小时前检查   │
│                                                              │
│  ╭── 检测到新版本 v1.2.0 ─────────────────────╮  [立即更新]    │
│  │ 发布时间：2026-05-14 16:00                  │  [查看变更]   │
│  │ 摘要（前 6 行 markdown，可展开全文）          │              │
│  ╰──────────────────────────────────────────╯               │
│                                                              │
│  ⓘ 上次更新：2026-05-13 02:14（v1.1.20 → v1.1.21）成功          │
│                                                              │
│  ⌄ 高级选项                                                  │
│    · 强制重新部署（当前已是最新）                              │
│    · 切换 channel（stable / main / pinned / vX.Y.Z）          │
│    · 回滚到上一版本 v1.1.20                                   │
└──────────────────────────────────────────────────────────────┘
```

三态切换：

| state | 卡片色 | 主按钮 | 副信息 |
| --- | --- | --- | --- |
| `UP_TO_DATE` | 绿/中性 | `已是最新` （禁用，hover 显示「强制重部署」次按钮） | `Xm 前从 GitHub 检查` |
| `UPDATE_AVAILABLE` | 琥珀 | `立即更新到 vX.Y.Z` | release notes 折叠 |
| `UNKNOWN`（GitHub 不可达 + 无缓存） | 灰 | `重新检查` | `GitHub 不可达：...` |
| `RUNNING` | 蓝 | `更新进行中…` | 既有 checklist + SSE 日志 |
| `FAILED` | 红 | `重试` | 错误 phase + 回滚按钮 |

交互细节：
1. 进入页面：触发一次 `useAdminCheckUpdateQuery({ force: false })`，命中缓存秒回。
2. 「重新检查」 → `useAdminCheckUpdateQuery({ force: true })`，按钮转 spinner。
3. 「立即更新」 → 弹确认弹窗（含 release.body_html 预览）→ `useTriggerAdminUpdateMutation`，body 带 `target_tag = check.resolved_image_tag`。
4. 「回滚到上一版本」 → `useRollbackPreviousMutation`，弹窗显示 `previous.release_id` + `previous.sha`。

---

## 4. 安全模型

| 风险 | 缓解 |
| --- | --- |
| `target_tag` 注入（如 `;rm -rf /`） | API 层 regex 白名单：`^(v[0-9]+(\.[0-9]+){0,2}|main|latest)$`，update.sh 二次 `lumen_image_tag_is_valid` 校验 |
| GitHub API 被劫持 / DNS 中毒 | TLS pin 由系统 CA 处理；release.body 渲染时统一通过 `markdown-it` + `DOMPurify`（前端） |
| Markdown XSS（release notes） | 服务端预渲染 `body_html` 时禁用 raw HTML，仅允许 GFM 子集；前端使用 `dangerouslySetInnerHTML` 包 `DOMPurify` 二次净化 |
| GitHub rate‑limit (60 req/h unauth) | Redis 缓存 20 min + 单进程 singleflight；force=true 仍走限流——前端给 `force` 加 5 s 客户端冷却 |
| 双击重复触发 | Idempotency‑Key 24 h |
| 跨实例触发 | Redis lock（NX EX）+ 心跳 |
| 锁卡死（API 崩溃） | TTL 30 min 自动到期；marker `started_at > 24h` 视为 stale（既有逻辑保留） |
| GHCR 镜像被替换 | digest pin（既有，依赖 docker compose） |
| 回滚目标 alembic head 不匹配 | 既有 `admin_release.py` 校验保留 |

---

## 5. 实施路线

### Phase 1 — API：检查更新（≈2 PR）

1. **新建** `apps/api/app/services/github_releases.py`
   - `class GitHubReleasesClient`（httpx 异步、支持代理池）
   - `async def fetch_latest(channel, allow_prerelease) -> GitHubRelease`
   - `async def fetch_tag(tag) -> GitHubRelease`
2. **新建** `apps/api/app/services/update_check.py`
   - `class UpdateCheckService` 持有 `cache: RedisCache`, `gh: GitHubReleasesClient`, `version_provider`
   - `async def check(force) -> UpdateCheckOut`（含 stale‑on‑error 兜底）
   - `async def resolve_target_tag(channel, override) -> str`（regex 白名单）
3. **改** `apps/api/app/routes/admin_update.py`
   - 新增 `GET /admin/update/version` / `GET /admin/update/check`
   - 在 `POST /admin/update` 注入 `LUMEN_UPDATE_RESOLVED_TAG` / `LUMEN_UPDATE_IDEMPOTENCY_KEY`
   - 接入 Redis 锁 + 幂等键（新模块 `app/services/system_lock.py`）
4. **新建** `apps/api/app/services/system_lock.py`（参考 sub2api `SystemOperationLockService`）
   - `acquire(operation, owner, ttl) -> Lock | LockBusy`
   - `release(lock, succeeded, reason)`、`heartbeat(lock)`
   - Redis 不可达 → 自动降级到 `_read_marker` 老路径
5. **新建** `apps/api/app/services/idempotency.py`
   - `with_idempotency(key, ttl, fn)`：命中→返回缓存；未命中→执行→缓存

测试：
- `tests/test_admin_update_check.py` — 缓存命中 / 缓存过期 / GitHub 503 / GitHub 4xx / channel=pinned / allow_prerelease
- `tests/test_system_lock.py` — Redis 正常 / Redis 离线降级 / 心跳续约 / 跨进程互斥
- `tests/test_idempotency.py` — 同 key 双发返回相同响应；不同 key 互不干扰

### Phase 2 — 脚本契约（1 PR）

1. **改** `scripts/lib.sh:2313` `lumen_image_tag_resolve`：
   - 首读 `${LUMEN_UPDATE_RESOLVED_TAG-}`，非空则直接返回。
   - 其它分支不变。
2. **改** `scripts/update.sh:609`（check phase）：emit `idempotency_key`、`resolved_tag_source` 信息行。
3. **新增** `scripts/version.py print-runtime`：CI 单点检测三元一致性（`VERSION` vs `LUMEN_IMAGE_TAG` vs `current/.lumen_release.json`）。

测试：
- `tests/test_update_script_resolved_tag.sh`：注入 `LUMEN_UPDATE_RESOLVED_TAG=v9.9.9` 后 check phase 不再调 curl。
- 在 `image-job/tests` 现有 smoke 中加一条「resolved tag 注入路径」断言。

### Phase 3 — 前端（1~2 PR）

1. **新建** `apps/web/src/lib/queries.ts` 中：
   - `useAdminUpdateVersionQuery()`
   - `useAdminCheckUpdateQuery({ force })`
   - `useRollbackPreviousMutation()`
2. **新建** `apps/web/src/lib/apiClient.ts`：`getAdminUpdateVersion` / `checkAdminUpdate` / `rollbackPrevious`
3. **改** `apps/web/src/app/admin/_panels/SettingsPanel.tsx`：
   - `LumenUpdateBlock` props 增加 `check: AdminUpdateCheckOut | undefined`、`onCheck: (force?: boolean) => void`、`onRollbackPrevious: () => void`
   - 顶部三态卡片渲染（见 §3.6）
   - Release notes 折叠组件：`<MarkdownPreview body={check.release.body_html} limitLines={6} />`，使用 `DOMPurify`
   - Idempotency‑Key：mutation 内部 `crypto.randomUUID()`，重试时复用（sub2api 同语义）
4. **新建** `apps/web/src/components/admin/UpdateAvailableCard.tsx`（拆分，避免 SettingsPanel 进一步膨胀）

UI 验证：
- Storybook / Ladle 三态截图
- Playwright e2e：mock `/admin/update/check` 三种返回值，断言按钮态与文案

### Phase 4 — 可观测性（0.5 PR）

- 审计：`admin.update.check` / `admin.update.trigger`（携带 `target_tag`, `idempotency_key`, `cache_hit`）/ `admin.update.rollback`
- Prom 指标：
  - `lumen_update_check_total{result="hit|miss|stale|fail"}`
  - `lumen_update_check_latency_seconds`
  - `lumen_update_lock_state{state="free|held|degraded"}`
  - `lumen_update_run_duration_seconds{result="ok|fail|noop"}`
- 日志统一 `lumen.update` logger，TraceID 沿用 `_admin_common.write_admin_audit_isolated` 的 request_id。

### Phase 5 — 文档 & runbook（0.5 PR）

- 更新 `docs/versioning.md`：说明 channel + check + force_redeploy 语义。
- 新增 `docs/runbooks/update-troubleshooting.md`：
  - 「为什么 has_update=false 但我知道有新版」→ 排查 channel / cache TTL
  - 「GitHub 不可达」→ 开启代理池 / 调 `update.proxy_name`
  - 「lock 卡死」→ `redis-cli DEL lumen:update:lock` + 检查 path‑unit
  - 「Idempotency 命中导致看不到进度」→ `?force=true` 重发 + 解释 24 h 行为

---

## 6. 兼容性与迁移

| 旧客户端 | 行为 |
| --- | --- |
| 旧版前端只调 `POST /admin/update` | 仍可用；API 自动解析 channel + 写入 `LUMEN_UPDATE_RESOLVED_TAG` |
| 旧版 update.sh（无 `LUMEN_UPDATE_RESOLVED_TAG` 支持） | shell 内 `lumen_image_tag_resolve` 仍兜底调 GitHub —— 无回归 |
| 没有 Redis 的小型部署 | 锁/缓存降级到文件 marker；`/admin/update/check` 每次直连 GitHub；UI 卡片显示 `cache.cached=false` |

回滚预案：
- 若新 check 接口在生产暴露问题：`runtime_settings.update.check_ttl_sec=0` 关闭缓存；
- 若锁机制误锁：`redis-cli DEL lumen:update:lock` + 文件 marker 路径自动恢复（既有逻辑）；
- 若前端三态卡片渲染异常：feature flag `LUMEN_WEB_UPDATE_V2`（环境变量 / settings.json）允许回到旧 UI。

---

## 7. 文件改动清单

```
apps/api/app/routes/admin_update.py            # +/admin/update/{version,check}; inject LUMEN_UPDATE_RESOLVED_TAG
apps/api/app/routes/admin_release.py           # +POST /admin/update/rollback-previous（薄壳，复用既有逻辑）
apps/api/app/services/github_releases.py       # NEW —— GitHub Releases httpx 客户端 + 代理池接线
apps/api/app/services/update_check.py          # NEW —— check 服务 + Redis 缓存 + stale-on-error
apps/api/app/services/system_lock.py           # NEW —— Redis 锁 + Redis-down 降级
apps/api/app/services/idempotency.py           # NEW —— 通用幂等键中间件
apps/api/app/runtime_settings_schema.py        # +update.channel / check_ttl_sec / allow_prerelease（按既有 schema 习惯定位）
apps/api/tests/test_admin_update_check.py      # NEW
apps/api/tests/test_system_lock.py             # NEW
apps/api/tests/test_idempotency.py             # NEW
apps/api/tests/test_admin_update.py            # 扩展：覆盖 resolved_tag 注入 / 幂等 / 锁
scripts/lib.sh                                 # ~5 行：lumen_image_tag_resolve 优先读 LUMEN_UPDATE_RESOLVED_TAG
scripts/update.sh                              # ~10 行：check phase 信息行 + idempotency_key 透传
scripts/version.py                             # +print-runtime 子命令
apps/web/src/lib/apiClient.ts                  # +getAdminUpdateVersion / checkAdminUpdate / rollbackPrevious
apps/web/src/lib/queries.ts                    # +useAdminUpdateVersionQuery / useAdminCheckUpdateQuery / useRollbackPreviousMutation
apps/web/src/app/admin/_panels/SettingsPanel.tsx  # 三态卡片接入；LumenUpdateBlock 改入参
apps/web/src/components/admin/UpdateAvailableCard.tsx  # NEW
apps/web/src/components/markdown/MarkdownPreview.tsx   # NEW（markdown-it + DOMPurify）
docs/versioning.md                             # +channel/check 解释
docs/runbooks/update-troubleshooting.md        # NEW
```

---

## 8. 风险 & 待决问题

1. **markdown 渲染服务端 vs 客户端**：服务端预渲染 `body_html` 减少前端依赖体积，但需要在 API 引入 `markdown-it`（或 `mistune`）。备选：纯前端渲染。**建议采用前端渲染**（避免 API 体积膨胀；DOMPurify 已是前端必备）。文档 §3.2 的 `body_html` 字段在该方案下退化为可选/废弃。
2. **是否需要 `/admin/update/restart`**：Lumen 通过 update.sh 内部 `restart_services` 阶段重启容器，前端无需独立按钮；但若引入「source build / 手动模式」可能需要。本期不实现，留接口位。
3. **`channel=main` 的语义**：仍走 GitHub Releases API 还是直接转 `LUMEN_IMAGE_TAG=main`？维持现状（直接 main，跳过 GH 调用），check 返回 `latest_version="main"`，`has_update=null`（前端展示「滚动 main 分支，无法精确比较」）。
4. **Idempotency 与 release notes 滚动**：同一 key 在 24 h 内复用结果，但 GitHub 上可能已经又有新版。预期行为：`POST /admin/update` 的幂等只保护「触发动作」本身，不重复解析 tag；如需更新到更新的 tag，前端必须传新 key（重新 `crypto.randomUUID()`）—— 这是 sub2api 现行行为，建议沿用并在 runbook 写清楚。
5. **`prerelease` 的对接**：GitHub `/releases/latest` 只返回非 prerelease 的最新。要支持 `allow_prerelease=true`，需改用 `/releases?per_page=10` + 客户端过滤。Phase 1 先支持 latest，prerelease 留 Phase 1.5。

---

## 8.5 Part Ⅰ.b — 让更新真正变快 / 无感（必读）

> 诚实评估：§1~§7 只解决了「点之前看得清 + 不重复点 + 不并发触发」的 **UX 层**，并没让物理更新更快。sub2api 是单 Go 二进制（download + atomic rename + restart ≈ 30 s），lumen 是 docker compose 多服务（pull + migrate + recreate ≈ 5–15 min）。下面 4 条把 lumen 的实际更新窗口压到 **首次容器替换 < 30 s、API 0 downtime**，与 sub2api 「点完几秒就好」的体感对齐。

### 8.5.1 预热拉取（warm pull）

**目标**：把最长的一步「`docker compose pull` 多镜像 200~800 MB」从用户感知路径里挪出去。

**做法**：

1. `GET /admin/update/check` 命中 `has_update=true` 时，API 进程异步触发一次预热任务 `warm_pull(resolved_image_tag)`，写 marker `lumen:update:warm:{tag}`（Redis SET NX EX 1800）。
2. 预热任务通过 **既有的 systemd-run / path-unit 三路执行器**（§3.5）执行：
   ```bash
   docker compose -f "$ROOT/current/docker-compose.yml" --profile pull-only \
     pull --include-deps lumen-api lumen-worker lumen-tgbot lumen-web
   ```
   pull-only profile 不创建容器；只下载 layer 到本地 docker 镜像存储。
3. 预热过程通过 `::lumen-step:: phase=warm_pull status=start|done` 输出到 `.update.log`；前端 check 卡片右下角显示「目标镜像已预热 ✓」/「预热中 87%」。
4. 真正点「立即更新」时，`update.sh` 的 `pull_images` 阶段命中本地 cache → docker 仅校验 manifest digest（秒级返回），跳过下载。

**回退**：预热失败不影响主流程；`update.sh` 的 pull 阶段仍能现拉。预热结果有效期 30 min，超时后下次 check 重新触发。

**最小 PoC（API 侧）**：

```python
# apps/api/app/services/update_warm.py
async def maybe_warm_pull(tag: str, lock: SystemLock) -> None:
    """Fire-and-forget warm pull. Idempotent via Redis NX marker."""
    marker_key = f"lumen:update:warm:{tag}"
    if not await lock.redis.set(marker_key, "1", nx=True, ex=1800):
        return  # 已有别人在拉
    try:
        # 写 .warm.trigger 文件；host 上 lumen-update-warm.path 单元 watch 该文件并执行
        trigger = Path(settings.backup_root) / ".warm.trigger"
        trigger.write_text(f"{tag}\n{datetime.now(timezone.utc).isoformat()}\n")
    except OSError as exc:
        await lock.redis.delete(marker_key)
        logger.warning("warm_pull trigger failed: %s", exc)
```

```ini
# deploy/systemd/lumen-update-warm.service —— host 侧
[Service]
Type=oneshot
WorkingDirectory=/opt/lumen/current
ExecStart=/usr/bin/env bash -c \
  'TAG=$(head -n1 /opt/lumendata/backup/.warm.trigger); \
   LUMEN_IMAGE_TAG=$TAG docker compose pull --quiet \
   lumen-api lumen-worker lumen-tgbot lumen-web \
   >> /opt/lumendata/backup/.update.log 2>&1; \
   rm -f /opt/lumendata/backup/.warm.trigger'
```

**收益**：把 5–15 min 总时长压到 **1–3 min**（仅 migrate + 容器替换）。

### 8.5.2 蓝绿切换（zero-downtime container swap）

**目标**：把 API 不可用窗口从「数秒～数十秒」压到 **0**（用户看不到 5xx / 重连）。

**现状**：`update.sh:restart_services` 直接 `docker compose up -d --wait` —— compose 会 stop 旧容器、start 新容器，停止-启动之间 API 不可达，nginx 看到 502。

**做法**：

1. `docker-compose.yml` 把 `lumen-api` 改成可缩放（`scale: 1`），并加 label `lumen.color=blue`（默认）。
2. 新增 compose override 文件 `docker-compose.bluegreen.yml`，定义 `lumen-api-green` 服务，绑定到 `127.0.0.1:18001`（影子端口；blue 在 `:18000`）。
3. `update.sh:restart_services` 改为：
   ```
   ① 起 green：docker compose -f compose.yml -f compose.bluegreen.yml up -d lumen-api-green
   ② 等 green 健康（curl localhost:18001/healthz × 5 次连续 200，超时 60 s）
   ③ nginx upstream weight：blue=100 green=0 → blue=50 green=50 → blue=0 green=100
      每步 `nginx -s reload`（reload 不断连），间隔 3 s
   ④ 等 blue 残留连接 drain（30 s 软上限，看 active connections == 0）
   ⑤ stop blue：docker compose stop lumen-api
   ⑥ rename：把 green 重命名为下一轮的 blue（labels 翻转 + recreate）
   ```
4. lumen-worker / lumen-tgbot 走另一条简化路径（无外部流量，单实例 OK）：先起新、再 SIGTERM 旧，依赖 worker 的 180 s graceful shutdown（既有）。

**回退**：每一步失败都能停在中间状态——nginx upstream 配置版本化（`/etc/nginx/upstream-blue.conf` / `-green.conf`），失败时 `ln -sf upstream-blue.conf current.conf && nginx -s reload` 一行回滚；blue 容器直到 ⑤ 之前都不动。

**与 §3.5 现有阶段协议兼容**：新增 phases `start_green`、`shift_traffic_50`、`shift_traffic_100`、`drain_blue`、`stop_blue`，前端 checklist 自动追加（既有「未识别 phase 追加到末尾」逻辑覆盖）。

**最小 PoC（nginx upstream 切换）**：

```bash
# scripts/lumen-shift-traffic.sh —— 由 update.sh 调用
set -euo pipefail
COLOR="$1"   # green | blue
WEIGHT_NEW="$2"  # 0 | 50 | 100
WEIGHT_OLD=$((100 - WEIGHT_NEW))
NGINX_CONF=/etc/nginx/conf.d/lumen-upstream.conf
TMP=$(mktemp)

cat > "$TMP" <<EOF
upstream lumen_api {
    zone lumen_api 64k;
    server 127.0.0.1:18000 weight=${WEIGHT_OLD} max_fails=2 fail_timeout=5s;
    server 127.0.0.1:18001 weight=${WEIGHT_NEW} max_fails=2 fail_timeout=5s;
    keepalive 32;
}
EOF

# nginx -t 验证；失败则中止，update.sh 接收 rc != 0 自动回滚
nginx -t -c /etc/nginx/nginx.conf -g "include $TMP;" || { rm -f "$TMP"; exit 1; }
mv "$TMP" "$NGINX_CONF"
nginx -s reload
echo "::lumen-info:: phase=shift_traffic key=color value=${COLOR}"
echo "::lumen-info:: phase=shift_traffic key=weight_new value=${WEIGHT_NEW}"
```

**收益**：API 0 downtime；worker drain 已 180 s，覆盖在途任务；tgbot 短暂重连可接受。

### 8.5.3 Expand‑then‑Contract 迁移闸门

**目标**：让 8.5.2 的蓝绿切换在 alembic 迁移期间也成立——**新老代码必须能共用同一份 schema**。

**做法**（CI 闸门 + lint 规则）：

1. 新增 `scripts/lint_alembic_breaking.py`，扫描 PR diff 中的 alembic 版本文件，**拒绝**以下操作（除非 commit message 含 `BREAKING:` 显式声明并附 runbook）：
   - `op.drop_column` / `op.drop_table`
   - `op.alter_column(... nullable=False, server_default=None)`（NOT NULL 必须有 default 或分两步）
   - `op.rename_column` / `op.rename_table`
   - `CHECK` 约束新增（除非 `IS NOT VALID` 后续 VALIDATE 模式）
2. 提供 helper：`packages/core/lumen_core/alembic_expand.py`
   ```python
   def add_column_nullable_then_backfill(table: str, col: sa.Column) -> None: ...
   def add_check_not_valid(table: str, name: str, expr: str) -> None: ...
   def rename_via_alias(table: str, old: str, new: str) -> None:
       """两步：① 新列 + 触发器双写；② 下个版本 drop 旧列。"""
   ```
3. CI 在 PR check 阶段运行 `lint_alembic_breaking.py`，违反时 fail；同时把 PR 描述追加一段 「⚠️ 此 PR 含破坏性迁移，需走停机更新窗口」。
4. update.sh 现有逻辑保留（先 migrate 再 switch），与本闸门正交——闸门只是保证「真到 switch 时新旧代码都能跑」。

**收益**：8.5.2 的蓝绿不会因为 alembic 迁移期间老 API 看不到新列而 5xx。

### 8.5.4 前端无感占位

**目标**：即便底层有 100~500 ms 的连接断裂窗口（nginx reload / TCP 半关闭），最终用户视觉上「完全没事」。

**做法**：

1. **全局 fetch 拦截器** `apps/web/src/lib/apiClient.ts`：
   - 检测 `502 / 503 / 504 / NetworkError / fetch failed` 时，**指数退避自动重试 3 次**（120 ms / 360 ms / 1080 ms），仅对幂等方法（GET / PUT / DELETE / 带 `Idempotency-Key` 的 POST）启用。
   - SSE / WebSocket 连接走专用 `useResilientEventSource(url)`，自动重连 + Last‑Event‑ID。
2. **「服务升级中」全局横幅**：
   - 订阅 `useAdminUpdateStatusQuery` 或公共 `/system/maintenance` 端点；`running=true` 时**所有页面顶部**显示一条琥珀色条幅 `Lumen 正在升级到 vX.Y.Z（预计 Y 分钟）`，含 ETA 倒计时（取 status.phases 的 `dur_ms` 中位数 × 剩余 phase 数估算）。
   - 普通用户卡顿不影响任务结果，只是慢一拍——条幅消除焦虑。
3. **乐观 UI**：在途请求展示 `pending` 占位（既有），不因 502 立刻置为 error；重试成功后无缝替换。
4. **「请稍候」遮罩**：仅在切换流量的 3 s 窗口（`shift_traffic` phase）激活；遮罩透明度 30%，可点击但提示「升级即将完成」。

**最小 PoC（fetch 拦截 + 指数退避）**：

```ts
// apps/web/src/lib/apiClient.ts —— 现有 apiFetch 包一层
const RETRYABLE_STATUS = new Set([502, 503, 504]);
const MAX_RETRY = 3;

export async function apiFetchResilient(url: string, init: RequestInit = {}): Promise<Response> {
  const idempotent = (init.method ?? "GET") === "GET"
    || (init.method === "POST" && init.headers && "Idempotency-Key" in init.headers);
  for (let attempt = 0; attempt <= MAX_RETRY; attempt++) {
    try {
      const r = await fetch(url, init);
      if (!RETRYABLE_STATUS.has(r.status) || attempt === MAX_RETRY || !idempotent) return r;
    } catch (err) {
      if (attempt === MAX_RETRY || !idempotent) throw err;
    }
    await new Promise(rs => setTimeout(rs, 120 * Math.pow(3, attempt)));
  }
  throw new Error("unreachable");
}
```

```tsx
// apps/web/src/components/SystemUpgradeBanner.tsx
export function SystemUpgradeBanner() {
  const q = useAdminUpdateStatusQuery({ refetchInterval: 5000 });
  if (!q.data?.running) return null;
  const phase = q.data.phases.find(p => p.status === "running")?.phase ?? "preparing";
  const remainingMin = estimateRemainingMinutes(q.data.phases);
  return (
    <div role="status" className="bg-amber-500/20 text-amber-200 text-sm px-4 py-2 text-center">
      Lumen 正在升级（{phase} · 预计 {remainingMin} 分钟内完成）·
      请求会自动重试，您可继续操作
    </div>
  );
}
```

### 8.5.5 PR 拆分 / 估时 / 依赖

| PR | 范围 | 估时 | 依赖 | 风险 |
| --- | --- | --- | --- | --- |
| **F1** `feat(update): warm pull on check hit` | 8.5.1 全部；新增 `services/update_warm.py` + path-unit + `--profile pull-only` | 2 人天 | Part Ⅰ §3 已合 | 低；失败不影响主路径 |
| **F2** `feat(update): blue/green compose + nginx shift` | 8.5.2 全部；新增 `docker-compose.bluegreen.yml` / `scripts/lumen-shift-traffic.sh` / `update.sh` 阶段重组 | 4 人天 | F1 | 中；需要 staging 完整演练 |
| **F3** `ci(alembic): expand-then-contract lint` | 8.5.3；`scripts/lint_alembic_breaking.py` + `alembic_expand.py` + CI workflow | 1.5 人天 | 无（可并行） | 低；只是 CI 闸门 |
| **F4** `feat(web): resilient fetch + upgrade banner` | 8.5.4 全部；`apiFetchResilient` / SSE 重连 hook / banner | 1.5 人天 | 无（可并行） | 低；前端独立 |
| **F5** `docs(runbook): blue-green upgrade & rollback` | 写 `docs/runbooks/blue-green-upgrade.md`，含失败矩阵 / nginx 回滚 | 0.5 人天 | F2 | — |

总计：**约 9.5 人天**，可并行 F1+F3+F4，关键路径 F1→F2→F5 约 7 人天。

### 8.5.6 验收（追加到 §9）

- [ ] 点击「立即更新」前，目标镜像已 `docker image inspect` 命中本地（预热生效）
- [ ] 蓝绿切换期间用 `wrk -t4 -c100 -d120s https://lumen.example.com/healthz` 压测：**0 个非 200 响应**
- [ ] worker 的在途生成任务（最长 180 s）在切换期间正确收尾，无丢任务
- [ ] CI 上提交一条 `op.drop_column` PR → check 失败并提示 BREAKING：标记
- [ ] 前端开发者工具人工 throttle 网络到 offline 5 s 期间，UI 不出现错误吐司，所有 GET 自动重试成功
- [ ] 全局升级横幅 ETA 偏差 ≤ 30%

---

## 9. 验收清单

- [ ] `curl /admin/update/version` < 5 ms（无 Redis 调用路径）
- [ ] `curl /admin/update/check`（命中缓存）< 20 ms；冷启动 < 1.5 s
- [ ] GitHub 主动 503 时 `/admin/update/check` 仍 200，含 `warning + cache.stale=true`
- [ ] 两个 API 副本同时 `POST /admin/update` —— 第二个返回 409 `update_running`
- [ ] 同浏览器双击「一键更新」 —— 第二次返回 `replayed=true` + 完全相同 `started_at`
- [ ] 关掉 Redis：API 不崩；`/admin/update/version` 返回 `degraded:["redis"]`；触发流程仍走文件 marker
- [ ] 注入 `LUMEN_UPDATE_RESOLVED_TAG=v1.2.0` 后，`update.sh` 不再 `curl api.github.com`
- [ ] 前端三态卡片在 `UP_TO_DATE` / `UPDATE_AVAILABLE` / `UNKNOWN` / `RUNNING` / `FAILED` 五种快照下渲染稳定
- [ ] release.body Markdown 中带 `<img onerror=...>` —— 前端不执行
- [ ] 回滚后 `current/.lumen_release.json.sha` 与 `/admin/update/version.sha` 一致

---

## 附录 A：与 sub2api 的契约对照表

| sub2api | Lumen 对应物 | 差异说明 |
| --- | --- | --- |
| `GET /admin/system/version` | `GET /admin/update/version` | Lumen 多 release_id / channel / build_type |
| `GET /admin/system/check-updates` | `GET /admin/update/check` | Lumen 增 `channel / resolved_image_tag / cache.ttl_remaining_sec` |
| `POST /admin/system/update` | `POST /admin/update`（保留路径） | Lumen 是异步 SSE，sub2api 是同步阻塞 |
| `POST /admin/system/rollback` | `POST /admin/update/rollback-previous` | Lumen 同时保留 `/admin/release/rollback` 多步回滚 |
| `POST /admin/system/restart` | — | Lumen 由 update.sh 末尾 `restart_services` 阶段承担 |
| `SystemOperationLockService` | `app/services/system_lock.py` | 同语义，新增 Redis‑down 降级 |
| `executeAdminIdempotentJSON` | `app/services/idempotency.py` | Header 名/TTL 默认值保持一致以减小心智负担 |
| `validateDownloadURL` | `app/services/github_releases.py:_validate_url` | 适配 GHCR + GitHub Releases；不下载二进制 |
| `checksums.txt 校验` | 不需要（GHCR digest 等价） | — |

---
---

# Part Ⅱ：计费系统重构

> 索引：`packages/core/lumen_core/billing.py:1` · `apps/worker/app/billing.py:1` · `packages/core/lumen_core/models.py:968` · `apps/worker/app/tasks/completion.py:2782`
> sub2api 索引：`backend/internal/service/billing_service.go` · `backend/internal/service/billing_cache_service.go` · `backend/internal/service/usage_billing.go`

## 10. TL;DR

| 维度 | 现状 | 重构后 |
| --- | --- | --- |
| 模型计费维度 | `per_1k_tokens_in` + `per_1k_tokens_out` 两个费率 | `input` / `output` / `cache_read` / `cache_creation`（含 5m / 1h）/ `image_output` / `priority_tier_*` / `long_context_*` 八档，与 sub2api / LiteLLM 对齐 |
| Prompt cache 计费 | **命中缓存仍按 input 全价扣** —— 用户被多扣 5~10× | `cache_read` 按官方 ~0.1× input、`cache_creation` 按 1.25× input 单独计费；上游未拆分时回退到 5m 单价 |
| `Completion` 数据列 | 仅 `tokens_in` / `tokens_out` | 新增 `cache_read_tokens` / `cache_creation_tokens` / `cache_creation_5m_tokens` / `cache_creation_1h_tokens` / `reasoning_tokens` / `image_output_tokens` |
| 上游 usage 解析 | 只识别 `input_tokens` / `prompt_tokens` / `output_tokens` / `completion_tokens` | 同时识别 `cache_read_input_tokens` / `cache_creation_input_tokens` / `cached_tokens` / `prompt_token_details.cached_tokens` / `cache_creation.ephemeral_5m_input_tokens` / `cache_creation.ephemeral_1h_input_tokens` |
| 定价表查询 | 每条 completion 2× DB SELECT，按字符串精确匹配 model | 进程内 LRU + Redis 缓存（singleflight）；支持 model alias / wildcard（`gpt-4o-*`）/ 区间分层（>200k token 走长上下文档） |
| 余额读 | 每次扣费前 SELECT `user_wallets` + 行锁 | 余额走 `BillingCacheService`：Redis 主，DB 兜底，singleflight 合流，扣减由 worker pool 异步写回 + 同步回退 |
| 速率限制 | 仅 Lua-based RPS 限流（`apps/api/app/ratelimit.py`） | 新增 5h / 1d / 7d 滑动窗口 token 用量（与 sub2api `APIKeyRateLimitCacheData` 同语义），用于 BYOK 额度 / 套餐限额 |
| 网关幂等 | 仅 `complete:{completion.id}` 一处 ledger 幂等键 | `UsageBillingCommand.RequestFingerprint`（含 model+tokens+cost SHA-256）+ Idempotency-Key 头双保险 |
| Rate multiplier | 无（所有用户 1.0×） | `users.billing_rate_multiplier` 列（default 1.0）+ `accounts.rate_multiplier`（团队/套餐覆盖）；合并优先级 admin override > account > user > 1.0 |
| 长上下文计费 | 无 | 单会话总 token 超过 `pricing.long_context_threshold` 后，input 与 output 都乘上 `long_context_*_multiplier`（OpenAI gpt‑5.4 等模型用） |
| 图像输出 token | 与文本输出共用 `tokens_out` | 拆出 `image_output_tokens`，按 `image_output_price_per_token` 单算（OpenAI 4o image / Gemini image） |
| 价格表来源 | 仅 DB `pricing_rules`，运维手填 | 增 `packages/core/lumen_core/pricing_fallback.py` 内置 ~30 个主流模型 fallback；DB 缺失时自动降级，不静默扣 0 |

---

## 11. 现状盘点

### 11.1 数据模型（lumen）

| 表 | 关键列 | 不足 |
| --- | --- | --- |
| `user_wallets` | `balance_micro / hold_micro / lifetime_topup_micro / lifetime_spend_micro / version` | 缺 `billing_rate_multiplier`、缺缓存版本号给 ETag |
| `wallet_transactions` | `kind / amount_micro / balance_after / hold_after / ref_type / ref_id / idempotency_key / meta` | 缺 `request_fingerprint`；`kind` 集合未覆盖 `topup_subscription / charge_cache_read / charge_cache_creation` |
| `pricing_rules` | `(scope, key, variant, unit)` 唯一 | `unit` 只有 `per_1k_tokens_in / per_1k_tokens_out / per_image`；没有 `per_token_cache_read / per_token_cache_creation_5m / …` |
| `completions` | `tokens_in / tokens_out / model / status` | 缺 cache_read / cache_creation 列；缺 `reasoning_tokens` |
| `generations` | width/height 已在 metadata | 不在本期范围 |

### 11.2 关键路径

- `apps/worker/app/billing.py:224 charge_completion` —— 计费入口（worker），每次：
  1. `_account_mode` SELECT users（无缓存）
  2. `_billing_enabled` settings SELECT（runtime_settings 有内存 LRU，OK）
  3. `_thresholds` settings SELECT
  4. `_existing_wallet_tx` SELECT
  5. `estimate_completion_cost` —— 2× SELECT pricing_rules（`per_1k_tokens_in` / `per_1k_tokens_out`）
  6. `billing_core.charge` —— SELECT … FOR UPDATE user_wallet + INSERT wallet_transactions + 再次 `_existing_tx` 双检
  > 单 completion ≥ 7 次 DB round‑trip；高峰期 worker 池压 PG 严重。

- `apps/worker/app/tasks/completion.py:2786` —— 上游响应解析仅取 `input_tokens` / `prompt_tokens` / `output_tokens` / `completion_tokens`。**所有缓存命中信息直接丢弃。**

### 11.3 失真点

1. **Anthropic prompt caching**：上游响应里 `usage.cache_read_input_tokens=10000, cache_creation_input_tokens=2000, input_tokens=500` 时，lumen 取 `input_tokens=500`（忽略另外 12000），按 input 单价扣 500，**实际官方账单按 (500×1 + 2000×1.25 + 10000×0.1)×input_rate = 4000 等效 input** —— 漏扣 8×。
2. **OpenAI prompt caching**：`usage.prompt_tokens_details.cached_tokens=8000` 表示在 `prompt_tokens` 内部，cached 占 8000，需要把这 8000 按 0.5× 计；lumen 把整个 `prompt_tokens` 都按 input 全价扣 —— 多扣 4×。
3. **gpt‑4o image gen**：`usage.output_tokens_details.image_tokens=3000`，按 `$0.0004/token` 计；lumen 全部塞进 `tokens_out` 按文本输出 `$0.000004` 计 —— 漏扣 100×。
4. **PricingRule 缺失静默扣 0**：现在 `estimate_completion_cost` 在 `in_rate=None` 时返回 0，仅写一条 `pricing.not_configured` 审计日志。**用户免费用了，运维不上报警**。
5. **PricingRule 表热点**：高峰期每秒可能 N 次 completion → 2N 次 PricingRule SELECT。

---

## 12. 借鉴 sub2api 设计

### 12.1 核心抽象

```text
                ┌────────────────────────────────────────┐
   incoming     │       BillingService.CalculateCost     │
   ─────────►   │   ┌──────────────────┐                 │
                │   │ PricingResolver  │  ← DB + Redis   │
                │   │  (intervals, alias, channel)       │
                │   └──────────────────┘                 │
                │       │                                │
                │       ▼                                │
                │  computeTokenBreakdown(                │
                │     pricing, UsageTokens,              │
                │     rateMultiplier, serviceTier,       │
                │     applyLongCtx)                      │
                └────────────────────────────────────────┘
                        │
                        ▼
                  CostBreakdown(
                    InputCost, OutputCost,
                    CacheCreationCost, CacheReadCost,
                    ImageOutputCost,
                    TotalCost, ActualCost,
                    BillingMode)
                        │
                        ▼
              BillingCacheService.DeductBalance
                 ├ singleflight on load
                 ├ async worker pool on write
                 └ Redis ↔ DB invariant via outbox
```

### 12.2 关键代码引用

- **多档定价结构** `service/billing_service.go:43` `ModelPricing { Input, Output, CacheCreation, CacheRead, CacheCreation5m, CacheCreation1h, LongContextInputMultiplier, ImageOutput, Priority* }`
- **统一计费入口** `service/billing_service.go:416 CalculateCostUnified` —— 三种 mode（token / per_request / image）
- **缓存创建分桶** `service/billing_service.go:546 computeCacheCreationCost` —— ephemeral 5m / 1h 拆分，上游未拆时回退 5m 单价
- **服务等级倍率** `service/billing_service.go:472 computeTokenBreakdown` 的 `tierMultiplier` 与 `usePriorityServiceTierPricing`
- **余额缓存 + singleflight** `service/billing_cache_service.go:288 GetUserBalance` —— Redis 命中直返；未命中 `singleflight.Do` 合流回源；回源后异步 `enqueueCacheWrite`
- **异步写 worker pool** `service/billing_cache_service.go:160 startCacheWriteWorkers` —— 固定 10 worker + 1000 缓冲；满则同步回退（关键任务）或丢弃 + throttled 日志（非关键）
- **网关粒度幂等** `service/usage_billing.go:14 UsageBillingCommand.RequestFingerprint` —— SHA‑256(model | tokens | cost) 与 Idempotency‑Key 互补
- **回退价格** `service/billing_service.go:140 initFallbackPricing` —— 内置 ~30 个主流模型 fallback（claude / gpt / gemini / 等）

---

## 13. 目标设计

### 13.1 原则

1. **失真优先于性能**：先把 cache token 扣对（哪怕初版还在打 DB），再讲缓存优化。
2. **价格表唯一权威**：DB > Redis 缓存 > 进程 LRU > 内置 fallback；任意一层缺失都降级而不静默扣 0。
3. **幂等三层**：Idempotency‑Key 头（API 侧 24 h）→ `RequestFingerprint`（ledger meta 层 7 d）→ `wallet_transactions.idempotency_key` 唯一索引（DB 层永久）。
4. **可观测**：每个失真维度（cache_read 漏扣 / pricing fallback / overdraw / rate_limit hit）都有 Prom 指标 + 审计事件。
5. **可关停**：所有新行为受 `runtime_settings.billing.*` 开关，可独立回退到旧 path（feature flag）。
6. **零静默回归**：未配置 cache 价格的模型，新流程按 **input 全价**扣 cache token —— 与现状等价，避免「升级 lumen 后所有 Anthropic 模型扣费翻倍」。

### 13.2 数据迁移

Alembic `0024_billing_cache_tokens.py`：

```python
def upgrade():
    op.add_column("completions", sa.Column("cache_read_tokens", sa.Integer, nullable=False, server_default="0"))
    op.add_column("completions", sa.Column("cache_creation_tokens", sa.Integer, nullable=False, server_default="0"))
    op.add_column("completions", sa.Column("cache_creation_5m_tokens", sa.Integer, nullable=False, server_default="0"))
    op.add_column("completions", sa.Column("cache_creation_1h_tokens", sa.Integer, nullable=False, server_default="0"))
    op.add_column("completions", sa.Column("reasoning_tokens", sa.Integer, nullable=False, server_default="0"))
    op.add_column("completions", sa.Column("image_output_tokens", sa.Integer, nullable=False, server_default="0"))

    op.add_column("user_wallets", sa.Column("billing_rate_multiplier", sa.Numeric(8, 4), nullable=False, server_default="1.0000"))

    # 不破坏现有 pricing_rules；通过新增 unit 值来扩展。
    # 既有 (chat_model, gpt-4o, default, per_1k_tokens_in) 行不动。
    # 新增以下 unit 枚举（应用层 validate）：
    #   per_1k_tokens_cache_read
    #   per_1k_tokens_cache_creation
    #   per_1k_tokens_cache_creation_5m
    #   per_1k_tokens_cache_creation_1h
    #   per_1k_tokens_image_output
    #   per_1k_tokens_reasoning
    #   per_1k_tokens_input_priority
    #   per_1k_tokens_output_priority
    #   per_1k_tokens_cache_read_priority
    #   long_context_threshold
    #   long_context_input_multiplier  (×10000 存 int)
    #   long_context_output_multiplier
```

### 13.3 价格解析（PricingResolver）

```python
# packages/core/lumen_core/pricing.py
@dataclass(frozen=True)
class ModelPricing:
    input_per_1k_micro: int
    output_per_1k_micro: int
    cache_read_per_1k_micro: int           # 默认 input × 0.1
    cache_creation_per_1k_micro: int       # 默认 input × 1.25
    cache_creation_5m_per_1k_micro: int    # 默认 = cache_creation
    cache_creation_1h_per_1k_micro: int    # 默认 cache_creation × 1.6
    image_output_per_1k_micro: int         # 默认 output（不拆分则等价）
    reasoning_per_1k_micro: int            # 默认 output
    input_priority_per_1k_micro: int       # 0 = 无 priority 档
    output_priority_per_1k_micro: int
    cache_read_priority_per_1k_micro: int
    long_context_threshold_tokens: int     # 0 = 不启用
    long_context_input_multiplier_x10000: int   # 整数化避免浮点漂移
    long_context_output_multiplier_x10000: int
    supports_cache_breakdown: bool

class PricingResolver:
    """按 (model, channel) 解析；命中顺序：
       1. settings:pricing.override.<model>  (admin 热改)
       2. DB pricing_rules
       3. process LRU (10s TTL)
       4. Redis lumen:pricing:v1:{model}  (60s TTL)
       5. packages/core/lumen_core/pricing_fallback.py  (内置)
       6. 全 0（极少；输出 pricing.not_configured 审计 + Prom 计数）"""
```

### 13.4 用量解析（UsageTokens）

```python
@dataclass(frozen=True)
class UsageTokens:
    input_tokens: int
    output_tokens: int                       # 含 image / reasoning 子项的总输出
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    reasoning_tokens: int = 0
    image_output_tokens: int = 0

def parse_usage(provider: str, usage: dict[str, Any]) -> UsageTokens:
    """供应商无关解析：
       - Anthropic: input_tokens / output_tokens / cache_read_input_tokens /
         cache_creation_input_tokens / cache_creation.ephemeral_*
       - OpenAI Responses: input_tokens / output_tokens /
         input_tokens_details.cached_tokens /
         output_tokens_details.reasoning_tokens /
         output_tokens_details.image_tokens
       - OpenAI Chat: prompt_tokens / completion_tokens /
         prompt_tokens_details.cached_tokens /
         completion_tokens_details.reasoning_tokens
       - Gemini: promptTokenCount / candidatesTokenCount /
         cachedContentTokenCount"""
```

### 13.5 成本计算

`CostBreakdown` 字段固定：

```jsonc
{
  "input_cost_micro": 12345,
  "output_cost_micro": 67890,
  "cache_read_cost_micro": 1234,
  "cache_creation_cost_micro": 5678,
  "image_output_cost_micro": 0,
  "reasoning_cost_micro": 0,
  "long_context_applied": false,
  "priority_tier_applied": false,
  "rate_multiplier_x10000": 10000,
  "total_cost_micro": 87147,
  "actual_cost_micro": 87147,             // total × rate_multiplier
  "billing_mode": "token",
  "pricing_source": "db|redis|process|fallback|missing"
}
```

服务层：

```python
class BillingService:
    def calculate(self,
                  *,
                  model: str,
                  usage: UsageTokens,
                  rate_multiplier_x10000: int,
                  service_tier: str = "standard",
                  channel: str | None = None) -> CostBreakdown:
        pricing = self.resolver.resolve(model, channel=channel)
        return compute_breakdown(pricing, usage, rate_multiplier_x10000, service_tier)
```

`compute_breakdown` 是纯函数（无 IO），便于在 Property test 中校验。整数运算全程：所有价格以 `micro_per_1k_tokens` 存储，先乘 tokens 再 `// 1000`，最后乘 `rate_multiplier_x10000` 再 `// 10000`，避免浮点漂移（与现有 `wallet_transactions.amount_micro` int64 同语义）。

### 13.6 余额缓存（BillingCacheService）

借鉴 sub2api `BillingCacheService` 拆 Python 版：

```
apps/api/app/services/billing_cache.py     # 余额 + 套餐缓存 + RPM 计数
apps/worker/app/services/billing_cache.py  # 复用前者；仅工作池启停时机不同
```

关键 API：

```python
class BillingCacheService:
    async def get_balance(self, user_id: str) -> int: ...          # μRMB
    async def queue_deduct(self, user_id: str, micro: int) -> None: ...
    async def deduct_sync(self, user_id: str, micro: int) -> int: ... # 队列满时回退
    async def invalidate(self, user_id: str) -> None: ...
    async def get_window_usage(self, key_id: str) -> WindowUsage: ...
    def queue_window_increment(self, key_id: str, micro: int) -> None: ...
```

实现要点：
- **singleflight**：用 `asyncio.Lock` 字典或 `aiotools.LRUCache` 合流，避免 N 个并发读取打 DB N 次。
- **Worker pool**：`asyncio.Queue(maxsize=1000)` + 10 个 worker task；每个 worker 拉一条 `CacheWriteTask` 执行 `redis.set / decrby`，超时 2 s。
- **背压**：队列满 → 关键任务（余额扣减）同步回退到 `deduct_sync`；非关键任务（窗口用量）丢弃，throttled INFO 日志（5 s 一次）+ Prom 计数。
- **失效广播**：管理后台 adjust / topup → 写 DB 后发 `lumen:billing:invalidate` pub/sub，多副本 API 立即清缓存。

### 13.7 网关粒度幂等

```python
@dataclass(frozen=True)
class UsageBillingCommand:
    request_id: str
    user_id: str
    completion_id: str | None
    generation_id: str | None
    api_key_id: str | None
    account_type: str             # "user" | "service" | "byok"
    model: str
    service_tier: str
    billing_type: int             # 0 = token / 1 = per_request / 2 = image
    tokens: UsageTokens
    cost: CostBreakdown
    request_fingerprint: str = field(default="")  # auto: sha256

def build_fingerprint(cmd: UsageBillingCommand) -> str:
    """与 sub2api 兼容字段顺序，便于跨系统对账：
       user|account|api_key|model|tier|billing_type|in|out|cr|cw|cw5m|cw1h|img|total|actual"""
```

Apply 入口同时：
1. 检查 `Idempotency-Key` 头（API 24 h）
2. 检查 `WalletTransaction.idempotency_key = "complete:<id>"`
3. 检查 `WalletTransaction.meta->>'request_fingerprint' = <fingerprint>`（防止同 completion_id 因 retry 重新算了不同 cost 还成功扣两次）

### 13.8 速率限制（滑动窗口 token 用量）

新增 Redis 结构（与 sub2api `APIKeyRateLimitCacheData` 对齐）：

```
lumen:billing:rl:{api_key_id} → HASH {
  usage_5h, window_5h_started_at_unix,
  usage_1d, window_1d_started_at_unix,
  usage_7d, window_7d_started_at_unix
}
```

`UserApiCredential` 增列：`limit_5h_micro / limit_1d_micro / limit_7d_micro`（0 = 不限）。

evaluate：
- 在 hold/charge 之前调用 `evaluate_rate_limits(api_key, projected_micro)`，超限抛 `RATE_LIMIT_EXCEEDED 429`（HTTP `Retry-After` 取 max(window_reset_time, now+1s)）；
- 实际扣费成功后异步 `queue_window_increment`。

### 13.9 Rate multiplier

- `users.billing_rate_multiplier` Numeric(8,4) default 1.0000，admin 后台可改；
- 加 `wallet_transactions.meta.rate_multiplier_x10000` 留痕；
- 当 `multiplier < 0` 直接夹到 0（与 sub2api `billing_service.go:432` 同语义），防止误配负数变成「免费 + 加余额」。

---

## 14. 接口契约（新增 / 变更）

#### `GET /admin/billing/pricing`（既有，扩展）

返回行 schema 增字段：`unit ∈ { ..., per_1k_tokens_cache_read, per_1k_tokens_cache_creation, per_1k_tokens_cache_creation_5m, per_1k_tokens_cache_creation_1h, per_1k_tokens_image_output, per_1k_tokens_reasoning, per_1k_tokens_input_priority, ... }`。

#### `POST /admin/billing/pricing/bulk`（新增）

`{ "model": "claude-sonnet-4-6", "channel": null, "rates": { "input": ..., "output": ..., "cache_read": ..., "cache_creation": ..., "cache_creation_5m": ..., "long_context_threshold": 200000, "long_context_input_multiplier": 2.0 } }` —— 一次性写入 8~12 行 PricingRule，单事务、原子。

#### `GET /admin/billing/usage/{user_id}`（新增）

聚合 `wallet_transactions.meta` 中 cache_read / cache_creation / image_output 等子项；前端可画堆叠柱状图。

#### `POST /v1/chat/completions`（既有网关）

新增请求头：`Idempotency-Key: <client-uuid>` —— 同 key 在 24 h 内复用 ledger，第二次返回上次 cost；解决「客户端断线 retry 重复扣费」。

#### `GET /me/billing/snapshot`（既有，扩展）

返回新增字段：

```jsonc
{
  "balance_micro": ...,
  "billing_rate_multiplier": "1.0000",
  "windows": {
    "5h": { "used_micro": ..., "limit_micro": ..., "resets_at": "..." },
    "1d": { ... },
    "7d": { ... }
  },
  "by_kind_30d": {
    "input": ..., "output": ..., "cache_read": ..., "cache_creation": ..., "image": ...
  }
}
```

---

## 15. 实施路线

### Phase B1 — 数据层（1 PR）

1. Alembic `0024_billing_cache_tokens` —— 加列 / 不删除既有列。
2. `packages/core/lumen_core/pricing_fallback.py` —— 内置 ~30 个主流模型（claude‑opus‑4‑7、claude‑sonnet‑4‑6、claude‑haiku‑4‑5、gpt‑5.4、gpt‑5.4‑mini、gpt‑4o、gpt‑4o‑mini、o3、o3‑mini、gemini‑2.5‑pro、gemini‑2.5‑flash 等），引用 sub2api `initFallbackPricing` 的数值表，**单位统一换算到 µRMB / 1k tokens**（汇率走 `settings.billing.usd_to_cny`，default 7.2）。
3. `parse_usage()` 单元测试覆盖 4 大供应商响应 schema。

### Phase B2 — 计费核心（1 PR）

1. 新建 `packages/core/lumen_core/pricing.py` —— `ModelPricing`、`UsageTokens`、`CostBreakdown`、`compute_breakdown`（纯函数）。
2. 新建 `packages/core/lumen_core/pricing_resolver.py` —— 6 层 fallback；进程内 `cachetools.TTLCache(maxsize=512, ttl=10)`。
3. 修改 `packages/core/lumen_core/billing.py:277 estimate_completion_cost` —— 委托给 `compute_breakdown`；保留旧签名，append `cache_read_tokens=...` kwargs。
4. 修改 `apps/worker/app/billing.py:224 charge_completion`：
   - 从 `completion` 读取所有 6 个 token 列；
   - 调 `billing_service.calculate(...)`；
   - 把 `CostBreakdown` 整个写入 `wallet_transactions.meta`（便于对账与展示）；
   - 把 `tx.kind` 从 `charge` 拆为 `charge_completion`（向后兼容旧 `charge` 仍读）。
5. 修改 `apps/worker/app/tasks/completion.py:2786` —— `parse_usage(provider, usage)` 替代旧两行；持久化 6 个 token 列。
6. Property test：`compute_breakdown(any pricing × any usage)` ≥ 0、`actual_cost = total × rate / 10000`、`cache_read_cost ≤ input_cost`。

### Phase B3 — 缓存层（1 PR）

1. 新建 `apps/api/app/services/billing_cache.py` + `apps/worker/app/services/billing_cache.py`（worker 入口实例化 + lifespan 启动 worker pool）。
2. 替换 `billing_core.get_wallet` 调用点：读侧走 `BillingCacheService.get_balance`；写侧仍持 `SELECT … FOR UPDATE` 行锁（避免 Redis 与 DB 双写竞态），但额外 `queue_deduct(user_id, amount)`，让前端余额显示秒级生效。
3. 管理后台 `topup` / `adjust` / `rollback` → `invalidate(user_id)` 走 pub/sub。
4. 关掉 Redis 时降级：`BillingCacheService.cache is None` 全路径退到 DB（与 sub2api `s.cache == nil` 同语义）。

### Phase B4 — 网关幂等 & RL（1 PR）

1. API gateway middleware：消化 `Idempotency-Key` 头 → 短路返回缓存结果（参考一键更新的 `app/services/idempotency.py`，复用）。
2. `UsageBillingCommand` 落地：worker 在 charge 前 build fingerprint 并存入 ledger meta；同 fingerprint 24 h 内返回首次结果。
3. `evaluate_rate_limits` 接入 `BillingCacheService.get_window_usage` —— hold 前/charge 前两阶段都查；超限抛 429。
4. 前端 `/me/billing/snapshot` 加 windows 字段；管理后台「限额」tab 可改 limit。

### Phase B5 — 可观测 & 收口（0.5 PR）

Prom 指标：

```
lumen_billing_cost_micro_total{kind="input|output|cache_read|cache_creation|image|reasoning"}
lumen_billing_overdraw_micro_total
lumen_billing_pricing_source_total{source="db|redis|process|fallback|missing"}
lumen_billing_cache_hit_ratio{layer="balance|pricing|window"}
lumen_billing_async_write_queue_len
lumen_billing_async_write_dropped_total{kind="..."}
lumen_billing_rate_limit_block_total{window="5h|1d|7d"}
lumen_billing_idempotency_replay_total
```

审计事件：

```
wallet.charge.completion           # 已有，meta 扩展
wallet.charge.completion.cache_read    # 新增（meta.cache_read_cost > 0）
billing.pricing.fallback_used      # fallback 路径
billing.pricing.missing            # 全 0 兜底（最严重）
billing.rate_limit.blocked
billing.idempotency.replayed
```

### Phase B6 — 回滚预案

`runtime_settings`：

| key | 默认 | 作用 |
| --- | --- | --- |
| `billing.cache_aware` | `1` | 0 → 关闭 cache_read/creation 解析，恢复旧两档 |
| `billing.use_redis_cache` | `1` | 0 → 全走 DB（应急 Redis 故障） |
| `billing.fingerprint_required` | `0` | 1 → 强制 fingerprint 一致才放行 charge（迁移完成后开） |
| `billing.window_rate_limit` | `0` | 默认关；运维显式开启 5h/1d/7d 限额 |
| `billing.allow_negative_balance` | 沿用 | — |
| `billing.usd_to_cny` | `7.2` | fallback 价格换算汇率 |

---

## 16. 安全 / 风险

| 风险 | 缓解 |
| --- | --- |
| 升级后 Anthropic 模型扣费翻倍（用户感知差） | 默认 `cache_read = input × 0.1`、`cache_creation = input × 1.25`；运维如未配置 cache 价格，cache_read 仍按 input 全价（与现状等价，不静默涨价）；新增 `billing.cache_aware=1` 才开启拆分 |
| Provider 误报 cache token（上游 bug 把 input 全部塞 cache_read） | 应用层 invariant：`cache_read ≤ input + cache_read`（恒真），但 `cache_read / (cache_read + input) > 0.95` 时记 warning 审计，便于运营复核 |
| Fingerprint 计算变动导致历史 idempotency 失效 | fingerprint 函数 `v1` 标签内置；变更时 bump 为 `v2`，旧 fingerprint 仍按 v1 校验，新写入用 v2 |
| Redis pub/sub 丢失 invalidate | TTL 兜底（balance 5 min, pricing 60 s）；管理后台改价后强制本地 `invalidate` 后再返回响应 |
| Async write 队列丢失关键扣减 | 关键 `cacheWriteDeductBalance` 队列满时同步回退；DB 是单一权威源（Redis 只是镜像） |
| Rate multiplier 改 0 把整租户变免费 | admin 后台改值时弹确认；变更写 `audit_log` + Prom 计数；`rate_multiplier_x10000 = 0` 在前端高亮红色 |
| 价格回退表过时 | CI 测试 `tests/test_pricing_fallback_freshness.py` 每月运行，对比 [LiteLLM model_prices](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) 报告差异（不强制 fail，仅提醒） |
| 长上下文倍率与区间定价双计 | `applyLongCtx = len(intervals) == 0`（与 sub2api 一致）；区间定价已自带分层时跳过倍率 |

---

## 17. 验收清单

- [ ] Anthropic claude‑sonnet‑4‑6 一条 completion，usage = `{input:500, output:300, cache_read_input:10000, cache_creation_input:2000}`，扣费 = `(500×input + 2000×cache_creation + 10000×cache_read + 300×output) × rate_multiplier`；wallet_transactions.meta 含全部 6 子项。
- [ ] OpenAI gpt‑5.4 一条 completion，usage = `{prompt:8000, completion:1500, prompt_tokens_details.cached_tokens:6000}`，扣费 = `2000×input + 6000×cache_read + 1500×output`。
- [ ] gpt‑4o image generation，usage = `{prompt:500, completion:3500, completion_tokens_details.image_tokens:3000}`，扣费 = `500×input + 500×output + 3000×image_output`。
- [ ] PricingRule 表为空但有 fallback，扣费走 fallback，`pricing_source=fallback` Prom 计数 +1，审计 `pricing.fallback_used`。
- [ ] PricingRule 与 fallback 都为空（极端），扣费 0，审计 `pricing.missing`，Prom `pricing_source=missing` +1。
- [ ] Redis 关掉：`BillingCacheService.get_balance` 走 DB；`queue_deduct` 直接 noop，hot path 仍能扣对，仅是余额变更对前端可见有 ~1 s 延迟。
- [ ] 同 `Idempotency-Key` 24 h 内重复 `POST /v1/chat/completions`，第二次返回与第一次同样的 cost / completion_id，不重复扣费。
- [ ] 同 completion 因 worker retry 触发两次 `charge_completion`：fingerprint 命中，第二次返回 replay audit，不双扣。
- [ ] `rate_multiplier_x10000 = 0` 用户：所有 charge 写 ledger 但 amount_micro=0，余额不变；审计 `wallet.charge.zero_rate`。
- [ ] 5h 窗口配置 100 元限额，用户 1 小时内消耗到 100 元 → 第 101 次请求返回 429，`Retry-After: <secs>`；窗口推进后恢复。
- [ ] 高并发压测：单用户 100 并发请求 → DB SELECT `user_wallets` ≤ 100（不是 100×N，证明 singleflight 工作）；Redis HINCRBY 调用按 worker pool 节奏。

---

## 附录 B：lumen × sub2api 计费维度对照

| sub2api | lumen 当前 | lumen 重构后 |
| --- | --- | --- |
| `ModelPricing.InputPricePerToken` | `pricing_rules.unit=per_1k_tokens_in` | `pricing_rules.unit=per_1k_tokens_in`（兼容）+ ModelPricing.input_per_1k_micro |
| `ModelPricing.OutputPricePerToken` | `per_1k_tokens_out` | 同上 |
| `ModelPricing.CacheReadPricePerToken` | — | `per_1k_tokens_cache_read`（新增） |
| `ModelPricing.CacheCreationPricePerToken` | — | `per_1k_tokens_cache_creation`（新增） |
| `ModelPricing.CacheCreation5mPrice` / `CacheCreation1hPrice` | — | `per_1k_tokens_cache_creation_5m` / `_1h`（新增） |
| `ModelPricing.ImageOutputPricePerToken` | — | `per_1k_tokens_image_output`（新增） |
| `ModelPricing.LongContextInputMultiplier` | — | `long_context_input_multiplier` + `long_context_threshold` |
| `ModelPricing.InputPricePerTokenPriority` | — | `per_1k_tokens_input_priority`（新增） |
| `UsageTokens` 结构体 | 仅 `tokens_in / tokens_out` | `UsageTokens` 含 6 子项 |
| `CostBreakdown` 结构体 | 单值 `cost_micro` | `CostBreakdown` 拆 6 子项 + total + actual |
| `RateMultiplier` | 无 | `users.billing_rate_multiplier`（合并 admin > account > user） |
| `BillingCacheService.GetUserBalance` + singleflight | `billing_core.get_wallet`（直查 DB） | `BillingCacheService.get_balance` + asyncio singleflight |
| `cacheWriteWorkers` pool | 无 | `asyncio.Queue` + 10 worker tasks |
| `APIKeyRateLimitCacheData (5h/1d/7d)` | 无 | `lumen:billing:rl:{api_key_id}` Redis hash |
| `UsageBillingCommand.RequestFingerprint` | 无 | `wallet_transactions.meta.request_fingerprint` |
| `Idempotency-Key` 头 (gateway 入口) | 无 | API gateway middleware (复用 §3.4 的实现) |
| `initFallbackPricing` 内置表 | 无 | `pricing_fallback.py` ~30 模型 |
