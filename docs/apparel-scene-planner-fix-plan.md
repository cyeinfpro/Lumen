# Apparel Scene Planner 改动审查 — 修复方案

适用范围：当前未提交的服饰 showcase GPT-5.5 preflight 相关改动

涉及文件：
- `apps/api/app/routes/workflows.py`
- `apps/api/app/routes/_apparel_scene_planner.py`（新文件）
- `apps/api/tests/test_workflows_route.py`
- `apps/web/src/components/ui/projects/stages/{ModelCandidatesStage,QualityReviewStage,ShowcaseGenerationStage}.tsx`
- `apps/web/src/components/ui/projects/types.ts`
- `apps/web/src/lib/apiClient.ts`
- `packages/core/lumen_core/schemas.py`
- `packages/core/tests/test_schemas.py`

下文按"严重度 → 中等 → 小问题"排列，每条都给出：触发条件、影响、根因、最小化修复 diff、回归测试建议。

---

## 一、严重 bug（必须修）

### Bug 1. `_should_try_next_attempt` 是死代码，遇到 401/403/404 仍会跑满 3 个 attempt

**文件**：`apps/api/app/routes/_apparel_scene_planner.py:738-866`

**触发条件**：任意 provider 出现不可恢复 4xx（鉴权失败、模型不存在、参数非法）。

**影响**：
- 单个 provider 上会连试 `gpt55-priority → gpt55-standard → gpt54-standard-fallback` 三次，每次都 ~45s read timeout。等于把一次失败放大成最长 ~135s 的阻塞，再叠加 N 张图、N 个 provider，等同把整条 preflight 拖死。
- `last_error` 只保留最后一次错误，401 之类被后续错误覆盖，排障时看不到根因。

**根因**：
```python
for provider in providers:
    for attempt in attempts:
        try:
            ...
        except Exception as exc:
            last_error = f"{provider.name}/{attempt['name']}: {exc}"
            logger.info("gpt55 json attempt failed: %s", last_error)
            if not _should_try_next_attempt(exc):
                continue          # ← 落到这里和不写 if 没区别
```

1. `continue` 已经在 except 块的末尾，写不写流程都是"进入下一轮 for"，完全无效。
2. `_should_try_next_attempt` 对 `_UpstreamHTTPError` 返回 `status_code in _RETRYABLE_STATUS or status_code >= 400`。`_UpstreamHTTPError` 只在 `resp.status_code >= 400` 时抛出（line 844），所以这个函数对所有 HTTP 错误恒返回 True。

**修复方案**：

1) `_should_try_next_attempt` 改成只对真正可重试状态返回 True：

```python
def _should_try_next_attempt(exc: Exception) -> bool:
    """是否继续尝试同 provider 下一个 attempt（更弱 reasoning / fallback 模型）。"""
    if isinstance(exc, _UpstreamHTTPError):
        if exc.status_code in _RETRYABLE_STATUS:
            return True
        # 4xx 非重试码（401/403/404/422 等）→ 同 provider 切到 fallback 模型也无意义
        if 400 <= exc.status_code < 500:
            return False
        return True
    return True
```

2) 把循环改成"按 attempt 失败 → 看是否换 attempt；不行 → break 到下一个 provider"：

```python
last_error = "unknown"
for provider in providers:
    provider_fatal = False
    for attempt in attempts:
        try:
            text = await _call_responses_text(
                provider=provider,
                attempt=attempt,
                purpose=purpose,
                instructions=instructions,
                payload=payload,
                max_output_tokens=max_output_tokens,
            )
            data = _extract_json_object(text)
            if isinstance(data, dict):
                return data
            raise ValueError("json root is not object")
        except Exception as exc:  # noqa: BLE001
            last_error = f"{provider.name}/{attempt['name']}: {exc}"
            logger.info("gpt55 json attempt failed: %s", last_error)
            if not _should_try_next_attempt(exc):
                provider_fatal = True
                break
    if provider_fatal:
        continue
raise RuntimeError(last_error)
```

3) 在 `_UpstreamHTTPError` 抛出处保留 response body 摘要，方便后续看 last_error：当前 `detail = resp.text[:500]` 已经够用，不动。

**测试建议**（新增）：

