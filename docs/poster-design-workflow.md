# 海报制作工作流方案

> 状态：方案设计中（V1 MVP 待实现）
> 适用范围：Lumen 项目工作流扩展功能（继 apparel-model-showcase 之后第二个结构化工作流模板）
> 用户决策：风格库为主 + 参考图为辅；文字层独立 Canvas 渲染避免 AI 错字；多尺寸走 fixed preset 直出，复用 4K 能力；多变体走多任务 stagger 入队，不依赖上游 `n>1`。

## 1. 背景与目标

Lumen 当前已经具备：

- 文生图、图生图、视觉问答与长任务能力
- direct image path（`POST /v1/images/generations` 与 `POST /v1/images/edits`）
- responses fallback（`/v1/responses` + `image_generation` 工具，gpt-5.4 + reasoning.effort=high）
- provider failover、image circuit breaker、rate limit cooldown、retry cache buster
- 4K 显式 fixed_size 直出能力（`MAX_EXPLICIT_PIXELS = 8_294_400`，已实测 `3840x2160`）
- 多任务并发 stagger 入队机制（`IMAGE_MULTI_GEN_STAGGER_S = 5`，cap 30s）
- 通用工作流基础设施（apparel-model-showcase 已落地）

海报制作不是简单的“一句文案生一张图”。它需要：

1. 用户文案做语义切分，确定信息层级（标题、副标、卖点、CTA）。
2. 风格基线（库选模板，或用户上传参考图后用 vision 抽取）。
3. 视觉层（背景 AI 生成）+ 文字层（前端 Canvas 渲染）的分层合成。
4. 同一视觉跨多尺寸保持一致。
5. 自动质检防止 AI 错字、配色漂离品牌色、安全区被切。

本功能不是替代通用 `text_to_image` / `image_to_image`，而是在它们之上增加一个面向海报场景的“分阶段、有确认点、有质量门槛”的工作流层。

目标：

1. 用户给定一段文案 + 风格选择或参考图，系统产出一组成品海报。
2. 多尺寸（朋友圈方图、抖音 9:16、横版 16:9、小红书 3:4 等）一次产出且视觉一致。
3. 文字层独立可编辑，不依赖 AI 渲染主标题、副标题、CTA 等核心信息。
4. 可质检、可局部返修、可批量复用风格。

## 2. 设计原则

### 2.1 风格先定，再上文案

第一阶段先确认视觉风格（背景 AI 生成的母版），第二阶段再叠加文字层。这样可以把“风格不满意”和“文字不满意”分开处理，降低返工成本。

### 2.2 双风格入口

风格选择支持两条入口：

- **风格库**：项目维护一组精选风格模板，每个模板自带 prompt 模板、palette、推荐 layout。
- **参考海报上传**：用 vision 抽取风格特征（配色、情绪、构图、装饰关键词），转换为 prompt 注入。

两条路最终都汇聚到同一份“风格摘要”，进入背景生成阶段。MVP 先做风格库，参考图上传留 V2。

### 2.3 文字层独立渲染

主标题、副标题、卖点、CTA、价格等核心文字由前端 Canvas 渲染，**不依赖 AI 画字**。理由：

- AI 文字仍存在错字和字体不一致风险，海报对文字精度要求高。
- 用户后期改文案不应触发重生图。
- 字体、字号、行距、对齐等参数需要精确控制。

仅装饰性元素（场景中的招牌、tag、手写感装饰）允许 AI 渲染，且仅出现在画面非 safe area 区域。

### 2.4 多尺寸用 preset 直出

项目已支持 4K preset（`packages/core/lumen_core/sizing.py`），9:16、16:9、1:1、3:4、2:3、21:9 等比例都能 fixed_size 直接打到上游。海报多尺寸走“母版生成 → 其他尺寸以母版为 reference 的 edits 重出”，**不需要 outpainting 扩画布**。

### 2.5 多变体走多任务

每个尺寸的多变体不依赖上游 `n>1`（gateway 不可靠）。统一走“1 个变体 = 1 个 Generation 任务”，复用现有 stagger 5s / cap 30s 入队机制，避免同 prompt 同账号触发上游内部 race。

### 2.6 阶段化 + 可暂停

