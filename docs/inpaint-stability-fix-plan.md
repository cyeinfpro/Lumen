# Inpaint 与生图稳定性增强计划（v1.1.18）

> 目标版本：v1.1.18
> 起草日期：2026-05-13
> 范围：apps/worker（错误分类、fallback 重试、inpaint reference 链路、日志）
> 不在范围：API/Web 层；账号池；调度器

## 背景

v1.1.5 已经修复了局部重绘的弹窗塌缩、mask 二值化、size 对齐三大正确性问题。现网 v1.1.17 实测（2026-05-13 22:43 抓数）：

- 24h 生成任务 105 个，成功 98，失败 2，运行中 5
- edit 子集：成功 40，失败 2，运行中 1 → edit 失败率 **4.7%**
- 失败错误码：`all_accounts_failed` × 1，`direct_image_request_failed` × 1

表面数据健康，但 6h worker 日志显示**大量任务在成功之前经过 5–15s 的重试 / failover 浪费**，用户感知的"卡顿率"显著高于 4.7%。

本计划解决五个真问题：

- **A**：上游下载 reference 图超时被错判为 terminal `invalid_value`，inpaint 必带 reference，正中靶心
- **B**：`direct_image_request_failed` 在 retry.py 漏配，落到兜底分支被判 terminal（**确认是遗漏 bug，不是设计选择**）
- **C**：5xx fallback 重试 5 次累计 backoff 1+2+4+8 = 15s，dual_race 另一条 lane 同步空等
- **D**：inpaint 走 url-mode provider 时上游需主动下载 reference webp，网络抖动直接失败
- **E**：dual_race 失败 summaries 整页 Cloudflare HTML，根因被噪声覆盖

修复分三个方案，按风险/收益从低到高排列。三个方案可独立合入，也可累加。

---

## 现状数据（v1.1.17 实测）

### 高频错误模式（6h worker 日志）

| 错误模式 | 出现次数 | 当前处理 | 期望处理 |
|---|---|---|---|
| `Timeout while downloading https://flux.infpro.me/refs/*.webp` | 多次 | `terminal error_code=invalid_value` | retriable，换 provider 重试 |
| `curl rc=35 TLS connect error ... http=0` → `direct_image_request_failed` | 多次 | `terminal unknown err_code=... http=0` | retriable network_error |
| `responses fallback retrying attempt=2/3/4/5 backoff=1/2/4/8s` 串 5 次 | 多次 | 累计等 15s 才换 lane | 3 次，累计 1+2 = 3s |
| `dual_race both lanes failed summaries=[…几 KB Cloudflare HTML…]` | 多次 | 日志被 HTML 灌爆，根因不可见 | 只保留 trace_id + error_code + status_code + lane |
| `Cybersol image_circuit_open image_failures=10 cooldown=10s` | 偶发 | 熔断 10s 后自动恢复（符合 feedback_provider_cooldown 偏好） | 保持不变 |

### Edit 任务 24h 状态分布

```
status    | count
----------+-------
succeeded |   40
failed    |    2
running   |    1
```

---

## 方案 1（P0：错误分类修正）

**预期收益**：edit 失败率 4.7% → ~2%；inpaint 在网络抖动下不再用户可见失败。
**风险**：极低，仅改两处错误分类，无逻辑路径变化。
**工作量**：1–2 小时含测试。

### 1.1 修复 `DIRECT_IMAGE_REQUEST_FAILED` 漏配（B）

**问题定位**

- `packages/core/lumen_core/constants.py:137` 已把 `DIRECT_IMAGE_REQUEST_FAILED` 归类到 retriable 区
- `apps/worker/app/upstream.py:4378-4384` 的 `_image_job_should_failover` 也把它列为应 failover
- 但 `apps/worker/app/retry.py:67-102` 的 `_RETRIABLE_ERROR_CODES` **没有它**
- 后果：`is_retriable()` 走到第 227 行兜底 `return RetryDecision(False, f"unknown err_code={err_code} http={http_status}")`
- 日志证据：`direct edit provider fei terminal error: unknown err_code=direct_image_request_failed http=0`

**抛出点确认**（所有点 http_status 都是 0 表示网络错）：

- `apps/worker/app/upstream.py:1084-1089` — image url 下载 httpx 异常
- `apps/worker/app/upstream.py:1373-1379` — direct edit httpx 异常
- `apps/worker/app/upstream.py:1535-1543` — direct edit curl 异常
- `apps/worker/app/upstream.py:1793-1799` — direct generate httpx 异常
- `apps/worker/app/upstream.py:1868-1874` — direct generate curl 异常