```python
# test_apparel_scene_planner.py
async def test_call_gpt55_json_skips_attempts_on_401(monkeypatch):
    calls = []

    async def fake(*args, **kwargs):
        calls.append(kwargs["attempt"]["name"])
        raise scene_planner._UpstreamHTTPError(401, "unauthorized")

    monkeypatch.setattr(scene_planner, "_call_responses_text", fake)
    with pytest.raises(RuntimeError):
        await scene_planner._call_gpt55_json(
            SimpleNamespace(),
            purpose="t",
            instructions="i",
            payload={},
            max_output_tokens=200,
            provider_order=[fake_provider("p1"), fake_provider("p2")],
        )
    # 每个 provider 只应该尝试 1 次，而不是 3 次
    assert calls == ["gpt55-priority", "gpt55-priority"]
```

---

### Bug 2. safe_prompt 兜底后 `review.must_rewrite` 仍为 True，下游和监控会误判

**文件**：`apps/api/app/routes/workflows.py:3263-3293`

**触发条件**：first review 标 `must_rewrite=True` → 重写 → second review 仍 `must_rewrite=True` → 走 safe_prompt 分支。

**影响**：
- `prompt_reviews` 数组里会出现 `must_rewrite=true` 但 `final_prompt` 其实是已退到 brief 形式的安全 prompt，前端 / DB 检索 "高风险图" 会把它当作没处理掉。
- 后续任何基于 `must_rewrite` 字段做的告警 / dashboard 都会有假阳性。

**根因**：
```python
else:
    safe_prompt = _showcase_prompt(...)
    composition = _fallback_prompt_composition(...)
    review = {
        **rewritten_review,            # ← must_rewrite 还是 True
        "fallback_reason": "rewrite_still_risky; using scene-free safe prompt",
    }
    final_prompt = safe_prompt
```

**修复方案**：显式标记 safe_prompt 已完成兜底，`must_rewrite` 置 False，并补一个 `safe_fallback=True` 字段方便审计区分。

```python
review = {
    **rewritten_review,
    "must_rewrite": False,
    "safe_fallback": True,
    "fallback_reason": (
        "rewrite_still_risky; using scene-free safe prompt"
    ),
}
final_prompt = safe_prompt
```

如果 `prompt_reviews` 的消费方依赖 risk_level，再补一行：

```python
review["risk_level"] = "low"  # 已退到无场景安全 prompt
```

**测试建议**：在已有 `test_prepare_showcase_preflight_rewrites_when_review_has_no_instruction` 旁加一个新用例 `test_prepare_showcase_preflight_falls_back_to_safe_prompt_when_rewrite_still_risky`，让 `fake_review` 始终返回 `must_rewrite=True`，断言：

```python
assert preflight["prompt_reviews"][0]["must_rewrite"] is False
assert preflight["prompt_reviews"][0].get("safe_fallback") is True
assert "GPT-5.5 单张执行 Prompt" not in preflight["final_prompts"][0]
```

---

### Bug 3. safe_prompt 分支虽然传了 `garment_lock` 但被静默忽略

**文件**：`apps/api/app/routes/workflows.py:2990-3019, 3268-3293`

**触发条件**：safe_prompt 兜底路径。

**影响**：本来 safe_prompt 是"最后一道防止改商品"的护栏，结果它走的是普通 `_showcase_prompt_brief`，没有"【最高优先级：商品 1:1 还原】…" 前缀，约束力反而比正常 GPT-composed prompt 更弱。

**根因**：`_showcase_prompt` 里 `garment_lock` 只在 `if composed_prompt and composed_prompt.strip():` 分支用到。safe_prompt 分支不传 `composed_prompt`，所以 `garment_lock` 被丢。

**修复方案**：把"garment_lock prefix"提取出来，无论走哪条路径，只要传了 `garment_lock`，都拼到最终 prompt 的最前面。

```python
def _showcase_prompt(
    *,
    ...
    scene_card: dict[str, Any] | None = None,
    garment_lock: dict[str, Any] | None = None,
    composed_prompt: str | None = None,
) -> str:
    ...
    lock_prefix = _showcase_garment_lock_prefix(
        garment_lock=garment_lock,
        product_preserve=product_preserve,
        model_consistency=model_consistency,
    ) if garment_lock else ""

    if composed_prompt and composed_prompt.strip():
        head = (lock_prefix + "\n\n【GPT-5.5 单张执行 Prompt】\n") if lock_prefix \
               else "【GPT-5.5 单张执行 Prompt】\n"
        return head + composed_prompt.strip()[: max(0, MAX_PROMPT_CHARS - len(head))]

    template_direction = _template_requirement(template, product_analysis, scene_environment)
    scene_direction = _showcase_scene_card_direction(scene_card)
    if scene_direction:
        template_direction = f"{template_direction}；{scene_direction}"
    body = _showcase_prompt_brief(
        ...
        scene_card_mode=bool(scene_direction),
    )
    if not lock_prefix:
        return body
    head = lock_prefix + "\n\n"
    return head + body[: max(0, MAX_PROMPT_CHARS - len(head))]
```