复用 apparel 工作流模型：每阶段独立 `waiting_input / running / needs_review / approved / failed / completed` 状态，关窗后可恢复，任务状态不丢失。

## 3. 核心用户旅程

### 3.1 发起项目

入口路径：

```text
创作 -> 选择"海报制作" -> 创建项目 -> 进入项目详情页
```

项目新建页要求用户完成：

1. 输入或粘贴文案（最长 `MAX_PROMPT_CHARS = 10000` 字符）。
2. 选择风格入口：从风格库选 1 个模板，或上传 1 到 3 张参考海报（V2）。
3. 选择目标尺寸组合：默认勾选朋友圈方图（1:1）+ 小红书（3:4）+ 抖音（9:16）+ 横版（16:9）。
4. 上传可选品牌资产：Logo、产品图、品牌主色、字体偏好。
5. 选择质量模式：标准（auto，约 1.57M 像素）或高级（fixed 4K preset）。
6. 点击「创建项目并开始分析」。

### 3.2 文案语义切分

系统对文案做结构化分析，输出：

```json
{
  "main_title": "限时五折",
  "subtitle": "全场夏季新品",
  "selling_points": ["满 200 减 50", "全场包邮"],
  "cta": "立即抢购",
  "price": "¥99 起",
  "tone": "促销/紧迫感",
  "info_density": "high"
}
```

用户可以直接确认，也可以手工修正。`info_density` 决定后续 layout 模板的选择：

- `high`：双栏布局，多卖点排列
- `medium`：标题 + 副标 + 单 CTA
- `low`：大留白，仅主标题

### 3.3 风格摘要

如果走风格库，直接读取模板自带的 prompt 模板 + palette + 推荐 layout。

如果走参考图（V2），调用 vision 抽取：

```json
{
  "primary_palette": ["#1a1a1a", "#ff6b35", "#fff5ee"],
  "mood": "高对比、复古、印刷感",
  "composition_hint": "中心对称 / 大留白 / 标题占上 1/3",
  "decoration_keywords": ["半调网点", "粗描边", "几何切割"]
}
```

无论哪条路，最终都产出统一的风格摘要 JSON，作为后续生成阶段的硬约束。

### 3.4 母版生成

系统按“风格摘要 + 信息密度 + 品牌资产”生成 1:1 母版。固定输出 4 张候选，用户选 1 张作为后续多尺寸的视觉基线。

母版只生成“背景层”，不在画面里画文字（prompt 显式 negative：`do NOT render any text, headline, glyph`）。文字位置预留 safe area，安全区由 layout 模板决定。

### 3.5 用户确认母版

用户可以执行：

- 选择一张候选母版作为最终方案
- 对单张候选发起重生（保留风格摘要，仅替换该候选）
- 修改风格摘要后整组重生（4 张全部重新生成）

母版卡片必须明确标注“未叠加文字，仅用于确认风格”，避免用户误以为是最终成品。

### 3.6 多尺寸批量生成

用户确认母版后，系统：

1. 按目标尺寸列表逐个生成（如 9:16 → `2160x3840`，16:9 → `3840x2160`，3:4 → `2448x3264`）。
2. 每个尺寸把母版作为 reference image 走 `/v1/images/edits`，multipart `image[]` 字段传母版。
3. prompt 强约束：`match the visual style, color palette, and composition logic of the reference exactly`。
4. 每个尺寸独立 Generation 任务，按 `IMAGE_MULTI_GEN_STAGGER_S` 错开入队。
5. 每张生成完成后，前端 Canvas 叠加文字层，输出预览。

### 3.7 文字层叠加

前端使用 Konva / Skia 类的 2D 渲染引擎，按 layout 模板放置：

- main_title、subtitle、selling_points、cta、price 等文本元素
- Logo、产品图等品牌资产元素

字号按短边比例缩放，字体优先从品牌资产读取，颜色从 palette 取主对比色。用户可以：

- 拖拽元素位置、调字号
- 一键替换文案保留版式
- 切换 layout 模板（centered / split / banner 等）

### 3.8 自动质检

系统对每张成品（背景 + 文字层合成后）做自动质检：

- safe area 内是否有 AI 生成的乱码文字（OCR 校对）
- 配色是否偏离品牌 palette 过多（直方图比对）
- 多尺寸视觉一致性（CLIP score vs 母版）
- 主体是否被画面边缘裁切（safe area 检测）

