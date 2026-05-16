# 模特库「参考图生成」模式 实施方案

> 状态：实施稿
> 适用范围：模特库独立生成入口（`/apparel-model-library/generate`）
> 目标：在现有「文生图」模式之外，新增「参考图生成」模式 —— 用户上传一张参考图，系统自动识别参考图中人物的外貌特征（年龄、性别、外貌方向、气质风格），并基于这些特征生成对应的模特库 2×2 contact sheet。

---

## 1. 背景

### 1.1 现状

模特库目前只有「文生图」模式，参见：

- 前端表单：`apps/web/src/components/ui/projects/library/ModelLibraryGenerator.tsx`
- API 入口：`apps/api/app/routes/workflows.py::generate_apparel_model_library_job`（line 6282）
- Prompt 构造：`apps/api/app/routes/workflows.py::_model_library_generate_prompt`（line 5546）
- 入参 Schema：`packages/core/lumen_core/schemas.py::ApparelModelLibraryGenerateIn`（line 647）

用户必须手选「年龄段 / 性别 / 外貌方向 / 气质标签 / 其他要求」5 个维度。对于「已经有一张满意人物照片，希望复刻这个类型的模特」的场景，需要手动逐个回填字段，体验差且容易丢细节。

### 1.2 目标

新增 `mode = "reference_image"` 入口：

1. 用户上传 1 张参考图（人像，**任意朝向 / 任意表情 / 任意构图**：正面、3/4 侧、纯侧、回头、半身、特写都行）。
2. 后端用 vision 模型自动抽取参考图人物特征（age_segment / gender / appearance_direction / style_tags / 自由描述）。
3. 把抽取结果 + 用户可选的覆盖项一起进 prompt，**把参考图作为身份锚点（identity lock）传给多模态 provider**，要求生成「同一个人」的 2×2 contact sheet —— 与文生图同源输出格式，复用现有 `model_library_generate` step / 任务中心 / 自动打标签 / 入库流程。
4. 即使参考图不是正面，也要按 contact sheet 固定四视图（正面全身 / 左 90° 侧面全身 / 背面全身 / 正面大头照）和**中性表情**重建朝向与表情；模型需要从参考图自行推断未见侧面 / 背面，并保持同一人面部特征一致。

**非目标**：

- 不绑定单一上游模型，不实施 LoRA / 训练侧定制 —— 全部走现有 provider chain 的多模态参考图通道，复刻强度取决于上游能力。
- 不修改模特库浏览页、收藏入库、任务中心聚合等已有逻辑。
- 不引入新的图片存储路径或 Provider 协议。

---

## 2. 总体方案

### 2.1 模式划分

新增 `mode` 字段（默认 `text`，保持向后兼容）：

| mode | 入参组合 | 描述 |
|---|---|---|
| `text`（默认） | age_segment + gender + appearance_direction + style_tags + extra_requirements | 现有文生图模式，不变 |
| `reference_image` | reference_image_id + 可选 overrides | 新增模式，自动抽取参考图人物特征后复刻同一人到 2×2 contact sheet |

后端进入 `reference_image` 分支时，先用 vision 抽取一次，再按文生图同样的 contact sheet prompt 入队 N 个 worker task。**只是 prompt 来源不同，task 拓扑完全一致**：1 个隐藏 WorkflowRun + 1 个 step + N 个 generation task。

### 2.2 端到端时序

```text
[Web]                      [API]                            [Worker / Vision]
 |  上传参考图 ───────────▶ POST /images/upload
 |  ◀─ image_id
 |
 |  POST /apparel-model-library/generate
 |    { mode: "reference_image",
 |      reference_image_id, count, overrides? } ─▶  /generate handler
 |                                                  ├─ 同步调 vision 抽取 (复用 model_library_tagging)
 |                                                  │    ↓ AutoTagResult
 |                                                  ├─ merge overrides
 |                                                  ├─ 写 WorkflowRun + step (input_json 含抽取快照)
 |                                                  ├─ 入队 N 个 generation task
 |  ◀─ Job (status=queued, items=[])                │      task.workflow_meta.reference_image_id
 |                                                  │      task.upstream_request.attachments=[ref]
 |  GET /apparel-model-library/jobs (轮询)           ▼
 |  ◀─ Job (status=running/succeeded, items=[...])  Worker pulls → generation pipeline
                                                       intent=IMAGE_TO_IMAGE，attachment=[ref]
                                                       prompt = contact sheet + 抽取特征
                                                       结果落库 → 自动打标签（不变）
```