> 注意：原有不带 garment_lock 的调用方（candidate prompt 等）行为保持不变，因为 `lock_prefix=""` 时走的就是老路径。

**测试建议**：扩展 `test_showcase_prompt_includes_scene_card_direction_and_garment_lock`，加一条只传 `garment_lock` 不传 `composed_prompt` 的断言：

```python
safe = workflows._showcase_prompt(
    product_analysis={"must_preserve": ["蓝色格纹"]},
    selected_candidate=candidate,
    accessory_plan={"enabled": False, "items": [], "strength": "subtle"},
    template="urban_commute",
    shot_type="front_full_body",
    final_quality="high",
    garment_lock={
        "core_identity": "蓝色格纹衬衫",
        "must_preserve": ["蓝色格纹"],
        "visibility_priority": ["正面胸口"],
        "mutation_bans": ["改颜色"],
        "occlusion_policy": "不要遮挡胸前。",
    },
)
assert "【最高优先级：商品 1:1 还原】" in safe
assert "蓝色格纹衬衫" in safe
```

---

### Bug 4. preflight 在 HTTP 路由内同步发起几十次 GPT-5.5，必然触发网关超时

**文件**：`apps/api/app/routes/workflows.py:3070-3325, 5915-5935`

**触发条件**：`output_count >= 8`，特别是 16，且未走 `rules_fallback`。

**影响**：
- 单次 HTTP 请求里串：`1 director + N × (compose + review)（最坏 + rewrite_compose + rewrite_review）`。`N=16` 时 ≈ **17–65 次** GPT-5.5 reasoning 调用。
- semaphore=4 + 每次 read timeout 45s，最坏总耗时 = `ceil(65/4) * 45 ≈ 12 分钟`。
- 反向代理 / ASGI 默认 60–300s 超时，前端会先 timeout，但服务器还在跑，结果是：
  - 前端报 504，用户重试 → 又起一次 preflight → 资源翻倍。
  - workflow run 已经创建，但 image task 没 enqueue，状态卡在 running。
- 即使不超时，对照原始未改前的实现（直接进入生图），用户感知"点了'生成'之后干等几分钟没反应"。

**根因**：preflight 是 GPT 重活，被放在了用户点击触发的同步 endpoint 内（`create_showcase_images`）。

**修复方案**：分两步推进，按工作量从小到大排列，至少做到第 1 步。

**Step 1（必须）：fan-out 收紧 + per-attempt 超时下调**

```python
gpt_semaphore = asyncio.Semaphore(min(4, max(1, len(shot_picks))))
```

- 减少每次调用的 read timeout 到 25s（GPT-5.5 reasoning medium 一般 8–15s 完成）：
  - `_apparel_scene_planner.py:833`
    ```python
    timeout=httpx.Timeout(connect=8.0, read=25.0, write=25.0, pool=8.0),
    ```
- 把 director 用 medium reasoning、composer/review 强制用 low reasoning（已是 attempt 顺序，但 review 调用本身可以直接用 low）。
- 在 `_prepare_showcase_preflight` 顶层用 `asyncio.wait_for` 包一个硬上限（如 90s），超时则降级走 `_rules_fallback_scene_planning + base prompt`：

```python
try:
    return await asyncio.wait_for(
        _prepare_showcase_preflight_impl(...),
        timeout=90.0,
    )
except asyncio.TimeoutError:
    logger.warning("apparel preflight timed out; falling back to rules planning")
    planning = _rules_fallback_scene_planning(...)
    return _rules_fallback_preflight_payload(planning, shot_picks, ...)
```

**Step 2（推荐）：把 preflight 落到后台 task**

把 preflight 拆成一个 `WorkflowStep`（例如 `apparel_scene_preflight`），endpoint 只创建 step、立刻返回 202。Worker 异步执行 preflight，然后再 enqueue 真正的图任务。前端原本就有 step 轮询，UI 只需要新增一个 "正在规划场景" 占位态。