不达标的图标记 `needs_review`，给出具体返修建议。

### 3.9 返修

用户可以选择：

- 对单张成品的背景层发起重生（保留文字层）
- 修改文字层（不重生背景，无 AI 调用，纯前端）
- 替换风格摘要（重生母版后整组重新生成）

海报工作流 V1 不直接暴露 inpaint 入口，但底层能力已就绪（创作会话已具备 inpaint），后续返修体验可逐步并轨。

### 3.10 交付

- 成品下载（PNG / JPG）
- 入图库（标签为 `poster_design` 项目类型）
- 保存“海报项目”到项目库（可后续替换文案批量复用同风格）

## 4. 信息架构与界面设计

### 4.1 一级导航

复用 apparel 已建议的：

```text
创作 / 项目 / 图库 / 我的
```

“海报制作”作为创作入口下的一个工作流模板，与“服饰模特展示图”平级。

推荐路由：

```text
/projects/poster-design/new
/projects/:projectId
```

### 4.2 项目详情页

复用 apparel 的三栏布局：

```text
左侧：阶段导航
中间：当前阶段操作区
右侧：项目资产与约束
```

阶段导航：

```text
1 文案输入
2 风格选择
3 文案分析
4 母版生成
5 母版确认
6 多尺寸生成
7 文字层编辑
8 质检返修
9 交付
```

每个阶段都有独立状态：`waiting_input / running / needs_review / approved / failed / completed`。

### 4.3 母版确认阶段 UI

- 顶部：用户文案摘要 + 风格摘要标签
- 中部：4 张候选母版网格，每张下方有“选择此风格 / 微调 / 重生本张”按钮
- 底部：风格描述摘要可编辑后整组重生

### 4.4 多尺寸生成阶段 UI

- 顶部：已确认母版缩略图，作为视觉基线展示
- 中部：各尺寸卡片网格（生成中 / 已完成 / 失败状态分别用不同视觉表示）
- 每张完成后：可点击进入文字层编辑器

### 4.5 文字层编辑器

类似 Canva 简化版的画布编辑器：

- 左侧：文本元素列表（main_title / subtitle / selling_points / cta / price）
- 中间：Canvas 实时预览（背景层 + 文字层合成）
- 右侧：layout 模板切换、字体、字号、颜色、行距

文字层编辑全程纯前端，不调上游接口。导出时把 Canvas 合成为成品 PNG。

### 4.6 交互细节

用户确认动作必须显式：

- “选择此风格”
- “开始生成多尺寸成品”
- “导出成品”

高成本生成前显示预计消耗：

- 母版阶段：4 张 1:1 候选
- 多尺寸阶段：N 个尺寸 × 1 张
- 是否使用 4K 高质量模式

不要让用户误以为母版图就是最终成品。母版卡片必须标注“未叠加文字，仅用于确认风格”。

## 5. 生成策略

### 5.1 文案语义切分

走 vision_qa 路径（无图也能做纯文本结构化），输出固定 schema JSON。失败时降级为前端按行/标点切分。

### 5.2 风格摘要

风格库路径：直接读取模板字段，无 AI 调用。

参考图路径（V2）：调用 vision_qa 一次抽取，输出 palette + mood + composition_hint。

两条路的输出都汇聚到同一份风格摘要，作为后续 prompt 的硬约束字段。

### 5.3 母版生成

入参：

- 风格摘要
- 文案信息密度（决定 layout 类型 + safe area 位置）
- 品牌资产（如有 Logo / 产品图，作为 reference 传入）
- 质量模式：`size_mode=fixed` + `2880x2880`（高级）或 `size_mode=auto`（标准，约 1.57M）

接口：`/v1/images/generations`，固定输出 4 张候选（4 个独立 Generation 任务，stagger 入队）。

### 5.4 多尺寸 reference 生成

入参：

- 已确认母版（作为 reference image）
- 目标尺寸 preset（如 `2160x3840`、`3840x2160`、`2448x3264`）
- 风格摘要
- safe area 位置约束

接口：`/v1/images/edits` multipart，`image[]` 字段传母版，size 用 fixed preset。