### 2.3 关键判断

| 决策点 | 选择 | 理由 |
|---|---|---|
| 复用 vision 抽取代码 | 是，复用 `apps/worker/app/tasks/model_library_tagging.py::AutoTagResult` 同款 prompt 协议 | 字段（age_segment / gender / appearance_direction / style_tags）一一对应，无新增维护成本 |
| 抽取在 API 同步还是 Worker 异步 | API 同步（HTTP 时延 ≤ 25s，已是 tagging 默认超时） | 抽取失败要立刻给用户报错，让他重试或切回文生图；放 worker 异步会让用户看到任务进了队列才知道失败 |
| Intent 用什么 | `Intent.IMAGE_TO_IMAGE`，attachment_ids=[reference_image_id] | 参考图必须进入上游 provider 的多模态请求，否则相当于退化成文生图 |
| 上传图片复用哪条链路 | 复用现有 `POST /images/upload`（前端 `uploadImage()`，见 `apps/web/src/lib/apiClient.ts:1382`） | 已有鉴权、配额、变体生成、缩略图，零新增基础设施 |
| 计费 | 与文生图相同：N 张正常计费；vision 抽取不单独计费（一次性小调用，与现有自动打标签的免计费策略一致） | 避免引入新的计费档位，与 `model_library_tagging` 现有策略对齐 |

---

## 3. 数据契约变更

### 3.1 `ApparelModelLibraryGenerateIn`（`packages/core/lumen_core/schemas.py`）

```python
class ApparelModelLibraryGenerateIn(BaseModel):
    # 既有字段保持不变（age_segment / gender / genders / appearance_direction /
    # extra_requirements / style_tags / count / auto_tag）

    # === 新增 ===
    mode: Literal["text", "reference_image"] = "text"
    reference_image_id: str | None = Field(default=None, max_length=64)

    # mode=text 时：age_segment 必填，reference_image_id 必须为 None。
    # mode=reference_image 时：reference_image_id 必填；age_segment / gender
    # 允许传 None，由 vision 抽取兜底；如果传了非空值，按"用户覆盖优先"策略合并。

    @model_validator(mode="after")
    def _validate_mode(self) -> "ApparelModelLibraryGenerateIn":
        if self.mode == "reference_image":
            if not self.reference_image_id:
                raise ValueError("reference_image_id is required when mode='reference_image'")
        else:
            if self.reference_image_id:
                raise ValueError("reference_image_id only allowed when mode='reference_image'")
            if self.age_segment is None:
                raise ValueError("age_segment is required when mode='text'")
        return self
```

把 `age_segment` 改成 `Optional[ModelAgeSegment]`，默认 `None`。Validator 保证 text 模式下必填，向后兼容（旧客户端都会传）。

### 3.2 `ApparelModelLibraryJobOut`（同文件）

仅新增 2 个只读字段，便于前端展示「这次任务的参考图与抽取结果」：

```python
reference_image_id: str | None = None          # 仅 mode=reference_image 时非空
reference_image_url: str | None = None         # 服务端按需返回（preview1024 变体）
extracted_profile: dict[str, Any] | None = None
# {"age_segment": "...", "gender": "...", "appearance_direction": "...",
#  "style_tags": [...], "notes": "..."}
```

### 3.3 `WorkflowRun.metadata_jsonb` / `WorkflowStep.input_json`

`run.metadata_jsonb` 增加：