- 复用现有 step 模型（同 `product_analysis_step` 一样建一个 step row，状态 pending → running → succeeded）。
- 在 worker 内完成 preflight 后，再调用现在 endpoint 里的 `_create_workflow_task` 循环。
- endpoint 立刻返回 `{ "showcase_step": <step_id>, "status": "queued" }`。

**Step 3（可选优化）：planner 结果缓存**

`director_payload` 的指纹（`product_analysis.must_preserve + template + shot_plan + variety + strategy`）相同时复用前一次结果。reruns 时省一次 director 调用。

**测试建议**：
- 写一个 stress / timing 测试，mock `_call_gpt55_json` 加 sleep，验证 `_prepare_showcase_preflight` 在硬超时下能走兜底。
- 在 staging 用 `output_count=16` 实跑一遍，确认 P95 < 30s。

---

## 二、中等 bug

### Bug 5. GPT planner 返回的 scene_cards 顺序未与 shot_plan 强绑定

**文件**：`apps/api/app/routes/_apparel_scene_planner.py:320-409` 与 `workflows.py:3307-3313`

**触发条件**：GPT 没按 shot_plan 顺序输出 scene_cards（很常见，特别是当 GPT 觉得某个分镜不"自然"时会重排）。

**影响**：`zip(shot_picks, scene_cards)` 按位置配对。如果 GPT 返回顺序与 shot_plan 错位，`detail_half_body`（应只拍上半身）会被配上 `full_body` 的 camera/distance，最终生图照不到衣服细节。这不会报错，只会出错图。

**修复方案**：

1) **在 director instructions 里强制按索引返回**：

```python
def _director_instructions(output_count: int) -> str:
    return (
        "你是服饰电商真人模特图的拍摄导演..."
        f"scene_cards 必须正好 {output_count} 条，"
        "且第 i 条必须严格对应 shot_plan[i]，"
        "id 用 shot_plan[i].shot_class 加 '-' 加索引（例如 detail_half_body-3）。"
        "禁止重排 shot_plan 顺序。"
        ...
    )
```

2) **在 `_normalize_scene_cards` 里做按 shot_class 重排兜底**：

```python
def _normalize_scene_cards(
    raw_cards: Any,
    fallback_cards: list[dict[str, Any]],
    shot_picks: list[tuple[str, dict[str, Any]]],   # ← 新增
) -> list[dict[str, Any]]:
    cards = raw_cards if isinstance(raw_cards, list) else []
    # 先按 product_visibility / id 尝试和 shot_picks[i].shot_class 对齐
    aligned: list[dict[str, Any] | None] = [None] * len(shot_picks)
    leftover: list[dict[str, Any]] = []
    expected = [_product_visibility_for_shot(cls) for cls, _ in shot_picks]
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        vis = clean_text(raw.get("product_visibility"), max_len=80)
        if vis in expected:
            slot = expected.index(vis)
            if aligned[slot] is None:
                aligned[slot] = raw
                expected[slot] = "__taken__"   # 占位防止重复匹配
                continue
        leftover.append(raw)
    # 用 leftover 顺序填充未匹配的位置
    for i in range(len(shot_picks)):
        if aligned[i] is None and leftover:
            aligned[i] = leftover.pop(0)
    # 再按原 normalize 逻辑生成 card，缺位用 fallback
    ...
```

调用处：

```python
cards = _normalize_scene_cards(raw.get("scene_cards"), fallback_cards, shot_picks)
```

**测试建议**：mock director 返回乱序、且故意带一个 `product_visibility=upper_body_detail` 放在第一位，断言 normalize 后第一个 card 的 `product_visibility` 与 `shot_picks[0]` 一致。

---

### Bug 6. `_showcase_prompt_brief` 在 scene_card 路径忽略 `allow_pet / allow_background_people`

**文件**：`apps/api/app/routes/workflows.py:539-547`

**触发条件**：用户在 UI 关掉 "宠物 / 路人"，但仍然走 `gpt55_batch_only` 或 rules-fallback（任何会传 scene_card 但不走 composed_prompt 的分支）。

**影响**：prompt 里仍然写"可有低存在感宠物、远处路人或生活道具作为环境辅助"，与用户开关相反。

**修复方案**：把开关传进 `_showcase_prompt_brief` 用三态文案：