每个尺寸独立 Generation 任务，按 `IMAGE_MULTI_GEN_STAGGER_S = 5` 错开入队。失败单独 retry，不阻塞其他尺寸。

### 5.5 推荐输出规格

母版：

- 4 张候选
- 1:1 比例
- 高级模式：`2880x2880`（高质量配置）
- 标准模式：`size=auto`（约 1.57M 像素）

多尺寸成品：

- MVP 默认 4 个尺寸：1:1 / 9:16 / 16:9 / 3:4
- 每个尺寸 1 张（变体在母版阶段已选定，多尺寸阶段不再多变体）
- 4K 仅对用户标记的主图使用

### 5.6 timeout 注意

复用项目 4K 长任务 timeout layered envelope：

```text
nginx 3600 / 1800 → arq 1800 → task 1500 → upstream 660
```

海报多尺寸批量任务**必须拆成独立 Generation 任务**，不能在单任务内串行生成多尺寸，否则会撞穿 task 1500s 上限。

## 6. 数据模型建议

复用 apparel 的 `workflow_runs / workflow_steps` 表，新增海报特有表。

### 6.1 workflow_runs

复用现有表，`type = "poster_design"`。

### 6.2 workflow_steps

复用现有表，`step_key` 取值：

- `copy_input`
- `style_selection`
- `copy_analysis`
- `master_generation`
- `master_approval`
- `multi_size_generation`
- `text_layer_editing`
- `quality_review`
- `delivery`

### 6.3 poster_styles

风格库表，系统内置 + 用户自建共享。

关键字段：

- `id`
- `name`
- `category`：促销 / 节日 / 品牌 / 社媒 / 通用
- `cover_image_id`
- `prompt_template`
- `palette_json`
- `recommended_layouts`
- `created_by`：null = 系统内置
- `usage_count`

### 6.4 poster_masters

记录母版候选。

关键字段：

- `id`
- `workflow_run_id`
- `candidate_index`
- `image_id`
- `style_summary_json`
- `status`
- `selected_at`

### 6.5 poster_renders

记录多尺寸成品。

关键字段：

- `id`
- `workflow_run_id`
- `master_id`
- `aspect_ratio`
- `size`
- `background_image_id`：纯 AI 背景层（无文字）
- `composition_json`：文字层渲染参数
- `final_image_id`：合成后成品
- `quality_report_id`

### 6.6 quality_reports

复用 apparel 表，检查项扩展为海报特有：

- `safe_area_ocr_score`
- `palette_drift_score`
- `consistency_score`：vs 母版的 CLIP 相似度
- `aesthetic_score`
- `artifact_score`
- `issues_json`
- `recommendation`

## 7. API 设计草案

### 7.1 创建工作流

```http
POST /api/workflows/poster-design
```

请求：

```json
{
  "conversation_id": "conv_123",
  "copy_text": "限时五折，全场夏季新品，满 200 减 50，立即抢购",
  "style_source": {
    "type": "library",
    "style_id": "style_promo_01"
  },
  "target_sizes": ["1:1", "9:16", "16:9", "3:4"],
  "brand_assets": {
    "logo_image_id": "img_logo",
    "product_image_id": null,
    "primary_color": "#ff6b35",
    "font_family": "PingFang SC"
  },
  "quality_mode": "premium"
}
```

返回：

```json
{
  "workflow_run_id": "wf_abc",
  "status": "running",
  "current_step": "copy_analysis"
}
```

`style_source.type` 可选值：`library` / `reference_image`（V2）。

### 7.2 确认文案分析

```http
POST /api/workflows/{workflow_run_id}/steps/copy-analysis/approve
```

请求：

```json
{
  "corrections": {
    "main_title": "限时五折",
    "subtitle": "全场夏季新品",
    "selling_points": ["满 200 减 50", "全场包邮"],
    "cta": "立即抢购",
    "price": null
  }
}
```

### 7.3 生成母版

```http
POST /api/workflows/{workflow_run_id}/masters
```

请求：

```json
{
  "candidate_count": 4,
  "size_mode": "fixed",
  "size": "2880x2880"
}
```

返回每个候选对应的 task_id 列表。4 个候选 = 4 个 Generation 任务，stagger 入队。

### 7.4 确认母版