```json
{
  "template": "apparel_model_library_generate",
  "mode": "reference_image",
  "reference_image_id": "img_...",
  "extracted_profile": { ... }
}
```

`step.input_json` 也镜像 `mode` 与 `extracted_profile`，让 `_model_library_run_inputs()` 在任务中心展示侧能复原原始输入。

### 3.4 单条 generation task 的 `upstream_request.workflow_meta`

```python
workflow_meta = {
    "workflow_action": MODEL_LIBRARY_GENERATE_WORKER_ACTION,
    "workflow_candidate_index": task_index,
    "workflow_model_library_mode": "reference_image",
    "workflow_model_library_reference_image_id": reference_image_id,
    # 其余既有字段保留
}
```

---

## 4. 后端实施

### 4.1 抽取流程：复用 vision tagging

新建 `apps/api/app/routes/_apparel_library_reference.py`，对外暴露一个协程：

```python
async def extract_reference_profile(
    *,
    db: AsyncSession,
    user: User,
    image_id: str,
) -> ReferenceProfile:
    """同步调 vision 抽取参考图人物特征。

    - 校验 image_id 属于当前用户（_validate_owned_images）。
    - 加载 Image 行 → 复用 worker 的 auto_tag_model_image() 协议。
      为避免在 API 进程里直接依赖 worker 模块，把 model_library_tagging.py
      中的 vision 调用部分抽到 packages/core/lumen_core/vision_tagging.py，
      worker 和 API 共用同一个 client。
    - 失败/超时返回 None 字段而非抛异常；上层根据是否有任何字段决定是否进 prompt。
    """
```

落地步骤：

1. 在 `packages/core/lumen_core/` 新建 `vision_tagging.py`，把 `model_library_tagging.py` 中的 `_TAGGING_*` 常量、provider chain 调用、JSON 解析、`AutoTagResult` 数据类抽过来。**这步是纯重构，行为不变**。
2. `model_library_tagging.py` 改为薄壳，复用 `lumen_core.vision_tagging`。原有 worker 任务签名保持不动。
3. `_apparel_library_reference.py` 调用同一份 `vision_tagging`，对一张参考图运行一次抽取。
4. API 进程超时严格控制 ≤ 30s（vision 默认 25s + 缓冲 5s），失败时回 HTTP 422 `reference_extract_failed`，前端引导用户「换图或切回文生图」。

> 注意：抽取调用要走「免计费」标记，类似 `purpose="model_library_tagging"`，避免对用户重复计费。

### 4.2 `generate_apparel_model_library_job` 路由改造

`apps/api/app/routes/workflows.py:6282`，按 mode 分支：

```python
async def generate_apparel_model_library_job(body, user, db):
    if int(body.count) not in MODEL_LIBRARY_GENERATE_COUNTS:
        raise _http("invalid_count", ..., 422)

    extracted: ReferenceProfile | None = None
    reference_image_id: str | None = None
    reference_image_url: str | None = None

    if body.mode == "reference_image":
        reference_image_id = body.reference_image_id
        await _validate_owned_images(db, user_id=user.id, image_ids=[reference_image_id])
        extracted = await extract_reference_profile(
            db=db, user=user, image_id=reference_image_id,
        )
        reference_image_url = imageVariantUrl(reference_image_id, "preview1024")

        # 合并：用户显式传入的字段优先于抽取结果
        body = _merge_reference_overrides(body, extracted)

    # 以下复用原有路径，只把 mode / reference_image_id / extracted_profile 透传
    genders = _model_library_generate_genders(body)
    title = _model_library_run_title(... , mode=body.mode)
    conv = await _get_or_create_workflow_conversation(...)
    run = WorkflowRun(
        ...,
        metadata_jsonb={
            "template": "apparel_model_library_generate",
            "mode": body.mode,
            "reference_image_id": reference_image_id,
            "extracted_profile": extracted.to_dict() if extracted else None,
            "model_profile": {...},
        },
    )
    step = WorkflowStep(
        ...,
        input_json={
            ...,
            "mode": body.mode,
            "reference_image_id": reference_image_id,
            "extracted_profile": extracted.to_dict() if extracted else None,
        },
    )
    ...
    bundles, _ = await _enqueue_model_library_generate_tasks(
        db=db, user=user, conv=conv, run=run, step=step, body=body,
        reference_image_id=reference_image_id,  # ← 新增
    )
```