```python
def _showcase_prompt_brief(
    ...
    scene_card_mode: bool = False,
    allow_pet: bool = True,
    allow_background_people: bool = True,
) -> str:
    ...
    if scene_card_mode:
        extras: list[str] = []
        if allow_pet:
            extras.append("低存在感宠物")
        if allow_background_people:
            extras.append("远处路人")
        extras.append("生活道具作为环境辅助")
        subject_rule = (
            f"9. 主角只有一位已确认模特；可有 {'、'.join(extras)}，"
            "但不得抢主体或遮挡商品。"
        )
    else:
        subject_rule = "9. 单人照。"
```

`_showcase_prompt` 把 `allow_pet / allow_background_people` 透传进来：

```python
def _showcase_prompt(
    ...
    allow_pet: bool = True,
    allow_background_people: bool = True,
) -> str:
    ...
    return _showcase_prompt_brief(
        ...
        scene_card_mode=bool(scene_direction),
        allow_pet=allow_pet,
        allow_background_people=allow_background_people,
    )
```

调用方（`_prepare_showcase_preflight` 内所有 `_showcase_prompt(...)`、`create_showcase_images` 内 fallback 的 `_showcase_prompt(...)`）都把 `body.allow_pet / body.allow_background_people` 透传。

**测试建议**：构造 `allow_pet=False, allow_background_people=False`，断言 prompt 既不包含 "宠物" 也不包含 "路人"。

---

### Bug 7. `_normalize_scene_cards` 去重时可能再次产生重复 fingerprint

**文件**：`apps/api/app/routes/_apparel_scene_planner.py:690-705`

**触发条件**：fallback_cards 内部本身有相同 fingerprint（同 shot_class 多次出现时常见），或 GPT 返回的两条 card 与 fallback_cards 中某一条都同质。

**影响**：`seen` 集合无效，scene_fingerprints 出现重复，影响 batch_context 里的 "已使用场景" 校验。

**修复方案**：

```python
def _dedupe_scene_cards(
    cards: list[dict[str, Any]],
    fallback_cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, card in enumerate(cards):
        fingerprint = scene_fingerprint(card)
        if fingerprint in seen and index < len(fallback_cards):
            replacement = dict(fallback_cards[index])
            replacement["source"] = "rules_fallback_dedupe"
            new_fp = scene_fingerprint(replacement)
            # 若 fallback 也重复，再加 index 后缀强制扰动 micro_event
            if new_fp in seen:
                replacement["micro_event"] = (
                    f"{replacement.get('micro_event') or ''}（变体 {index + 1}）"
                )[:160]
                new_fp = scene_fingerprint(replacement)
            card = replacement
            fingerprint = new_fp
        seen.add(fingerprint)
        card["fingerprint"] = fingerprint
        out.append(card)
    return out
```

**测试建议**：构造两条 GPT card fingerprint 相同，且 `fallback_cards[1]` 与 `out[0]` 相同，断言最终 out 三条 fingerprint 全不同。

---

### Bug 8. `prefix` 自身可能突破 `MAX_PROMPT_CHARS`

**文件**：`apps/api/app/routes/workflows.py:2990-2999`

**触发条件**：`garment_lock.must_preserve / mutation_bans` 列表异常长（GPT 上游脏数据，或者后续业务追加字段）。

**影响**：最终 `final_prompt` 长度可能超过 10000，下游 prompt 校验或上游 image model 直接拒绝。

**修复方案**：先对 prefix 自身硬截断，再算剩余空间。

```python
def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"

if composed_prompt and composed_prompt.strip():
    prefix = (
        _showcase_garment_lock_prefix(
            garment_lock=garment_lock,
            product_preserve=product_preserve,
            model_consistency=model_consistency,
        )
        + "\n\n【GPT-5.5 单张执行 Prompt】\n"
    )
    # 给正文留至少 600 字符的空间
    if len(prefix) > MAX_PROMPT_CHARS - 600:
        prefix = _truncate(prefix, MAX_PROMPT_CHARS - 600)
    return prefix + composed_prompt.strip()[: max(0, MAX_PROMPT_CHARS - len(prefix))]
```

同步在 `_showcase_garment_lock_prefix` 内对 `preserve / visibility / mutation_bans` 做条数和单条长度上限：

```python
preserve = "、".join(
    str(item)[:40]
    for item in (garment_lock.get("must_preserve") or [])[:8]
    if item
)
```

**测试建议**：构造 must_preserve 32 条、每条 200 字的 garment_lock，断言 `_showcase_prompt(...)` 长度 ≤ MAX_PROMPT_CHARS。