```http
POST /api/workflows/{workflow_run_id}/masters/{master_id}/approve
```

### 7.5 生成多尺寸

```http
POST /api/workflows/{workflow_run_id}/renders
```

请求：

```json
{
  "sizes": ["9:16", "16:9", "3:4"],
  "use_master_as_reference": true,
  "quality_mode": "premium"
}
```

返回每个尺寸对应的 task_id。

### 7.6 编辑文字层

```http
PATCH /api/workflows/{workflow_run_id}/renders/{render_id}/composition
```

请求：

```json
{
  "elements": [
    {"type": "main_title", "text": "限时五折", "x": 0.5, "y": 0.2, "font_size": 0.08, "color": "#1a1a1a"},
    {"type": "subtitle", "text": "全场夏季新品", "x": 0.5, "y": 0.32, "font_size": 0.04, "color": "#1a1a1a"},
    {"type": "cta", "text": "立即抢购", "x": 0.5, "y": 0.85, "font_size": 0.05, "color": "#ff6b35"}
  ],
  "layout_template": "centered"
}
```

服务端不调用上游，仅落库 composition_json 并触发前端重新合成成品（Canvas 导出 PNG 后写入 final_image_id）。

### 7.7 返修

```http
POST /api/workflows/{workflow_run_id}/renders/{render_id}/revise
```

请求：

```json
{
  "scope": "background",
  "instruction": "色调再暖一点，留更多空间给标题"
}
```

`scope` 可选值：

- `background`：仅重生背景，文字层保留
- `text_only`：仅改文字层，无 AI 调用
- `style`：重生母版，整组多尺寸重新生成

## 8. Prompt 合约

### 8.1 文案分析 Prompt

用途：从用户原始文案抽取结构化信息。

要点：

- 输出固定 schema JSON
- 不确定字段标 `null`
- 计算 `info_density` 字段（high / medium / low）

### 8.2 母版生成 Prompt

```text
Create a clean poster background design for ecommerce or social marketing usage.
This is a base layer; do NOT render any title, headline, body text, price, or CTA inside the image.
Reserve a clear safe area in the {layout_safe_area} region for text overlay added later.
Style direction: {style_summary}.
Color palette priority: {palette}.
Mood: {mood}.
Composition: {composition_hint}.
Brand asset constraints: {brand_logo_constraint}.
Avoid: real text, garbled glyphs, watermark, signature, busy textures inside the safe area.
Output a high-quality, print-ready visual base layer.
```

注意：prompt 前缀必须保持稳定以利用上游 prompt cache（`upstream.py` 顶部注释要求）。`{...}` 占位符用确定性插值，不包含时间戳、随机串、用户 ID。

### 8.3 多尺寸 reference Prompt

```text
Re-render the reference poster background into a {target_aspect} composition.
Match the visual style, color palette, mood, and decoration logic of the reference image exactly.
Adapt the composition naturally to the new aspect ratio without distortion.
Keep the safe area in the {layout_safe_area} region clear of text or busy elements.
Do NOT add real text, headlines, or CTA inside the image.
Reference palette: {palette}.
```

### 8.4 风格抽取 Prompt（参考图路径，V2）

```text
Analyze the visual style of this poster reference.
Output structured JSON with the following fields:
- primary_palette: 3-5 dominant hex colors
- mood: 1-2 sentence description of emotional tone
- composition_hint: short description of layout structure
- decoration_keywords: 3-6 keywords describing decorative elements
Do not describe the literal text content; focus only on visual style.
```

### 8.5 质检 Prompt

```text
Review this poster image for quality issues.
Check:
- Are there any garbled or incorrect text strings inside the image?
- Does the color palette match: {expected_palette}?
- Is the safe area in {layout_safe_area} clear for text overlay?
- Are there any visible AI generation artifacts on edges or fine details?
Output structured JSON with scores 0-100 and specific issues with severity level.
```

## 9. 质量标准

### 9.1 风格一致性

合格：

- 母版与各尺寸版本视觉风格相同（CLIP score ≥ 0.82）
- palette 主色偏移 ≤ 15%

强制返修：

- 明显变成另一种风格
- 主色完全不一致

### 9.2 文字渲染正确性

合格：

- safe area 内 AI 不生成任何文字
- 文字层 Canvas 渲染清晰可读，对比度达 WCAG AA