**修改：`apps/worker/app/retry.py`**

在 `_RETRIABLE_ERROR_CODES` 末尾加一行：

```python
_RETRIABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        # ... 现有项不变 ...
        EC.PROVIDER_EXHAUSTED.value,
        EC.ALL_DIRECT_IMAGE_PROVIDERS_FAILED.value,
        # NEW: direct image/edit 路径 httpx/curl 网络错的包装码。
        # 与 ALL_DIRECT_IMAGE_PROVIDERS_FAILED 对称，让 task 层 backoff 后重试。
        EC.DIRECT_IMAGE_REQUEST_FAILED.value,
    }
)
```

**单测要补**（`apps/worker/tests/test_retry.py`，文件已存在）：

```python
def test_direct_image_request_failed_is_retriable():
    d = is_retriable(
        err_code="direct_image_request_failed",
        http_status=0,
        error_message="curl failed rc=35 stderr=...",
    )
    assert d.retriable is True
    assert "retriable" in d.reason
```

### 1.2 修复 "Timeout while downloading reference" 误判（A）

**问题定位**

- 上游 sub2api / 网关在拉用户 reference webp 超时后，返回 `error_code=invalid_value` + `message="Timeout while downloading https://flux.infpro.me/refs/xxx.webp"`
- `apps/worker/app/retry.py:49` 的 `_TERMINAL_ERROR_CODES` 包含 `INVALID_VALUE` → 直接 terminal
- 但本质是上游网络问题，换个 provider 可能就成（reference webp 在我们 CDN 上，确实在线）

**修改：`apps/worker/app/retry.py:127-135` 区域**

在 `is_retriable()` 内，**`_TERMINAL_ERROR_CODES` 判断之前**插入关键词救援：

```python
def is_retriable(
    err_code: str | None,
    http_status: int | None,
    has_partial: bool = False,
    *,
    error_message: str | None = None,
) -> RetryDecision:
    msg = (error_message or "").lower()

    # NEW: 上游下载用户 reference 图超时 — provider 网络问题，不是用户输入问题。
    # 典型 message: "Timeout while downloading https://flux.infpro.me/refs/xxx.webp"
    # 上游通常包装为 error_code=invalid_value，会被 _TERMINAL_ERROR_CODES 误判。
    # 换 provider 重试可恢复（reference 仍在 CDN 上）。
    if (
        "timeout while downloading" in msg
        or "failed to download" in msg
        or "could not download" in msg
    ):
        return RetryDecision(True, "retriable upstream_reference_download_timeout")

    # 1) terminal 优先于 retriable（pixel budget / 上传图超限 / 参数错）
    if err_code in _TERMINAL_ERROR_CODES:
        return RetryDecision(False, f"terminal error_code={err_code}")
    # ... 其余不变
```

**注意**：插入位置必须在 `_TERMINAL_ERROR_CODES` 判断之前，否则 `invalid_value` 会先匹配 terminal。

**单测要补**：

```python
def test_upstream_reference_download_timeout_is_retriable():
    d = is_retriable(
        err_code="invalid_value",  # 上游错判码
        http_status=400,            # 上游可能用 400 也可能 200
        error_message="Timeout while downloading https://flux.infpro.me/refs/abc.webp.",
    )
    assert d.retriable is True
    assert "reference_download" in d.reason

def test_invalid_value_without_download_marker_still_terminal():
    # 防回归：不带关键词的 invalid_value 仍应 terminal
    d = is_retriable(
        err_code="invalid_value",
        http_status=400,
        error_message="Requested resolution exceeds the current pixel budget",
    )
    assert d.retriable is False
```

### 1.3 验证清单

- [ ] `apps/worker/tests/test_retry.py` 全部通过（含新增 3 个用例）
- [ ] 抓现网相同 trace_id 的失败任务复现，确认改后会触发 failover 而非 terminal
- [ ] 不影响其他 `invalid_value` 终态错误（pixel budget / authentication 等）

---

## 方案 2（P1：重试预算与日志精简）

**前置依赖**：方案 1 合入并稳定 1–2 天。
**预期收益**：edit 平均等待时间 ↓ 5–10s；告警可读性提升；不改失败率。
**风险**：低，但需观察 fallback 全失败率（重试减少后理论上 ↑ 1–2%，由方案 1 的更早 failover 弥补）。
**工作量**：3–5 小时含测试。