`_merge_reference_overrides` 策略：

| 字段 | 合并规则 |
|---|---|
| age_segment | body 非空 → 用 body；否则用 extracted；都空 → fallback `young_adult` |
| gender / genders | body 非空 → 用 body；否则用 extracted；都空 → fallback `["female"]` |
| appearance_direction | body 非空字符串 → 用 body；否则用 extracted（如果 extracted 有） |
| style_tags | 合并去重，body 优先；上限保持 12 |
| extra_requirements | 透传 body（用户自由输入栏），抽取不覆盖 |

### 4.3 `_enqueue_model_library_generate_tasks` 改造

`apps/api/app/routes/workflows.py:6222`，增加 `reference_image_id` 入参：

```python
async def _enqueue_model_library_generate_tasks(
    *, db, user, conv, run, step, body,
    reference_image_id: str | None = None,
):
    ...
    for gender in genders:
        for idx in range(1, int(body.count) + 1):
            prompt = _model_library_generate_prompt(
                age_segment=body.age_segment,
                gender=gender,
                appearance_direction=body.appearance_direction,
                extra_requirements=body.extra_requirements,
                style_tags=body.style_tags,
                candidate_index=idx,
                reference_mode=reference_image_id is not None,  # ← 新增
            )
            bundle, _, gen_ids = await _create_workflow_task(
                db=db, user=user, conv=conv,
                intent=Intent.IMAGE_TO_IMAGE if reference_image_id else Intent.TEXT_TO_IMAGE,
                text=prompt,
                attachment_ids=[reference_image_id] if reference_image_id else [],
                idempotency_key=f"mlib:{run.id[:24]}:{gender}:{idx}",
                workflow_run_id=run.id,
                workflow_step_key=MODEL_LIBRARY_GENERATE_STEP_KEY,
                image_params=_model_library_generate_image_params(),
                workflow_meta={
                    "workflow_action": MODEL_LIBRARY_GENERATE_WORKER_ACTION,
                    "workflow_candidate_index": task_index,
                    "workflow_model_library_mode": "reference_image" if reference_image_id else "text",
                    "workflow_model_library_reference_image_id": reference_image_id or "",
                    "workflow_model_library_age_segment": body.age_segment,
                    "workflow_model_library_gender": gender,
                    "workflow_model_library_appearance_direction": body.appearance_direction or "",
                    "workflow_model_library_style_tags": _clean_style_tags(body.style_tags),
                    "workflow_model_library_auto_tag": bool(body.auto_tag),
                },
            )
            ...
```

### 4.4 `_model_library_generate_prompt` 改造

`apps/api/app/routes/workflows.py:5546`，新增 `reference_mode: bool = False` 参数。**参考图模式下要把现有的「差异化 anchor」关掉**（candidate_index 仍然透传，仅用于 idempotency_key 与日志去重），并改用「身份锚定 + 朝向重建」指令：