强制返修：

- AI 在画面里生成乱码字
- 文字层与背景对比度过低导致不可读
- 文字溢出画布边界

### 9.3 安全区遵守

合格：

- layout 模板定义的 safe area 内主体清晰、无遮挡
- 文字位置在任一目标尺寸下都不被裁切

强制返修：

- 主体被画面边缘切掉
- 文字溢出或被装饰元素遮挡

### 9.4 高级质感

合格：

- 边缘干净，没有明显 AI 生成瑕疵
- 装饰元素与主题协调，不杂乱
- 对比度充足，主体突出

强制返修：

- 明显 AI artifact（变形、错位、重影）
- 整体观感廉价或杂乱

## 10. MVP 范围

第一版只做最小可用闭环：

1. 用户输入文案 + 选择风格库 1 个模板（参考图上传放 V2）。
2. 文案自动语义切分 + 用户修正。
3. 母版生成 4 张候选（1:1）。
4. 用户选 1 张母版。
5. 4 个目标尺寸（1:1 / 9:16 / 16:9 / 3:4）批量生成。
6. 文字层编辑器：基础元素拖拽位置、改字号、改颜色，3 个 layout 模板（centered / split / banner）。
7. 自动质检：safe area OCR + palette 比对 + 一致性 CLIP。
8. 单张返修：背景重生 / 文字层重编。
9. 下载 PNG / 入图库。
10. 对单张参考图的局部 inpaint（涂抹 mask + 编辑意图 → 局部重画）。

MVP 暂不做：

- 参考图上传（V2）
- 印刷模式 CMYK + 出血位 + 300 DPI（V2）
- 节日热点模板自动推荐（V2）
- 文字合规检查 / 字体版权（V2）
- 一键换文案保版式批量产出（V2）
- 动态海报 GIF / MP4（V3）
- 团队品牌资产共享（V3）
- 数据回流驱动风格推荐（V3）

## 11. 后续版本

### 11.1 V2：风格灵活性

- 参考图上传 + vision 风格抽取
- 用户自建风格模板并保存到风格库
- 一键换文案保版式批量产出（同模板 N 套文案 → N 张海报）

### 11.2 V2：印刷与编辑

- 印刷模式：CMYK 色彩空间 + 出血位 + 300 DPI
- 文字合规检查（广告法敏感词 + 字体版权）

### 11.3 V2：智能化

- 节日 / 热点模板自动推荐（按日历）
- 历史海报二次利用（换季 / 换促销主题一键改色）

### 11.4 V3：动态与协作

- 动态海报：GIF / MP4 输出
- 团队品牌资产库（多用户共享 Logo / 字体 / palette）
- 数据回流：投放 CTR 驱动下次风格推荐

## 12. 风险与对策

### 12.1 AI 在 safe area 内画字

风险：母版 prompt 已显式禁止文字，但 GPT Image 仍可能生成装饰性文字字符串。

对策：

- prompt 中 negative 列表明确：`do NOT render any text, headline, glyph, garbled characters`
- 质检阶段对 safe area 做 OCR
- 检测到任何字符直接判定 `needs_review` 并自动重试一次

### 12.2 多尺寸视觉漂移

风险：以母版为 reference 重出其他尺寸，色调或装饰元素可能偏离。

对策：

- prompt 强调 `match the visual style, color palette, mood, and decoration logic of the reference image exactly`
- palette 显式作为 prompt 字段
- 质检阶段 CLIP score 阈值 0.82
- 不达标自动重试一次后再标 `needs_review`

### 12.3 文字溢出 / 裁切

风险：文字层模板在极端比例（如 21:9）下溢出画布。

对策：

- 字号按短边比例缩放
- 触发最小字号阈值时自动减字 / 换行 / 切换 layout
- 不同比例配套不同 layout 模板，不强求一个模板通吃所有比例

### 12.4 4K 长任务超时

风险：4K 多尺寸批量超出现有 timeout envelope。

对策：

- 严格遵循“每个尺寸 = 独立 Generation 任务”
- 不在单任务里串行生成多尺寸
- 复用现有 4K timeout layered envelope（nginx 3600 / arq 1800 / task 1500 / upstream 660）

### 12.5 用户在母版阶段误以为是成品