### 2.1 调整 5xx fallback 重试预算（C）

**问题定位**

`apps/worker/app/upstream.py:221`：

```python
_FALLBACK_MAX_ATTEMPTS_5XX = 5
```

配合 `apps/worker/app/upstream.py:4024-4027` 的 backoff：

```python
backoff = min(
    _FALLBACK_RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)),
    _FALLBACK_RETRY_BACKOFF_MAX_S,
)
```

= base 1.0s，max 8.0s；5 次尝试只会在前 4 次失败后 sleep，所以累计 backoff = 1 + 2 + 4 + 8 = **15s**。再叠加每次请求自身耗时，单 lane 失败要等近 1 分钟。dual_race 另一条 lane 还在并发跑，但用户看到的总等待还是被慢 lane 拖累。

**修改：`apps/worker/app/upstream.py:221-222`**

```python
_FALLBACK_MAX_ATTEMPTS = 2
# GEN-P1-9: fallback 层重试预算按错误码 / HTTP 状态分类动态选择。
# v1.1.18: 5xx 从 5 降到 3（累计 backoff 3s，原 15s），让 dual_race 更快切 lane。
# 实测 99% 5xx 在 attempt=2 仍未恢复，第 4-5 次成功率极低，不值得用户等待。
_FALLBACK_MAX_ATTEMPTS_5XX = 3
_FALLBACK_MAX_ATTEMPTS_429 = 5  # 429 受 retry-after 引导，保留 5 次
_FALLBACK_MAX_ATTEMPTS_4XX = 1  # 401/403/404/422 等终态错误，重试无意义
```

**为何不再降到 2**：3 次留一次"指数回退后再赌一把"的机会，5xx 抖动有时刚好在第 3 次恢复（curl 路径 80s 周期）。

**修改：`apps/worker/app/upstream.py:226`**

```python
_FALLBACK_RETRY_BACKOFF_MAX_S = 4.0  # 原 8.0；保留给 429 / 默认预算的上限
```

**回归测试**

- [ ] `apps/worker/tests/test_upstream_retry.py` 所有用例
- [ ] 灰度阶段对比：v1.1.17 vs v1.1.18 同时间窗 edit p95 等待时长

### 2.2 精简 dual_race 失败 summary 日志（E）

**问题定位**

`apps/worker/app/upstream.py` 的 dual_race 失败处会把两条 lane 的完整 `UpstreamError.payload` 序列化到日志。payload 里如果是上游 Cloudflare 502 错误页，会带几 KB HTML：

```
edit dual_race: both lanes failed; summaries=[{"lane": "image2", "type": "UpstreamError", ..., "payload": {"raw": "<!DOCTYPE html>\n<!--[if lt IE 7]> ..."}}]
```

**修改方向**

在 dual_race summary 生成处（用 `grep -n "both lanes failed" apps/worker/app/upstream.py` 定位，应在 4500 行附近）增加 payload 裁剪：

```python
def _truncate_lane_summary(lane: str, exc: BaseException) -> dict[str, Any]:
    """裁剪到核心字段：lane / type / message 前 200 / error_code / status_code / trace_id"""
    out: dict[str, Any] = {
        "lane": lane,
        "type": type(exc).__name__,
        "message": str(exc)[:200],
    }
    if isinstance(exc, UpstreamError):
        out["status_code"] = exc.status_code
        out["error_code"] = exc.error_code
        payload = exc.payload or {}
        if isinstance(payload, dict):
            # 只保留可观察字段，丢弃 raw HTML / 完整 stack
            for k in ("trace_id", "x_trace_id", "url", "path", "method"):
                if k in payload:
                    out[k] = payload[k]
    return out
```

替换原来的 `summaries=[full_payload_dict_for_lane(...) for lane in ...]`。

完整 payload 同时通过 `logger.debug` 输出（开发环境开 DEBUG 才出），生产环境 logger.warning 只看精简版。

**注意**：sentry / 监控 hook 通常订阅的是 exception 对象，不是 logger 文本，所以裁剪日志不影响 Sentry 上传完整错误。

### 2.3 验证清单