```python
def _model_library_generate_prompt(*, ..., reference_mode: bool = False) -> str:
    ...
    base_parts = [
        # 现有 contact sheet 描述全部保留
        ...,
    ]
    if reference_mode:
        # 1) 身份锚定：明确要求"同一人"，参考图可能是任意朝向 / 表情；
        # 2) 朝向 & 表情重建：按四视图固定姿态 + 中性表情，不复制参考图原始构图；
        # 3) 抑制多样化：把"每张候选差异化"换成"每张候选都尽量像同一个人"。
        base_parts.insert(
            1,
            "Identity lock: the attached image is a reference of the SAME PERSON "
            "that must appear in all four panels. Preserve the reference person's "
            "face (facial structure, eye shape, nose, mouth, eyebrows), skin tone, "
            "hair color, hair length and hair style as faithfully as possible. "
            "The reference image may be taken from ANY angle (front, three-quarter, "
            "profile, back, close-up, candid) with ANY expression. Do NOT copy the "
            "reference's pose, framing, background, clothing or expression — infer "
            "the unseen sides of the head and body from the reference and "
            "re-render the same person in the four required views with a neutral, "
            "relaxed expression and closed mouth. If the reference shows only the "
            "face or upper body, infer plausible body proportions consistent with "
            "the inferred age and gender.",
        )
    return " ".join(part for part in base_parts if part).strip()


def _enqueue_model_library_generate_tasks(...):
    ...
    diversity_idx = idx if reference_image_id is None else 1
    # ↑ 文生图保持差异化；参考图模式把 candidate_index 固定为 1，
    #   让 _model_diversity_anchor 输出最弱的差异化偏置（同一人的多张副本）。
    prompt = _model_library_generate_prompt(
        ...,
        candidate_index=diversity_idx,
        reference_mode=reference_image_id is not None,
    )
```

> 为什么不直接 `if reference_mode: skip diversity_anchor`：保留 anchor 调用点能让 `_model_diversity_anchor` 自然演进；这里把 index 锁成 1 是更小的侵入式改动。如果后续观察发现「同一人复刻 + 微差异化」更好（比如表情有轻微变化），可以在 `_model_diversity_anchor` 内部针对 `reference_mode` 单独切一条短分支，本期不做。

### 4.5 `_model_library_run_inputs` 与任务中心展示

`apps/api/app/routes/workflows.py:5630`，扩展返回字段：

```python
def _model_library_run_inputs(step: WorkflowStep) -> dict[str, Any]:
    raw = step.input_json if isinstance(step.input_json, dict) else {}
    return {
        # 既有字段保持
        ...,
        "mode": raw.get("mode") or "text",
        "reference_image_id": raw.get("reference_image_id"),
        "extracted_profile": raw.get("extracted_profile"),
    }
```

`_job_from_library_run`（同文件附近）填到 `ApparelModelLibraryJobOut.reference_image_id / reference_image_url / extracted_profile`，让任务中心卡片可视化「来源是参考图 + 抽取结果」。

### 4.6 `_validate_owned_images` 已有，不需新增

可直接复用，路径见 `workflows.py` 既有用法。

### 4.7 Worker 侧

`apps/worker/app/tasks/generation.py` 已经支持 `Intent.IMAGE_TO_IMAGE` + attachments，不需要改：

- `attachment_ids` 通过 `upstream_request.attachments[].image_id` 传给 provider；
- provider chain 自行选择最适合「图生图 / 多模态参考」的上游模型；
- 失败重试 / SSE 进度 / 入库 / 自动打标签全部复用现有路径。

唯一需要确认：worker 的 `_handle_model_library_generate_task`（如有）是否对 `mode` 做了硬编码假设。从 grep 结果看，worker 只读 `workflow_action == MODEL_LIBRARY_GENERATE_WORKER_ACTION` 决定走完成回调，对 attachments 是否存在不敏感，不需要改。

---

## 5. 前端实施

### 5.1 顶层模式切换

`apps/web/src/components/ui/projects/library/ModelLibraryGenerator.tsx` 在 `<header>` 下方新增 mode tab（与现有 Editorial 设计语言一致）：

```tsx
const MODE_OPTIONS: Array<["text" | "reference_image", string, string]> = [
  ["text", "文生模特", "通过描述生成"],
  ["reference_image", "参考图生模特", "上传一张人像，复刻同一人到 2×2 contact sheet（参考图朝向 / 表情不限）"],
];

const [mode, setMode] = useState<"text" | "reference_image">("text");
```

UI：放在 `N°00` 一行 underline chip group。两个 mode 的字段都展示在下面，但根据 mode 互斥隐藏：