风险：母版只有背景没文字，用户可能不理解。

对策：

- 母版卡片明确标注“未叠加文字，仅用于确认风格”
- 阶段条强调当前是“母版确认”而非“成品”
- 选中按钮文案用“选择此风格”而不是“完成”

### 12.6 prompt cache miss 导致成本上升

风险：风格 prompt 模板抖动会让上游 prompt cache 全量 miss。

对策：

- 风格模板 prompt 前缀固定，仅末尾追加用户具体输入
- tools 数组按 name 排序后再发
- 不在 instructions / 历史拼装顺序里塞动态字段

### 12.7 上游 mask 行为已实测确认

2026-05-07 spike 实测：上游 flux.infpro.me + gpt-image-2 网关支持 OpenAI 标准的 mask-based inpaint，但**必须用 OpenAI invariant prompt 模板**才能正确 inpaint，否则 mask 区会被填黑而 prompt 内容画到画面别处。

固定的 prompt 模板：

```text
Inside the masked region, {user_intent}. Preserve everything outside the mask exactly: colors, geometry, lighting. Do not add anything outside the masked area.
```

应用：

- 用户输入只填编辑意图（如"换成红苹果"），后端固定加 invariant 包装
- 短 prompt（"Replace the masked region with X, keep the rest the same"）已实测会失败
- 局部 inpaint 因此从原 V2 提前到 V1（创作会话 i2i 路径），海报工作流 V1 仍不直接暴露 inpaint 入口，但底层能力已就绪

## 13. 验收标准

功能可上线的最低标准：

1. 用户能从一段文案 + 风格选择启动完整工作流。
2. 系统能生成 4 张母版候选并让用户选定。
3. 系统能基于选定母版批量生成 4 个尺寸的成品。
4. 每个尺寸的成品文字层独立可编辑，不触发上游调用。
5. 多尺寸视觉风格保持一致（CLIP ≥ 0.82）。
6. 母版背景 safe area 内不出现 AI 生成的乱码文字。
7. 每张成品都有质检报告。
8. 用户能对至少一张成品发起背景重生或文字层重编。
9. 工作流关窗后可恢复，任务状态不丢失。
10. 4K preset 大图任务不会超出 timeout envelope。

## 14. 推荐实施顺序

第一阶段：基础工作流

1. 增加 `poster_styles` 表 + 系统内置 5 到 10 个风格模板。
2. 增加 `poster_masters` / `poster_renders` 表。
3. 接入文案语义切分（复用 vision_qa 路径）。
4. 母版生成 + 用户确认 UI。

第二阶段：多尺寸 + 文字层

1. 多尺寸 reference 生成（复用 stagger 入队）。
2. 前端 Canvas 文字层编辑器（基础元素拖拽 + 3 个 layout 模板）。
3. 文字 + 背景合成导出。

第三阶段：质量门槛

1. 自动质检（OCR + palette + CLIP）。
2. 单张返修（背景重生 / 文字层重编）。
3. 下载入图库。

第四阶段：复用与扩展

1. 用户自建风格模板。
2. 一键换文案保版式批量产出。
3. 参考图上传（V2）。
4. 印刷模式与节日模板（V2）。

## 15. 结论

海报制作工作流是 Lumen 在 apparel-model-showcase 之后的第二个结构化工作流模板。它复用了项目已有的：

- 工作流基础设施（workflow_runs / workflow_steps / 阶段状态机）
- 4K preset 直出能力（`sizing.py`）
- 多任务 stagger 并发机制（`IMAGE_MULTI_GEN_STAGGER_S`）
- responses fallback / circuit breaker / retry / cache buster
- 自动质检框架（OCR + palette + CLIP）
- prompt cache 友好的前缀稳定约定

核心创新点：

- “背景 AI 生成 + 文字 Canvas 渲染”分层架构，避免 AI 错字
- 多尺寸用 reference image 重出，复用 4K preset 直出，无需 outpainting
- safe area + OCR 联合质检，防止 AI 在文字位置生成乱码

V1 MVP 闭环简洁，工程量集中在 UI（项目页 + 文字层编辑器）+ 工作流编排 + 质检扩展三个面；V2 / V3 扩展空间充足，可逐步演进为更完整的设计工具。