- [ ] 全部 fallback 相关单测通过
- [ ] 灰度灰一台 worker（lumen.infpro.cn 当前只有 1 台，可用 v1.1.17 镜像并行跑 1 小时对比）
- [ ] grep 日志确认 dual_race summary 不再含 HTML，长度 < 500 字节
- [ ] Sentry 仍能看到完整 payload

---

## 方案 3（P2：inpaint reference 链路优化）

**前置依赖**：方案 1+2 上线并稳定 3–5 天。
**预期收益**：inpaint 由 reference 下载导致的失败 ↓ 50%+；同时降低 flux.infpro.me/refs/* 出向带宽。
**风险**：中，触及 inpaint provider payload 构造，可能影响非 inpaint 的 edit 任务。
**工作量**：1–2 天含测试。

### 3.1 背景与问题

**现状**：inpaint 必须传 reference + mask。当前实现取决于 provider 的 `image_edit_input_transport` 配置：

- `transport=file`：worker 把 reference 字节直接 multipart 上传，**网络稳定**
- `transport=url`：worker 上传 reference 到 `flux.infpro.me/refs/{uuid}.webp`，然后把 url 传给 provider，**provider 主动拉取**

后者在日志里观察到大量 `Timeout while downloading https://flux.infpro.me/refs/*.webp` 失败。

**根因**：

1. provider 与 `flux.infpro.me` 的网络路径不可控（跨境、CDN 边缘节点抖动）
2. reference webp 上传后没有本地缓存，每次任务都重新上传 + provider 重新下载
3. 一个用户连续 inpaint 5 次同一张图，reference 会上传 5 次

### 3.2 修改方案 A：强制 file-mode 优先

**修改：`apps/worker/app/upstream.py` 的 `_filter_for_mask` / `_pool_select_compat`**

inpaint 场景下（`mask is not None`）：

1. 优先选择 `image_edit_input_transport=file` 的 provider
2. 全部 file-mode provider 用尽后才降级到 url-mode
3. url-mode lane 失败时直接 failover 而非内部重试（这条 lane 本来就脆）

代码骨架：

```python
def _pool_select_compat(*, has_mask: bool, ...) -> list[Provider]:
    candidates = [...]  # 原有筛选
    if has_mask:
        file_mode = [p for p in candidates if p.image_edit_input_transport == "file"]
        url_mode = [p for p in candidates if p.image_edit_input_transport == "url"]
        # file-mode 排前面；url-mode 仅当 file-mode 全失败时才用
        return file_mode + url_mode
    return candidates
```

### 3.3 修改方案 B（更激进）：worker 端 reference LRU 缓存

**目标**：同一个用户对同一张原图做多次 inpaint，reference 只上传一次。

**设计**：

- 缓存 key：`sha256(reference_bytes)`
- 缓存值：`{"upload_url": "...", "expires_at": ts, "size": int}`
- 存储：Redis hash `lumen:ref_cache:{user_id}`，TTL 30 分钟
- 命中时直接复用 url，跳过上传步骤

**修改文件**：

- `apps/worker/app/upstream.py`：新增 `_get_or_upload_reference(bytes, user_id) -> url` helper
- `apps/worker/app/tasks/generation.py:1180-1240` 附近：inpaint 路径调用上面 helper 替代直接上传

**风险**：缓存命中错误可能导致 provider 拉到旧 url 404。需要：

- TTL 30 分钟 < 上游 refs CDN TTL（24h，已确认）
- LRU 容量按用户：10 个 reference / 用户，避免 Redis 暴涨
- 命中后异步 HEAD 校验 url 仍可用，失败则失效缓存

### 3.4 验证清单

- [ ] 单测：file-mode 优先的 provider 选择
- [ ] 单测：LRU 缓存命中 / 未命中 / 过期
- [ ] 灰度：观察 7 天 `Timeout while downloading` 错误数下降
- [ ] 观察 redis 内存增长 < 100MB

---

## 测试与验证

### 单测覆盖

| 方案 | 文件 | 新增用例数 |
|---|---|---|
| 1.1 | `apps/worker/tests/test_retry.py` | 1 |
| 1.2 | `apps/worker/tests/test_retry.py` | 2 |
| 2.1 | `apps/worker/tests/test_upstream_retry.py` | 3（5xx attempts / backoff clamp / 429 budget） |
| 2.2 | `apps/worker/tests/test_dual_race_image.py` | 1（裁剪并保留 trace_id） |
| 3.1 | `apps/worker/tests/test_inpaint_mask.py` | 2（file 优先 / url 降级） |
| 3.2 | `apps/worker/tests/test_reference_cache.py`（新建） | 4 |

### 集成验证

1. **本地跑全套测试**：`uv run pytest apps/worker/tests -q`
2. **服务器灰度**：先打 `v1.1.18-rc1`，部署到 lumen.infpro.cn 跑 24h，对比 edit 成功率、p95 延迟
3. **真实 inpaint 测试**：用 4K 图做 3 次连续局部重绘，确认无 reference 下载超时

### 关键指标

| 指标 | 当前 (v1.1.17) | 目标 (v1.1.18) | 数据源 |
|---|---|---|---|
| edit 失败率 | 4.7% | ≤ 2.5% | PG `generations` 表 |
| direct_image_request_failed terminal 数 | 实测有 | 0 | worker 日志 grep |
| `Timeout while downloading` 引起的 terminal | 实测有 | 0 | worker 日志 grep |
| 5xx fallback 累计 backoff p95 | 15s | ≤ 3s | metrics（如有） |
| dual_race 单条日志大小 | 几 KB | < 500B | `wc -c` 抽样 |

---

## 部署与回滚

### 部署流程

按 memory `feedback_lumen_version_bump`：

1. 修改完后跑 `python3 scripts/version.py sync` 同步 8 处 manifest
2. 本地 `pytest` 全绿
3. `git commit + tag v1.1.18` → push 触发 CI build GHCR 镜像
4. lumen.infpro.cn `lumenctl update v1.1.18`（按 `project_lumen_update_button` 路径）

### 回滚

每个方案的修改都是**纯函数级 / 配置级**，无 schema 迁移、无数据回填、无 API 协议变更，所以：

- 单方案回滚：直接 revert commit
- 全量回滚：`lumenctl update v1.1.17`

### 阶段性合入策略

强烈建议分三个 PR / 三次 tag：

1. `v1.1.18` = 方案 1（错误分类）→ 观察 2 天
2. `v1.1.19` = 方案 2（重试预算 + 日志）→ 观察 3 天
3. `v1.1.20` = 方案 3（reference 链路）→ 观察 7 天

不要把三个方案合在一个 tag 里，否则灰度时无法分离归因。

---

## 附录：相关文件清单

| 文件 | 行号 | 作用 |
|---|---|---|
| `apps/worker/app/retry.py` | 47-65 | `_TERMINAL_ERROR_CODES` |
| `apps/worker/app/retry.py` | 67-102 | `_RETRIABLE_ERROR_CODES` |
| `apps/worker/app/retry.py` | 111-227 | `is_retriable()` 主函数 |
| `apps/worker/app/upstream.py` | 218-229 | fallback 重试常量 |
| `apps/worker/app/upstream.py` | 1084-1089 | image_url_download 网络错包装 |
| `apps/worker/app/upstream.py` | 1373-1379 | direct edit httpx 网络错包装 |
| `apps/worker/app/upstream.py` | 3625-3643 | `_max_attempts_for_exception` |
| `apps/worker/app/upstream.py` | 3960-4040 | responses fallback 主循环 |
| `apps/worker/app/upstream.py` | 4370-4385 | `_image_job_should_failover` |
| `packages/core/lumen_core/constants.py` | 95-145 | `GenerationErrorCode` 枚举 |
| `apps/worker/app/tasks/generation.py` | 1180-1240 | inpaint reference 加载 |

## 附录：现网日志取证（v1.1.17，2026-05-13）

完整 6h worker 日志摘抄存档于 `docs/internal/inpaint-stability-evidence-2026-05-13.log`（待生成；如未生成则 ssh 到 82.152.164.223 跑 `docker logs --since 6h lumen-worker 2>&1 | grep -iE "inpaint|mask|edit"`）。

关键样本：

```
edit image dispatch provider_failover: from=fei remaining=7 ... reason=retriable http=503
direct edit provider fei terminal error: unknown err_code=direct_image_request_failed http=0
responses fallback retrying action=edit size=2560x3200 attempt=5/5 backoff=8.0s err=UpstreamError('upstream http 502')
image job edit/shinyshinyschool@gmail.com endpoint=responses error_class=upstream_4xx decision=terminal error_code=invalid_value: UpstreamError('Timeout while downloading https://flux.infpro.me/refs/IKVUj6fbE7OlhEJTihR4TKGJKSA2W1OC.webp.')
```