- `mode === "text"`：现有所有字段（N°01 ~ N°04），不变。
- `mode === "reference_image"`：
  - N°01 替换为「参考图」区块（上传 / 预览 / 替换）。
  - N°02「外貌方向」「年龄段」「性别」「气质」改为**可选覆盖项**，标题加 `（可选 · 默认从参考图自动识别）`，留空走自动识别。
  - N°03「其他要求」保持。
  - N°04「张数 / 自动识别」保持。

### 5.2 参考图上传组件

新建 `ModelLibraryReferenceUploader.tsx`，复用 `uploadImage()`（`apps/web/src/lib/apiClient.ts:1382`）：

```tsx
const onPick = async (file: File) => {
  if (file.size > 10 * 1024 * 1024) {
    toast.error("参考图过大", { description: "请上传 10MB 以内的图片" });
    return;
  }
  setUploading(true);
  try {
    const uploaded = await uploadImage(file);
    onChange({ imageId: uploaded.id, previewUrl: uploaded.preview_url });
  } catch (err) {
    toast.error("上传失败", { ... });
  } finally {
    setUploading(false);
  }
};
```

UI 要求：

- 接受 `image/png, image/jpeg, image/webp`；前端先用 `<input type="file" accept=...>` 限制。
- 上传中显示 skeleton；上传完显示 4:5 比例的缩略图 + 「替换 / 清除」按钮。
- 必填校验：mode=reference_image 但未上传时，提交按钮 disabled，并在按钮下方提示 `请先上传参考图`。
- 保持与现有 `Chip` / `Field` / `Section` 视觉一致（hairline 分隔 + mono eyebrow）。

### 5.3 提交 payload

```ts
const body: ApparelModelLibraryGenerateIn = {
  mode,
  age_segment: mode === "text" ? ageSegment : (ageSegment || null),
  genders: genders.length ? genders : undefined,
  gender: genders[0] ?? "female",
  appearance_direction: appearance || null,
  extra_requirements: extra.trim() || null,
  style_tags: styleTags,
  count,
  auto_tag: autoTag,
  reference_image_id: mode === "reference_image" ? referenceImageId : null,
};
```

对应 `apps/web/src/lib/apiClient.ts` 中 `ApparelModelLibraryGenerateIn` interface 新增 `mode` / `reference_image_id` 字段。

### 5.4 任务中心展示

`ModelLibraryJobsPanel.tsx`：

- Job 卡片左上角增加角标：`文生 · 参考图`；mode 来自后端新增字段 `extracted_profile != null` 或显式 `reference_image_id`。
- 展开 Job 详情时（如果当前有该交互）显示 `参考图` 缩略图 + 抽取结果（`extracted_profile` 字段以 chip 行展示，例如 `识别：青年 · 女 · 东亚 · 温柔亲和`），帮助用户复盘。

### 5.5 设计系统合规

按 `docs/frontend-theme-dialog-standards.md` 与 `docs/DESIGN.md`：

- 上传卡片用 `--bg-1` / `--border` / `--shadow-1`，不要写死 `bg-neutral-*`。
- 文案区用 `--fg-2` / `--fg-3`，按钮用现有 `Button` primitive。
- 提交 / 校验失败的 toast 用 `toast.error` / `toast.success`，与现有一致。

---

## 6. 提示词与人物特征注入

### 6.1 抽取出来的字段如何注入 prompt

在 `_merge_reference_overrides` 完成后，进入 `_model_library_generate_prompt` 时所有字段已经 normalize 完毕。`reference_mode=True` 会在 prompt 头部插入「身份锚定 + 朝向 / 表情重建」指令（见 §4.4），其余 `appearance / style_tags / age_directive / gender_label` 仍然走原有 prompt 拼接路径作为**辅助约束**：