---

### Bug 9. `provider_order` 解析失败被设为 `[]`，下游不会重新解析直接全 fallback

**文件**：`apps/api/app/routes/workflows.py:3081-3088` 与 `_apparel_scene_planner.py:738-753`

**触发条件**：`_resolve_scene_provider_order` 抛出（例如 DB 抖动、settings 表暂时不可读）。

**影响**：在这次 preflight 整个生命周期内，所有 compose / review 都立刻 `RuntimeError("no responses provider available")` 触发 fallback。本来 DB 一秒后就恢复，结果整批图全部走规则兜底，质量明显劣化。

**修复方案**：失败时设为 `None`（让 `_call_gpt55_json` 每次再尝试解析），同时打 warning：

```python
provider_order: list[ProviderDefinition] | None = None
if scene_planner != "rules_fallback":
    try:
        provider_order = await _resolve_scene_provider_order(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "apparel scene provider resolution failed (will retry per call): %s",
            exc,
        )
        provider_order = None
```

如果担心每次重解析 DB 压力，可以加一个 short-TTL（5–10s）的内存缓存。

**测试建议**：mock `_resolve_scene_provider_order` 第一次抛错，第二次成功，验证最终 compose 走的是成功 provider 不是 fallback。

---

## 三、小问题（建议清扫）

### Bug 10. `currentConfigKey` 用 `initial*` 拼接，UI 改了下拉不更新

**文件**：
- `apps/web/src/components/ui/projects/stages/ModelCandidatesStage.tsx:137-141`
- `apps/web/src/components/ui/projects/stages/QualityReviewStage.tsx:87-92`
- `apps/web/src/components/ui/projects/stages/ShowcaseGenerationStage.tsx:96-101`

**触发条件**：用户切换 dropdown 后期望 `trackedConfigKey` 触发 reset。

**影响**：本次 PR 之前就有；新加字段后影响面变大。把 `currentConfigKey` 里的 `initial*` 全部换成本地 state 同样不对——它的语义本就是"snapshot 旧 step 配置 vs 新拉到的 step 配置"。建议保持现状但补一行注释，避免后续被误改。

**修复方案**（注释而非改逻辑）：

```ts
// currentConfigKey 故意只用 initial*：仅在 showcaseStep.input_json 改变时（即上游
// 新建/重新生成 step 时）才 reset 本地表单，避免用户改了 dropdown 又被重置。
const currentConfigKey = `${initialTemplate}:${initialAspectRatio}:${initialQuality}:${initialOutputCount}:${initialSceneStrategy}:${initialSceneVariety}:${initialContinuityAnchor}`;
```

### Bug 11. 三个 stage 中的 `coerceSceneStrategy / coerceSceneVariety / coerceContinuityAnchor` 重复

**文件**：上述三个 stage 末尾各有一份。

**影响**：将来扩字段必漂移。

**修复方案**：搬到 `apps/web/src/components/ui/projects/types.ts` 旁边一个 `coercers.ts`，三处统一 import。函数本体不变：

```ts
// apps/web/src/components/ui/projects/coercers.ts
import {
  CONTINUITY_ANCHOR_LABELS,
  SCENE_STRATEGY_LABELS,
  SCENE_VARIETY_LABELS,
  type CreateContinuityAnchor,
  type CreateSceneStrategy,
  type CreateSceneVariety,
} from "./types";

export function coerceSceneStrategy(value: unknown): CreateSceneStrategy {
  return SCENE_STRATEGY_LABELS.some(([option]) => option === value)
    ? (value as CreateSceneStrategy)
    : "natural_series";
}
// ... 同理 coerceSceneVariety / coerceContinuityAnchor
```

### Bug 12. 前端 `allow_pet` 永远 = `continuityAnchor === "pet"`，schema 字段冗余

**文件**：三个 stage 的 `handleSubmit` + `apiClient.ts:670-675` + `schemas.py:800-815`

**影响**：用户没法独立勾选 `allow_pet`，但 schema 把它作为独立 boolean 暴露，新加 UI 之前都是死字段。

**修复方案**（二选一）：

- 选 A（推荐，最小化）：保留字段，在前端 SelectField 旁加两个 Checkbox 让用户独立勾选 `allow_pet / allow_background_people`，与 `continuity_anchor` 解耦。
- 选 B：删字段。在 schema 删 `allow_pet / allow_background_people`，后端从 `continuity_anchor == "pet"` 自动推导 `allow_pet`，`allow_background_people` 默认 True。

