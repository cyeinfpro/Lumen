# gpt_image_playground 深度分析与 Lumen 借鉴建议

分析对象：[CookSleep/gpt_image_playground](https://github.com/CookSleep/gpt_image_playground)

本地拉取路径：`/tmp/gpt_image_playground`

初版日期：2026-05-18

修订日期：2026-05-18（v2 — 对照 Lumen 当前实现重新核实，调整优先级，删除已完成项）

## 0. 修订说明

第一版文档在写作时高估了 Lumen 缺口，把多个 Lumen 已经做完的事情仍写成"待办建议"。本次修订做了三件事：

1. 对照 `apps/web` 与 `apps/api` 当前代码，逐条核实 playground vs Lumen 的差距。
2. 把"Lumen 已经实现"的部分从"建议项"改写成"现状记录"，避免误导优先级。
3. 把"Lumen 真正缺失或落后"的部分集中到第 5 节修订后的优先级列表，并补充了量化判断标准。

读者只关心结论可以直接跳到 [第 5 节](#5-对-lumen-的具体借鉴优先级修订版) 和 [第 7 节](#7-最推荐的落地路线修订版)。

## 1. 总体判断

`gpt_image_playground` 不是一个复杂后端系统，而是一个把生图体验做得很细的纯前端工作台。它的价值不在架构规模，而在几个产品工程层面的细节：

1. 把模型参数、服务商差异、结果解析、错误诊断做成了明确的前端控制环。
2. 把"参考图、遮罩、历史任务、再次编辑"打通成一个低摩擦工作流。
3. 把不稳定的图片上游问题前置为可解释的状态，比如 CORS、返回结构不一致、提示词被改写、异步任务恢复。
4. 把本地历史、缩略图、导入导出做得足够完整，使单用户使用时很顺。

对 Lumen 来说，不能照搬它的"浏览器即后端"形态。Lumen 已经有 FastAPI、Worker、PostgreSQL、Redis、Provider Pool 和服务端图片资产链路；但在生图交互、局部修改细节、参数解释、服务商适配和故障模拟这些"细颗粒度的工程化"上，playground 仍然能贡献思路。

**核实后的结论**：Lumen 在 inpaint、参数面板、admin provider 工具链上已经走得比 playground 远；真正可借鉴的增量集中在三块：

- **上游故障模拟器**（Lumen 几乎空白）。
- **结果可解释性的三个微增量**：实际生效参数 diff、revised prompt 回显、provider/proxy/耗时痕迹。
- **多参考图引用 token**（Lumen 在 16 张参考图上限下早晚需要）。

## 2. 生图优化机制

### 2.1 参数规范化是第一层优化

目标项目把尺寸视为核心约束，而不是普通字符串。关键实现见 [/tmp/gpt_image_playground/src/lib/size.ts](/tmp/gpt_image_playground/src/lib/size.ts:1)。

它的约束模型包括：

- 宽高必须规整到 16 的倍数。
- 最长边不超过 3840。
- 长宽比不超过 3:1。
- 总像素限制在 655,360 到 8,294,400 之间。
- 支持 1K、2K、4K 预设，并按比例计算实际输出尺寸。

值得注意的是，它不是非法就简单报错，而是会做归一化：先 round 到 16 倍数，再多轮 scale-to-fit 或 scale-to-fill。这个策略适合用户输入自由尺寸时使用，可以减少"用户以为能生成，实际 API 拒绝"的情况。

**Lumen 现状**：[apps/web/src/lib/sizing.ts](/Users/liangchanghua/Downloads/Image/apps/web/src/lib/sizing.ts:1) 已经有显式 fixed size 校验、4K 预设、像素边界、长宽比限制和前后端对称实现，整体比 playground 更严谨。Lumen 走的是"非法即报错"路线，符合生产级 API 的安全感。

**建议**：保留现有严谨策略；唯一可吸收的是"自由输入自动规整提示"——如果未来开放自定义尺寸输入框，可以在输入旁加一行轻量提示"将自动调整到 16 对齐尺寸（例：1024×1536）"，但仍以服务端校验为准。**优先级 P3，纯 UX**。

### 2.2 质量、尺寸、格式被当作联动参数

目标项目在 [/tmp/gpt_image_playground/src/lib/paramCompatibility.ts](/tmp/gpt_image_playground/src/lib/paramCompatibility.ts:1) 中做 provider-aware 的参数修正：

- fal.ai 最大输出数为 4，OpenAI 类接口最大输出数为 10。
- fal.ai 没有输入图且 size 为 auto 时，强制落到 `1360x1024`。
- fal.ai 的 `quality=auto` 会转为 `high`。
- PNG 输出不发送 `output_compression`。
- Codex CLI 兼容模式下会去掉不支持的质量参数。

这类逻辑的重点不是某个数值本身，而是"用户选择"和"真实上游能力"之间有一个显式兼容层。

**Lumen 现状**：[apps/web/src/store/useChatStore.ts](/Users/liangchanghua/Downloads/Image/apps/web/src/store/useChatStore.ts:2218) 已会把 composer 参数解析为 `image_params`，并联动 `quality`、`size`、`output_format`、`output_compression`、`moderation`。服务端 Provider Pool 也会按 provider capability 做转换。

**建议**：补一个"参数差异回显"——把"用户选择"和"上游实际生效"的差异在图片详情里 diff 出来，让用户知道系统帮他改了什么。这是 P0 微增量之一（详见 5.1）。

### 2.3 提示词防改写和引用标记

目标项目在 Responses API 和 Codex CLI 兼容模式中使用固定前缀：

`Use the following text as the complete prompt. Do not rewrite it:`

实现见 [/tmp/gpt_image_playground/src/lib/openaiCompatibleImageApi.ts](/tmp/gpt_image_playground/src/lib/openaiCompatibleImageApi.ts:21)。这不是万能保护，但它把"不要改写提示词"变成了默认请求约束，尤其适合需要精准复现或局部编辑的场景。

更有价值的是图片引用标记。[/tmp/gpt_image_playground/src/lib/promptImageMentions.ts](/tmp/gpt_image_playground/src/lib/promptImageMentions.ts:3) 用不可见边界字符包住 `@图1`，在 UI 中显示为普通引用，但内部可以：

- 判断光标是否在引用内。
- 插入、删除、重排图片时 remap 引用。
- 发送 API 前把 `@图1` 转成 `[image 1]`。
- 图片不存在时把引用替换成"已移除图片"。

**Lumen 现状**：composer 区（如 `apps/web/src/components/ui/composer/shared/attachments.ts`）目前只处理文件列表，**完全没有引用 token 机制**。Lumen 已经把普通参考图上限从 4 张提到 16 张，对齐 OpenAI GPT image models 的编辑输入上限（带 mask 的 inpaint 仍然是"单主图 + 单 mask"）。

**建议**：把"prompt 防改写前缀"作为请求层默认行为之一（低成本，可立即加）。"图片引用 token"是更大的功能，列为 P1，但应先做用户调研：实际用户是否真的使用 4+ 张参考图、是否会被附件顺序困扰。详见 5.3。

### 2.4 请求执行层对上游差异很敏感

目标项目把图像接口统一成 `callImageApi`，再按 provider 分发到 OpenAI 兼容和 fal.ai，见 [/tmp/gpt_image_playground/src/lib/api.ts](/tmp/gpt_image_playground/src/lib/api.ts:1)。

OpenAI 兼容层的关键点：

- Images API 和 Responses API 双模式。
- Images API 支持生成和编辑两条路径。
- 编辑请求用 multipart，mask 和参考图按上游要求转成 Blob。
- Codex CLI 模式下，多图生成会拆成多个并发单图请求，见 [/tmp/gpt_image_playground/src/lib/openaiCompatibleImageApi.ts](/tmp/gpt_image_playground/src/lib/openaiCompatibleImageApi.ts:229)。
- 解析时同时支持 `b64_json`、HTTP URL、data URL。
- 如果结果不可识别，会把原始响应作为诊断 payload。

fal.ai 层的关键点：

- 会记录 requestId 和 endpoint，便于后续恢复。
- 结果解析时兼容 URL、base64、对象结构。
- 自定义 baseUrl 返回非 fal 格式时，会提示用户改用自定义服务商配置，见 [/tmp/gpt_image_playground/src/lib/falAiImageApi.ts](/tmp/gpt_image_playground/src/lib/falAiImageApi.ts:94)。

**Lumen 现状**：Provider Pool 已经支持 priority、weight、failover、proxy、并发与熔断，能力比 playground 更强。已具备 OpenAI Images / Responses image_generation 双路径。

**建议**：把 playground 的"用户可见诊断"思想吸收进 Lumen 的图片详情：

- 在图片详情中展示请求参数、实际参数、上游 provider、是否 failover、是否 prompt 被改写。
- 在失败时保留原始上游响应的安全摘要。
- 当检测到上游缺参或返回结构不标准时，给出具体修复建议，而不是只显示"生成失败"。

这是 P0 微增量的主体（详见 5.1）。

### 2.5 响应解析和 CORS 处理是体验优化

目标项目把远程图片 URL 下载视为风险点，而不是默认能成功。[/tmp/gpt_image_playground/src/lib/imageApiShared.ts](/tmp/gpt_image_playground/src/lib/imageApiShared.ts:95) 的处理包括：

- 如果返回 HTTP URL，尝试 fetch 并转 data URL。
- fetch 因 CORS 失败时，用 no-cors 探测判断图片是否其实已生成。
- 提示用户复制原始链接或开启 Base64 返回。
- HTTP 非 200、网络离线、链接过期都会给不同错误文案。

**Lumen 现状**：服务端图片代理 + 内部对象存储链路已经规避了浏览器 CORS。Lumen 不会让前端直接 fetch 上游临时 URL。

**建议**：仍可借鉴它的"错误分类思想"。当服务端拉取上游图片失败时，应在图片详情把"生成成功"和"图片下载/转储失败"拆开显示，并保留上游提供的临时 URL 让用户应急。这条并入 P0 微增量。

### 2.6 历史和缩略图缓存提升反复迭代效率

目标项目的 IndexedDB 数据层见 [/tmp/gpt_image_playground/src/lib/db.ts](/tmp/gpt_image_playground/src/lib/db.ts:1)。它把任务、原图、缩略图拆成不同 object store，并用 SHA-256 对图片 data URL 去重。

Store 层还有一套内存 LRU 和缩略图 backfill，见 [/tmp/gpt_image_playground/src/store.ts](/tmp/gpt_image_playground/src/store.ts:42)：

- 原图内存缓存只保留少量，避免 4K data URL 常驻。
- 缩略图缓存保留更多。
- 可见图片优先 backfill 缩略图，后台图片低优先级补齐。
- 根据图片像素量动态控制缩略图生成并发。
- 启动时清理孤立图片，只枚举 key，避免把所有 4K 原图读进内存。

**Lumen 现状**：图片主存已在服务端，前端通过 `display2048` 等派生尺寸消费，不应复制 IndexedDB 主存。

**建议**：可以吸收的只有三点，且都是 P3：

1. 前端 lightbox / gallery 做短生命周期原图 LRU，避免反复重新拉 display2048。
2. 大图缩略图生成和预热应有可见优先级，不要一口气 preload 全部历史。
3. "历史任务导出包"作为用户备份或排查工具，由服务端导出，不要在浏览器单点完成。

## 3. 局部修改机制

### 3.1 局部修改的核心是 mask 与主图一致性

目标项目对 mask 的约束很明确。[/tmp/gpt_image_playground/src/lib/canvasImage.ts](/tmp/gpt_image_playground/src/lib/canvasImage.ts:56) 会同时加载 mask 和 source image，要求宽高完全一致，然后读取 mask alpha 判断覆盖情况。如果尺寸不一致，直接要求重新绘制。

提交任务时也会做一致性保护，见 [/tmp/gpt_image_playground/src/store.ts](/tmp/gpt_image_playground/src/store.ts:1135)：

- 如果有 mask，先把 mask 对应的图片排到参考图第一位。
- 校验 mask 与第一张图一致。
- 如果 mask 覆盖整张图，会弹确认框提醒用户这可能是整图重绘。
- mask 图会作为独立图片存储，任务记录保存 `maskImageId` 和 `maskTargetImageId`。

**Lumen 现状（核实结果）**：

- `submitInpaintTask` 在 `useChatStore.ts` 已会在 `image_to_image + 单张参考图 + mask target 仍指向第一张` 时才发送 `mask_image_id`，方向一致。
- [apps/web/src/components/ui/inpaint/InpaintModal.tsx:40](/Users/liangchanghua/Downloads/Image/apps/web/src/components/ui/inpaint/InpaintModal.tsx:40) 定义 `FULL_COVERAGE_WARN = 0.95`，[InpaintModal.tsx:299-303](/Users/liangchanghua/Downloads/Image/apps/web/src/components/ui/inpaint/InpaintModal.tsx:299) 会在涂抹覆盖率超过 95% 时弹"接近整图重画"警告（虽然是 warning 提示而非二次确认弹窗）。
- mask 导出做了 alpha 全分辨率二值化（见 `MaskBoard.tsx`）。

**结论**：playground 的"提交前用户确认"和"mask 覆盖分类"思路 **Lumen 已基本完成**。差距是确认弹窗强度——目前 Lumen 是允许直接提交并附带 warning 文案，没有阻断式 modal。这是 UX 取向问题，不是缺陷。

**建议**：把"≥95% 是否升级为阻断式确认"列为 UX 待讨论项（P3），暂不动。

### 3.2 mask 预处理控制成本和失败率

目标项目会在进入遮罩编辑前准备工作图，见 [/tmp/gpt_image_playground/src/lib/maskPreprocess.ts](/tmp/gpt_image_playground/src/lib/maskPreprocess.ts:4)：

- 遮罩工作图最长边默认压到 1920。
- 宽高规整为 16 的倍数。
- 非 PNG 转 PNG。
- 保留 original width/height、scale、是否 resize、是否转换等元数据。

这个设计的好处是：

- 降低浏览器 canvas 操作内存压力。
- 降低 mask 上传尺寸。
- 避免上游因文件体积或格式拒绝。
- 让局部修改和官方尺寸限制保持一致。

**Lumen 现状**：[apps/web/src/components/ui/inpaint/MaskBoard.tsx](/Users/liangchanghua/Downloads/Image/apps/web/src/components/ui/inpaint/MaskBoard.tsx:1) 已经在交互层做得更细：stroke 抽稀、实时覆盖采样、暗图颜色自适应、导出时二值化 alpha（见 MaskBoard.tsx:490）。比 playground 更适合生产。

**待评估**：是否需要在"进入 inpaint 前"加一层独立的工作图预处理？目前 MaskBoard 直接基于原图自然尺寸导出 mask，桌面端体验最佳，但 4K 原图在移动端或弱设备上可能压力较大。可考虑的折中：

- 桌面端默认保留原图尺寸导出。
- 移动端或超大图开启 1920 / 2048 工作尺寸。
- 后端负责把 mask 安全 resize 回参考图尺寸，并做二值化兜底。

**建议**：需要先做一次真实设备性能采样（移动端 4K 原图 mask 编辑的 frame drop / OOM 率），再决定是否动。P2，依赖数据。

### 3.3 编辑器体验也是生成质量的一部分

目标项目的 MaskEditorModal 使用多 canvas 分层，维护 image canvas、preview canvas、mask canvas 和 cursor canvas，见 [/tmp/gpt_image_playground/src/components/MaskEditorModal.tsx](/tmp/gpt_image_playground/src/components/MaskEditorModal.tsx:112)。

交互上它做了：

- brush / eraser 双工具。
- undo / redo。
- 鼠标、触控、双指缩放。
- 视图 transform clamp，缩放范围 1 到 6，见 [/tmp/gpt_image_playground/src/lib/viewportTransform.ts](/tmp/gpt_image_playground/src/lib/viewportTransform.ts:24)。
- 移动端 comfortable initial transform，让图片在小屏上有可操作空间。
- 绘制时实时更新 preview。

**Lumen 现状**：MaskBoard 使用 react-konva，已有键盘快捷键、画笔预设、实时覆盖比例、错误重试、fit 容器等能力。

**建议**：补齐两个方向：

1. **添加 pinch zoom / pan**，尤其是移动端细节涂抹。这是真实可用性缺口，列为 P2。
2. 在 lightbox 层进入 inpaint 时，把当前 zoom 区域或图片焦点传给 inpaint modal，让用户从查看到编辑的视觉上下文连续。**可选 P3**。

### 3.4 inpaint prompt 与尺寸策略要绑定原图

**Lumen 现状（核实结果）**：

- inpaint 时会根据源图宽高推断 aspect ratio，避免默认 16:9 导致 mask 和输出构图错位。
- [InpaintModal.tsx:538-549](/Users/liangchanghua/Downloads/Image/apps/web/src/components/ui/inpaint/InpaintModal.tsx:538) 已在 prompt 输入区右上角展示比例胶囊，title 文案为 "按原图比例生成（避免构图变形）"。

**结论**：这点 Lumen 已经做了。不再列为待办。

## 4. 服务商配置和可调试性

### 4.1 自定义服务商 Manifest 很值得研究

目标项目的自定义 provider 设计像一个轻量 DSL，核心在 [/tmp/gpt_image_playground/src/lib/apiProfiles.ts](/tmp/gpt_image_playground/src/lib/apiProfiles.ts:214) 和 [/tmp/gpt_image_playground/src/components/SettingsModal.tsx](/tmp/gpt_image_playground/src/components/SettingsModal.tsx:90)。

一个自定义服务商可以描述：

- 文生图提交接口 `submit`。
- 图生图或局部修改接口 `editSubmit`。
- 异步任务查询接口 `poll`。
- 请求 method、query、body 模板、multipart 文件映射。
- 响应里的图片 URL 或 b64 路径。
- task id、status、success/failure values、error path。

它还内置了一段让 LLM 根据 API 文档生成 provider JSON 的提示词，见 [/tmp/gpt_image_playground/src/components/SettingsModal.tsx](/tmp/gpt_image_playground/src/components/SettingsModal.tsx:183)。

**Lumen 现状（核实结果）**：

- `apps/web/src/app/admin/_panels/ProvidersPanel.tsx` 已有完整 CRUD + Draft 编辑系统（Draft 类型、字段错误校验、API Key 保留、Proxy 关联）。
- `apps/api/app/routes/providers.py` 已提供 `GET/PUT /admin/providers`、`POST /admin/providers/probe`、`PATCH /admin/providers/{name}/enabled`，并有 `_probe_one()` / `_classify_probe_status()` 做探活分类。
- Probe 默认用 `gpt-5.4-mini` + 一个"99×99"提示词做端到端校验（见 providers.py:59-64）。

**结论**：playground 那套 manifest 之所以复杂，是因为它要在浏览器里同时适配 OpenAI / fal.ai / 各种异步 task 接口，把上游差异全部转成配置字段。Lumen 服务端已经把 OpenAI 兼容、Responses、Images、async task 这些封装在 Provider Pool 里，admin 接入新 provider 实际只需要填 `base_url` + `api_key` + `purposes`，其余字段都有合理默认值。

**不需要"LLM 辅助 Draft 生成"**：playground 的 LLM prompt 是为它那套 30+ 字段 manifest 服务的；Lumen 现有的两三个必填字段手填即可，引入 LLM 反而是给自己造问题（增加复核成本、不熟悉 Lumen 自定义概念如 `endpoint_kind_allowed`、`image_edit_input_transport`）。本节不再列待办，详见 §6 第 7 条。

### 4.2 URL 参数导入适合集成入口

目标项目支持 `?apiUrl=...&apiKey=...&apiMode=...&model=...&codexCli=true`，也支持 `?settings=...` 导入完整 provider/profile，见 [/tmp/gpt_image_playground/src/lib/urlSettings.ts](/tmp/gpt_image_playground/src/lib/urlSettings.ts:82)。

**Lumen 现状**：Lumen 是多账号 SaaS 形态，API Key 全部存服务端、用户走 BYOK。把 Key 放 URL 风险太高。

**建议**：不照搬。如果未来需要"外部系统集成入口"，可以做"管理员生成不含密钥的 provider preset link，用户点开后进入 BYOK 填 key"。**优先级 P3，按需触发**。

### 4.3 本地 mock API 是很实用的稳定性工具

目标项目的 [/tmp/gpt_image_playground/scripts/mock-image-api.mjs](/tmp/gpt_image_playground/scripts/mock-image-api.mjs:1) 模拟了多种故障：

- 返回 b64。
- 返回 URL 且 CORS 允许。
- 返回 URL 但 CORS 阻断。
- 图片 404。
- 重定向后 CORS 阻断。
- HTTP 500。
- invalid JSON。
- 响应结构不符合预期。
- 慢响应。

**Lumen 现状（核实结果）**：`apps/api/tests/` 下只有 `test_metrics_upstream.py` 之类的单元测试，**没有可启动的 mock image upstream，也没有针对图片链路的端到端故障注入工具**。这是 Lumen 当前最大的工程化缺口。

**建议**：新增一个独立可运行的"图片上游故障模拟器"，覆盖：

- OpenAI Images API（成功 / 401 / 429 / 500 / invalid JSON / 慢响应）。
- Responses image_generation（成功 / revised prompt / partial_image 推送）。
- 异步 task API（submit → poll → result，含中途失败和恢复）。
- 图片 URL 过期 / CORS 阻断 / 404。
- `actual_size` 缺失或异常。
- provider failover 中途成功。

接入 Playwright 或 API 测试套件，作为 release 前 image stability check 的硬门槛。**这是修订后的唯一 P0 大项，详见 5.2**。

## 5. 对 Lumen 的具体借鉴优先级（修订版）

> **优先级判断标准**：P0 = 真实生产风险或用户每天都会遇到的体验缺口；P1 = 明显体验提升或维护成本下降，但非阻塞；P2 = 增强或纯 UX；P3 = 待数据/调研触发再考虑。

### P0.1 — 结果可解释性微增量（三件套）

**目标**：让用户知道一次图片生成到底发生了什么。

**Lumen 已有**：

- 图片详情面板 [LightboxParamsPanel.tsx:37-160](/Users/liangchanghua/Downloads/Image/apps/web/src/components/ui/lightbox/LightboxParamsPanel.tsx:37) + `buildLightboxMetadataSections()` 已展示尺寸、比例、Seed、质量、模式、模型。
- 错误码映射表 `apps/web/src/lib/errors.ts`（network_error / upstream_timeout / quota_exceeded 等），`useChatStore.ts` 调用 `errorCodeToFullText()` 展示。

**待补的三件套**：

1. **请求参数 vs 实际生效参数 diff**：当系统因 provider capability 调整了 `quality`、`size`、`output_format`、`output_compression` 时，在元数据面板把差异以"请求 X → 实际 Y"形式显示，并加一个"已自动调整"小标签。
2. **revised prompt 回显**：上游返回 `revised_prompt` 时（OpenAI / 部分中转都会回写），在 prompt 区下方加一个折叠块"模型改写后的提示词"，并支持复制。
3. **provider / proxy / 耗时 / failover 痕迹**：在元数据面板增加一节"运行信息"，含 provider name、proxy 是否启用、首次尝试 / 实际成功的 provider、上游耗时、是否 failover、debug id。失败时显示安全摘要（不含 key）。

**实现路径**：

- 后端：扩展 `image_job` 持久化字段或 result envelope，把 effective_params、revised_prompt、provider_attempts、debug_id 透传给前端。
- 前端：扩展 `buildLightboxMetadataSections()` 增加"运行信息"和"参数差异"两个 section。

**参考 playground**：[imageApiShared.ts:165](/tmp/gpt_image_playground/src/lib/imageApiShared.ts:165)、[openaiCompatibleImageApi.ts:133](/tmp/gpt_image_playground/src/lib/openaiCompatibleImageApi.ts:133)。

### P0.2 — 图片上游故障模拟器

**目标**：让图片链路测试覆盖真实生产故障，把"上游不稳定导致的回归"变成可复现的 case。

**Lumen 现状**：完全空白。

**建议实现**：

- 在 `scripts/` 或 `tools/` 新建 `mock-image-upstream/`，作为独立 Node 或 Python 服务，端口可配置。
- 提供路由别名：`/scenario/{name}` 切换不同故障场景。
- 接入 pytest + httpx mocking，再加一个 Playwright e2e suite 覆盖前端 SSE 表现。
- 把 mock upstream 纳入 release checklist：每次 image 链路变更必须跑一遍。

**覆盖场景**（按优先级）：

- 必跑：成功 b64、成功 URL、401、429、500、invalid JSON、慢响应（>30s）、上游超时、prompt 被改写。
- 推荐：async task submit → poll → result 全链路、provider failover 中途成功、`actual_size` 缺失、CORS / 404 上游 URL。

**参考 playground**：[scripts/mock-image-api.mjs](/tmp/gpt_image_playground/scripts/mock-image-api.mjs:1)。

### P1 — 多参考图引用 token

**目标**：让用户能精确引用某张参考图，而不是只靠自然语言和附件顺序。

**Lumen 现状**：composer 区只支持附件列表 + 自然语言。普通参考图上限已从 4 张提到 16 张（对齐 OpenAI GPT image models 编辑输入上限）；inpaint 仍维持"单主图 + 单 mask"。

**前置调研**（建议做完再动手）：

- 拉一份 30 天数据，看每次会话平均参考图数量分布。如果 ≥4 张的占比 <10%，可降级为 P2。
- 用户访谈：现有用户是否真的会因为附件顺序混乱而出错。

**调研通过后的建议**：

- composer 支持插入"@图1 / @图2" token（结构化 mention，不用不可见字符，参考现代编辑器 mention 实现）。
- 附件重排、删除时自动 remap token；不存在的引用替换为"已移除"占位。
- 发送给后端时转为 `[image n]` 或结构化 prompt hint。
- 历史任务复用、再次编辑时保留引用关系。

**参考 playground**：[promptImageMentions.ts:86](/tmp/gpt_image_playground/src/lib/promptImageMentions.ts:86)。

### P2.1 — 图库批量操作扩展

**Lumen 现状**：[ConversationImageGallery.tsx:242-296](/Users/liangchanghua/Downloads/Image/apps/web/src/components/ui/chat/desktop/ConversationImageGallery.tsx:242) 已有多选 + 批量分享（createMultiShareMutation）。

**待补**：批量删除、批量收藏、批量加入项目、批量导出。桌面端可加框选 + 边缘自动滚动；移动端长按进入选择模式。

**参考 playground**：[TaskGrid.tsx:60](/tmp/gpt_image_playground/src/components/TaskGrid.tsx:60)。

### P2.2 — Inpaint 移动端 pinch zoom

**Lumen 现状**：MaskBoard 已有键盘和鼠标交互，但缺移动端双指缩放/平移。

**建议**：补 pinch zoom + pan，并在画板上保留 fit 按钮，避免缩放后迷路。属于真实可用性缺口。

### P3 — 不阻塞，按需触发

- **自由尺寸输入自动规整提示**（2.1）：开放自定义尺寸输入时再做。
- **inpaint ≥95% 是否升级为阻断式确认**（3.1）：UX 取向，需要产品决定。
- **lightbox 短生命周期原图 LRU**（2.6）：观察 display2048 拉取频率后再决定。
- **inpaint mask 工作图预处理层**（3.2）：先做移动端 4K 原图性能采样，必要再做。
- **lightbox → inpaint 视觉上下文继承**（3.3）：纯 UX 锦上添花。
- **管理员生成无密钥 provider preset link**（4.2）：等到有"外部系统集成"具体需求再考虑。
- **服务端历史任务导出包**（2.6）：等到客服/排查工具有具体诉求再考虑。

## 6. 不建议照搬的部分

1. 不建议把 IndexedDB 当作 Lumen 的主历史存储。Lumen 的账号、多端、分享、Telegram、管理后台都依赖服务端状态。
2. 不建议把 API Key 放在 URL query 中作为常规入口。Lumen 应以账号凭证、BYOK 或管理员 provider 配置为主。
3. 不建议把 provider 请求逻辑放到浏览器。Lumen 的代理、熔断、审计、计费和安全都应该继续在后端。
4. 不建议直接迁移 Vite 单页结构。Lumen 的 Next.js 16 结构和项目约束完全不同。
5. 不建议为了追求灵活，把任意自定义 HTTP provider 暴露给普通用户。这个能力应该是管理员工具。
6. 不建议把 inpaint 覆盖率检测改成 playground 那种"全图重绘强制阻断弹窗"。Lumen 当前"warning + 允许提交"是更好的取向；保留为 P3 待产品决定。
7. 不建议引入"LLM 辅助 Provider Draft 生成"。playground 的 manifest 是 30+ 字段的轻量 DSL，需要 LLM 才能填得动；Lumen 服务端已经把 OpenAI 兼容、Responses、Images、async task 全封装在 Provider Pool 里，admin 接入新 provider 实际只需要 `base_url` + `api_key` + `purposes`，其余字段都有合理默认值。加 LLM 反而引入复核成本，且 LLM 不熟悉 Lumen 自定义概念（`endpoint_kind_allowed`、`image_edit_input_transport`）。

## 7. 最推荐的落地路线（修订版）

### 第一阶段 — 生图可解释性 + 故障模拟（P0，1-2 个迭代）

**前端**：
- 图片详情新增"运行信息" section（provider/proxy/耗时/failover/debug id）。
- 增加"请求参数 vs 实际参数"diff，附"已自动调整"小标签。
- revised prompt 回显，可复制。
- 失败状态拆分为"模型失败 / 图片交付失败 / 上游结构异常"三类，对应不同文案和建议。

**后端**：
- `image_job` result envelope 扩展 effective_params、revised_prompt、provider_attempts、debug_id。
- 失败状态分类入库，前端无需重新推导。

**测试基建**（与前两条并行启动）：
- 新建 `tools/mock-image-upstream/`，覆盖必跑场景。
- 接入 pytest，作为图片链路 PR 的硬门槛。
- Playwright e2e 覆盖 SSE 表现。

### 第二阶段 — 多参考图精确控制（P1，依赖调研）

- 拉数据 + 用户访谈，决定是否启动。
- 启动后：composer 引用 token + 重排 remap + 后端 schema 透传 + 历史复用保留。

### 第三阶段 — 图库批量与移动端 inpaint（P2，跟随节奏穿插）

- ConversationImageGallery 批量删除/收藏。
- MaskBoard 移动端 pinch zoom。

### 第四阶段 — 长尾 P3

按真实数据触发，无固定排期。

## 8. 一句话结论

`gpt_image_playground` 最值得 Lumen 借鉴的不是某个 UI 样式，而是它把生图链路拆成了"参数规整、上游兼容、结果解释、历史复用、局部编辑安全"五个小闭环。Lumen 已经有更强的后端和任务系统，并且在 inpaint、admin provider、参数面板上已经走得比 playground 远；**下一步最划算的是补两件事：图片上游故障模拟器（填空白）+ 结果可解释性三件套（diff / revised prompt / 运行痕迹）**。多参考图引用 token 列为 P1 但需先验证用户真实需求；其余项按实际数据触发再做。

## 9. 修订记录

- **v1（2026-05-18 初版）**：完成 playground 全量分析，提出 P0/P1/P2 借鉴清单。
- **v2（2026-05-18 修订）**：对照 Lumen 当前代码核实，删除已完成项（inpaint 比例提示、覆盖率分类、alpha 二值化、admin provider CRUD/probe、图库多选）；把"上游故障模拟器"升为 P0；把"inpaint 覆盖率阻断弹窗"降为 P3；把"多参考图引用 token"加上调研前置；新增第 0 节修订说明、第 5 节优先级判断标准、第 9 节修订记录。
- **v2.1（2026-05-18 修订）**：删除"LLM 辅助 Provider Draft 生成"。原因：Lumen Provider Pool 已在服务端封装上游差异，admin 实际只需填 `base_url` + `api_key` + `purposes`，不需要 playground 那种为 30+ 字段 manifest 服务的 LLM 草稿。该条挪至 §6 "不建议照搬"第 7 条。原 §4.1 结论段、§5 P2.1 同步重写。