- 抽取的字段是辅助：即使 vision 抽错性别 / 年龄段，contact sheet 输出仍以参考图本身的视觉特征为准。
- 字段同时进 prompt 的好处：让模型在「参考图与文字描述不一致」时有一个一致性的判断框架；并且抽取结果会写到 `step.input_json`，方便后续在任务中心展示「识别为：青年 · 女 · 东亚」帮助用户复盘。

候选张数 = N 时，N 张产出都是「同一人 + 同四视图 + 同中性表情」，差异极小，目的是给用户多张候选挑选最像的一张（参见 §4.4 关于 diversity_idx 锁 1 的说明）。如果用户想要「同一人 + 多种气质 / 风格」，应该用 N=1 多次提交不同 overrides。

### 6.2 `extracted_profile` 的 `notes` 字段

vision 抽取会返回 `notes`（自由描述，例如 `"短发，眼镜，文艺风"`），但**不直接拼进 prompt**。原因：

- notes 来源不稳定，偶尔会有上游模型的 hallucination 或冗余形容词，拼进去会让 prompt 失控。
- 但在 step.input_json 里保留，让任务中心展示「识别说明」，方便用户判断要不要重新生成或手动覆盖。

如果未来确认 notes 质量稳定，可以追加为 `extra_requirements` 的兜底，但本期不做。

---

## 7. 边界与错误处理

| 场景 | 处理 |
|---|---|
| 参考图不属于当前用户 | `_validate_owned_images` 抛 403 `image_not_owned` |
| 参考图已被删除 | 同上链路报 404 `image_not_found` |
| vision 抽取超时 / 失败 | 抽取函数返回空 `ReferenceProfile`；handler 检查：如果用户也没显式传 age_segment，直接 422 `reference_extract_failed`，文案：「无法识别参考图人物特征，请换一张更清晰的人像，或切回文生图模式」。注意：抽取只决定文字辅助约束；参考图本身仍会以 attachment 形式传给上游用于身份复刻，所以即便 vision 抽错也不会致命 |
| 参考图不是人像（vision 给出 `notes="not a person"`） | 抽取返回 `gender / age_segment` 为空 → 同上 422，文案：「未在参考图中检测到人物，请上传一张包含人脸或半身的照片」 |
| 参考图人物被遮挡（口罩 / 墨镜 / 背面）| 不报错，让上游模型按可见特征推断；prompt 已包含「infer the unseen sides」指令。任务中心展示侧不做特殊提示 |
| 多人合影 | vision 抽取会按主体人物给出，不在 API 层做裁剪；如果用户对结果不满意，建议引导用户重新上传单人照（文案放在前端上传区域 placeholder） |
| count 超限 | 沿用现有 `MODEL_LIBRARY_GENERATE_COUNTS` 校验 |
| 上传 > 10MB | 前端拒绝，不调 API |
| 上传非 png/jpg/webp | 前端拒绝；后端 `/images/upload` 现有 MIME 校验兜底 |
| 同一参考图重复生成 | 不去重，每次独立 Run；用户可能就是要多次生成挑选最像的一张 |

---

## 8. 安全 & 合规

1. **儿童 / 未成年**：vision 抽取出 `age_segment ∈ {toddler, child, teen}` 时不需要特殊处理（模特库本来就支持这些段，与现有文生图同策略）。
2. **NSFW**：复用现有 `/images/upload` 的内容审核与 provider 侧的安全过滤；本期不新增策略。
3. **审计**：`run.metadata_jsonb.reference_image_id` + `extracted_profile` 已落库，后续若需要追溯「某张生成图来自哪张参考图」可直接 JOIN。

---

## 9. 实施步骤（按 PR 拆分）

### PR-1：Vision tagging 抽离到 `lumen_core`（纯重构）

- 把 `apps/worker/app/tasks/model_library_tagging.py` 中的 `AutoTagResult`、provider chain 调用、JSON 解析逻辑移到 `packages/core/lumen_core/vision_tagging.py`。
- worker 文件改为薄壳；poster_style_tagging.py 同步切换。
- 行为不变，所有现有测试通过。

### PR-2：后端 mode 分支 + API 改造