A 更面向未来；B 立刻让 schema 自洽。

### Bug 13. `_showcase_scene_card_direction` 在字段缺失时输出 "None"

**文件**：`apps/api/app/routes/workflows.py:2861-2887`

**触发条件**：scene_card 某字段被 GPT 漏返（被 `_normalize_scene_cards` 兜底过基本不会，但 rules_fallback 也可能错过）。

**影响**：prompt 里出现 "场景族：None；地点：None…" 字样，污染上游模型。

**修复方案**：把每个 `f"...{x}"` 改成 helper：

```python
def _kv(label: str, value: Any) -> str:
    text = str(value or "").strip()
    return f"{label}：{text}" if text else ""

parts = [
    _kv("场景族", scene_card.get("scene_family")),
    _kv("地点", scene_card.get("location")),
    _kv("事件", scene_card.get("micro_event")),
    _kv(
        "机位",
        " / ".join(
            x for x in (
                camera.get("distance"), camera.get("angle"), camera.get("lens_feel"),
            ) if x
        ),
    ),
    _kv("动作", scene_card.get("pose")),
    _kv("动态", scene_card.get("motion")),
    _kv("道具", prop_line),
    _kv("光线", scene_card.get("lighting")),
    _kv("构图", scene_card.get("composition")),
    _kv("商品可见性", scene_card.get("product_visibility")),
    _kv("本张禁令", negative_line),
]
return "；".join(part for part in parts if part)
```

---

## 四、修复落地顺序与回归套件

### 推荐合并节奏

1. **第 1 个 PR（必须，安全修）**
   - Bug 2（must_rewrite 误标）
   - Bug 3（safe_prompt 加 garment_lock prefix）
   - Bug 6（allow_pet/路人开关贯通到 prompt）
   - Bug 13（None 字面量过滤）
   - 每条都补对应单测，全部走当前已有 `pytest -q apps/api/tests/test_workflows_route.py`。

2. **第 2 个 PR（必须，稳定性）**
   - Bug 1（_should_try_next_attempt 重写）
   - Bug 9（provider_order 失败回退为 None）
   - Bug 7（dedupe 加二次扰动）
   - 新建 `apps/api/tests/test_apparel_scene_planner.py` 跑 _call_gpt55_json / dedupe 单测。

3. **第 3 个 PR（性能 / 超时）**
   - Bug 4 Step 1（fan-out 收紧 + asyncio.wait_for(90s) 兜底）
   - 在 staging 跑 output_count={4, 8, 16} 三组观察 P95。
   - 若 P95 仍 > 30s，做 Bug 4 Step 2（后台 step）。

4. **第 4 个 PR（次要）**
   - Bug 5（scene_cards 顺序对齐）
   - Bug 8（prefix 截断）
   - Bug 10（注释）/ 11（coercers 提取）/ 12（allow_pet UI 暴露或字段删除）。

### 全量回归测试清单

后端：
```bash
uv run --project apps/api pytest -q apps/api/tests/test_workflows_route.py
uv run --project packages/core pytest -q packages/core/tests/test_schemas.py
# 新增（如果按本文建议）
uv run --project apps/api pytest -q apps/api/tests/test_apparel_scene_planner.py
```

前端：
```bash
pnpm --filter web typecheck
pnpm --filter web test
```

手工 E2E：
1. `output_count=4 / scene_planner=gpt55_preflight` 跑通；
2. `output_count=16 / scene_planner=gpt55_preflight` 不超时，10 分钟内完成；
3. 拔掉所有 provider 配置，跑 `scene_planner=gpt55_preflight`，确认自动降级到 `rules_fallback` 且生成正常；
4. `allow_pet=false` 时，生成的图 prompt 文本不含"宠物"；
5. 故意 mock 上游一直 high-risk，验证最终落到 safe_prompt 时 `prompt_reviews[i].must_rewrite=false`。

### 监控建议（非代码修复）

- 给 `workflow_scene_planner_effective` 加一个 Grafana stat panel，监控 `rules_fallback` 比例；高于 10% 就告警。
- 给 preflight 总耗时打一个 histogram metric（`apparel_preflight_seconds`），P95 > 30s 告警。
- 给 `prompt_reviews[*].safe_fallback=true` 数量打 counter，频繁触发说明 director 输出有质量问题。