- `schemas.py`：新增 `mode` / `reference_image_id` 字段 + validator。
- `_apparel_library_reference.py`：新文件，封装 `extract_reference_profile()`。
- `workflows.py`：改造 `generate_apparel_model_library_job` + `_enqueue_model_library_generate_tasks` + `_model_library_generate_prompt` + `_model_library_run_inputs` + `_job_from_library_run`。
- 单测：抽取 mock + merge 策略 + intent 选择 + workflow_meta 字段写入。

### PR-3：前端 UI

- `apiClient.ts`：interface 同步。
- `ModelLibraryReferenceUploader.tsx`：新组件。
- `ModelLibraryGenerator.tsx`：mode tab + 字段互斥隐藏 + 提交 payload 改造。
- `ModelLibraryJobsPanel.tsx`：来源角标 + 详情区参考图与抽取结果展示。
- `npm run type-check && npm run lint && npm run build` 全绿。

### PR-4：联调 & 文档

- docker compose 拉起本地 stack，端到端走两种 mode 各一次。
- 更新 `docs/apparel-model-library-design.md`，在第 2 节加一条「2.4 参考图驱动生成」。
- 若发版，按 `MEMORY.md::Lumen Release Workflow` 走 version bump + tag。

---

## 10. 测试计划

### 10.1 后端单元测试

- `tests/api/test_apparel_model_library_generate.py`：
  - `test_text_mode_unchanged`：mode=text + 不传 reference_image_id，行为与现状完全一致（snapshot run.metadata_jsonb / step.input_json）。
  - `test_reference_mode_happy_path`：mock vision 返回 `{age_segment: "young_adult", gender: "female", appearance_direction: "east_asian", style_tags: ["温柔亲和"], notes: "..."}` → 校验 task.workflow_meta、step.input_json、prompt 包含「Use the attached reference image ONLY」。
  - `test_reference_mode_user_override_wins`：body 同时传 `age_segment="adult"` 和 reference_image_id，最终 age_segment=adult。
  - `test_reference_mode_extract_fail_no_override`：vision 返回空 + body 未传字段 → 422。
  - `test_reference_mode_extract_fail_with_full_override`：vision 返回空但 body 传齐 age_segment/gender → 走兜底，正常入队（视团队偏好决定，可选）。
  - `test_reference_mode_image_not_owned`：参考图属于别人 → 403。
  - `test_validator_blocks_reference_image_id_in_text_mode`：text + reference_image_id → 422。

### 10.2 前端

- Storybook 或本地：mode 切换、上传成功 / 失败、未上传时按钮 disabled、覆盖项 placeholder 文案正确显示。
- E2E（如已有 Playwright）：上传一张本地 fixture 人像 → 提交 → 任务中心出现新 Job → 等待 succeeded → 缩略图渲染。

### 10.3 上游兼容

- 验证至少一个主用 provider（Responses / Gemini / 其它已配置链路）在 `intent=IMAGE_TO_IMAGE + attachment=[ref]` 下能正常出 2×2 contact sheet。如某个 provider 不支持多模态参考图，由 provider chain 自动降级到支持的下游 —— 不在本方案范围内修改。

---

## 11. 监控 & 后续

- 上线后观察：
  - `mode=reference_image` 占比（埋点：API 侧 metric `apparel_model_library_generate_mode_total{mode}`）。
  - 抽取失败率（metric `apparel_model_library_reference_extract_total{result}`）。
  - 任务最终状态分布对比 text vs reference_image（看是否参考图模式失败率更高、要不要调整 prompt）。
- 后续可演进方向（**不在本期实施**）：
  - 真正的 IP-Adapter / 同人复刻模式（`mode=identity_lock`），需要选型支持人像 LoRA 的下游 provider，并补一套肖像权审批流。
  - 抽取阶段加缓存（同一 image_id 5 分钟内复用），减少 vision 调用。
  - 批量参考图上传（一次抽取多张，按抽取结果聚类后批量生成）。
