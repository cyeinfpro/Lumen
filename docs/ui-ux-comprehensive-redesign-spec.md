---
status: proposal-and-spec
owner: web
created: 2026-09-03
title: Lumen Web UI/UX 综合设计重构与体验升级规范 (V2.0)
scope: apps/web 全栈可视界面、核心设计系统、交互动线与组件原语
references:
  - apps/web/DESIGN.md
  - apps/web/src/app/globals.css
  - docs/ui-optimization-review-2026-08-03.md
  - docs/ui-ux-redesign-plan-2026-08-04.md
  - docs/ui-ux-implementation-handbook-2026-08-04.md
---

# Lumen Web UI/UX 综合设计重构与体验升级规范 (V2.0)

> **核心定性**：
> 当前的 Lumen Web 并不是“缺少 CSS 变量”，而是**“拥有 80 分的设计规范体系，却呈现出 40 分的工业毛坯房质感”**。
> 本次代码与界面深度审计（结合实际渲染截图与源码调用链）表明：即使 8 月初引入了部分 Token 与基础原语，界面依然存在**大黄砖按钮暴力侵染、死黑平涂缺乏深度、骨架碎裂多重标题、原生裸控件穿帮、汉字排版散乱发虚、以及严重的填表式交互**。
> 本文档旨在提供一份从**审美哲学、材质光影、排版阶梯、控件规格到五大核心模块重构**的完整设计规范与落地方案。

---

## 一、 为什么“现在的感觉还是很难看”？（代码级深层病理审计）

在对本地运行的桌面端与移动端（暗色 / 浅色模式）进行全量像素级审查及代码通读后，梳理出以下 6 大核心病理：

### 1. 色彩灾难：大黄砖与高饱和暴力侵染
* **代码实锤**：
  * 全站共有 **105 处** 直接使用了 `bg-[var(--accent)]`。
  * `apps/web/src/components/ui/primitives/Button.tsx` 的 `primary` 变体写死为：
    `bg-[var(--accent)] text-[var(--accent-on)] hover:bg-[var(--accent-hover)] shadow-[var(--shadow-1)]`。
    其中 `--accent` 取值为 `#F2A93A`（极高饱和度的琥珀橙黄）。
  * 在 `apps/web/src/components/ui/Sidebar.tsx` 中，新建会话按钮是满宽的大黄条：
    `<Button variant="primary" fullWidth ...>新建会话</Button>`。
  * 在 `apps/web/src/components/ui/projects/ProjectFunctionHub.tsx` 中，开始项目直接手写 `<Link className="... bg-[var(--accent)] text-[var(--accent-on)] ...">开始服饰项目 →</Link>`。
* **视觉后果**：
  在幽暗克制的 Darkroom 界面中，整块纯黄大色块极其突兀、刺眼，像工业施工现场的安全警示牌，直接拉低了整个产品的科技感与艺术感。

### 2. 页面骨架与排版节奏崩塌：碎线与重复标题（以 `/projects` 为例）
* **代码实锤**：
  * 在 `ProjectFunctionHub.tsx` 中，垂直 300px 的视口内连续堆叠了：
    1. `<p className="type-page-kicker">项目工作台</p>`
    2. `<h1 className="type-page-title">继续最近项目，保持创作节奏</h1>`
    3. 移动端同屏并列 `<h1 className="type-page-title">项目工作台</h1>`
    4. `<RecentProjects />` 内部又自带 `最近项目` 标题
    5. `<section className="border-t ..."><h2 className="type-section-title">选择工作流</h2>`
  * 整个页面被多个 `border-t border-[var(--border)]` 贯穿屏幕的细灰横线切得支离破碎。
* **视觉后果**：
  信息缺少**卡片化（Container）的包裹感**，大量的尴尬留白让页面显得极度空旷与简陋；一个页面在几秒内给用户灌输 4 次“项目”词汇，视觉动线彻底割裂，宛如未完成的原型脚手架。

### 3. 材质缺乏“空气感与深度”（死黑与惨白）
* **暗色模式（Darkroom）**：
  底色是绝对死黑 `#09090B`，卡片是生硬平涂的暗灰 `#121318`。缺乏现代高端 SaaS（如 Linear, Raycast, Vercel）核心的**微妙微光（Subtle Gradient）、超细内发光边框（1px inner border glow `border-white/[0.08]`）、磨砂玻璃反射（Backdrop Blur）**，使得所有元素扁平死板。
* **浅色模式（Lightroom）**：
  背景与白色卡片之间灰度差异极小，边框透明度过低，导致卡片边缘消失，页面白茫茫一片，像样式丢失的文档。
* **首页 Hero 空状态（`Onboarding.tsx`）**：
  直接使用硬编码大字：`从一句话开始，<span className="text-[var(--accent)]">慢慢把画面定下来。</span>`，配合下方 4 个纯文字的灰框按钮，既没有 AI 生成工具的视觉冲击力（Feed/Gallery），又带有一股浓郁的过时宣传页味道。

### 4. 零件拼凑感与原生裸控件穿帮
* **裸露的系统控件**：
  在 `apps/web/src/app/video/video-workbench-controls.tsx` 中，直接使用未经样式的原生 `<select>` 标签。即使 `primitives/Select.tsx` 已经存在，全站仍有 **8 处裸 `<select>`** 和 **274 处裸 `<button>`**。
* **顶栏穿帮与混乱**：
  * **默认头像写个“账”字**：`DesktopAccountMenu.tsx` 中 `const label = meQuery.data?.name || meQuery.data?.email || "账户"; const avatar = label.slice(0, 1);`，在未登录状态下，右上角赫然显示一个灰圆圈里写着汉字“账”！
  * **顶栏双搜索放大镜**：`DesktopAssetStream.tsx` 在传给 `DesktopTopNav` 的 `right` prop 里放置了 `StreamToolbar`（自带搜索图标），而 `DesktopTopNav` 自身右侧又写死了全局命令面板搜索图标，导致右上角并排出现两个放大镜！
  * **异常状态条打穿界面**：`RuntimeResilienceStatus.tsx`（“会话验证暂不可用”）使用 `fixed right-3 top-...`，在移动端直接横向骑在顶部模式切换栏上，遮挡关键操作；在桌面端则硬生生挤在输入框右侧。

### 5. 中文排版水土不服（“显脏、显散”）
* **中文字符套用西文样式**：
  大量中文标签被无脑附加了 `font-mono`、`uppercase` 以及 `tracking-[0.16em~0.22em]`。中文汉字没有大小写概念，大字距直接将汉字的笔画与字间距撕裂，字体边缘发虚、显得脏乱不堪。
* **非标准字号泛滥**：
  代码中充斥着不在 14 档类型阶梯内的任意值（`text-[8px]`、`text-[10.5px]`、`text-[12.5px]`、`text-[28px]` 等），阶梯完全形同虚设。

### 6. UX 创作动线缺失（“填表感压垮创作灵感”）
* **AI 工具 ERP 化**：
  无论是服饰模特（Showcase）还是视频生成（Video Workbench），用户一进来面对的不是“惊艳的视觉样本与一键同款”，而是冗长的大文本框、步骤说明（1 上传、2 模特、3 生成...）和生硬的提交按钮。用户在创作前需要先像行政人员一样“填报表”，体验极其沉重。

---

## 二、 新一代视觉设计系统：Lumen Visual Design System 2.0

为彻底解决上述问题，必须重构材质、色彩、排版与控件四大支柱。

### 1. 色彩哲学：从“高饱和刷漆”到“暗夜微光点睛”

| 色彩角色 | 旧版错误做法 | V2.0 标准规范 | 语义与场景 |
|---|---|---|---|
| **品牌主色 (Brand Accent)** | 全大块高饱和 `#F2A93A` 纯色平涂填充 | **收敛明度 + 琥珀微光**<br>描边 `rgba(242,169,58,0.35)` + 悬浮光晕 `shadow-[0_0_16px_rgba(242,169,58,0.20)]` | 仅用于：激活态导航指示、输入框焦点环、运行状态脉冲、核心主操作悬浮光晕 |
| **主操作按钮 (Primary CTA)** | `bg-[var(--accent)]`（大黄砖） | **深色质感底 + 琥珀微光边框** 或 **渐变柔和琥珀**<br>`bg-gradient-to-b from-[#EAA336] to-[#D48B22]`，文字为深黑 `--accent-on` | 去除刺眼荧光感，强化高级珠宝般的温润质感 |
| **背景层级 (Background Elevation)** | 死黑 `#09090B` 到底 | **四阶微渐变层级**：<br>Base: `#08080A`<br>Surface: `#0F1015`<br>Raised: `#15171F`<br>Overlay: `#1B1E28` | 营造层次分明、深邃静谧的暗房空间感 |
| **状态色彩 (Semantic Tone)** | 随意混用黄色表达警示与普通提示 | **严格 5 槽归位**：<br>运行/焦点: Accent (琥珀)<br>成功: Success (翡翠绿 `#30A46C`)<br>警告/耗费: Warning (琥珀黄)<br>危险/超额: Danger (赤红 `#E5484D`)<br>中性提示: Info (冰蓝 `#3E9EFF`) | 严禁成功用黄色，严禁普通提示用刺眼警告色 |

### 2. 表面材质与空间深度（Surface Depth Formula）

告别“死黑平涂”，统一为卡片与面板注入质感：

```css
/* V2.0 现代深色高级卡片质感 (Surface-Card-2.0) */
.surface-card-v2 {
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.035) 0%, rgba(255, 255, 255, 0.01) 100%), var(--bg-1);
  border: 1px solid rgba(255, 255, 255, 0.07);
  box-shadow: 0 4px 20px -2px rgba(0, 0, 0, 0.4), inset 0 1px 0 0 rgba(255, 255, 255, 0.05);
  border-radius: var(--radius-card);
  transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
}

.surface-card-v2:hover {
  border-color: rgba(255, 255, 255, 0.14);
  box-shadow: 0 8px 30px -4px rgba(0, 0, 0, 0.6), inset 0 1px 0 0 rgba(255, 255, 255, 0.08);
  transform: translateY(-1px);
}

/* 浮动面板与顶栏 (Surface-Panel-V2) */
.surface-glass-v2 {
  background: rgba(13, 14, 17, 0.75);
  backdrop-filter: blur(16px) saturate(180%);
  -webkit-backdrop-filter: blur(16px) saturate(180%);
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
```

### 3. 中文 CJK 排版宪法

1. **彻底绝缘 Mono 与无意义大写**：
   - 严禁在中文元素上附加 `font-mono`、`uppercase`、`tracking-[0.1em~0.3em]`。
   - 中文标签（Tag / Eyebrow）统一使用 `font-sans`、`font-medium`、`tracking-normal`。
2. **严格收敛字号阶梯（消灭任意 px）**：
   - `type-display`: 28px / 1.2 / font-bold（仅用于极少的大标题）
   - `type-page-title`: 20px / 1.3 / font-semibold
   - `type-section-title`: 16px / 1.4 / font-semibold
   - `type-card-title`: 14px / 1.4 / font-medium
   - `type-body`: 14px / 1.5 / font-normal
   - `type-body-sm`: 13px / 1.5 / font-normal
   - `type-caption`: 12px / 1.4 / font-normal
   - 彻底废除 `text-[8px]`、`text-[10.5px]`、`text-[12.5px]`，最小可读字号下限为 **11px**。
3. **行高与避头尾**：
   - 中文长文本必须启用 `break-words text-pretty`，防止移动端出现突兀的单字换行。

### 4. 基础控件库升级与收敛规格

#### Button（按钮体系重塑）
* **Primary Variant（主按钮）**：
  改为高质感琥珀渐变 + 深黑文字，或者中性深色底 + 琥珀光晕边框：
  ```tsx
  // 推荐方案：温润优雅的微光主按钮
  className="bg-gradient-to-b from-[#F5B858] to-[#E0932A] text-[#0A0A0C] font-medium shadow-[0_1px_2px_rgba(0,0,0,0.3),0_0_12px_rgba(242,169,58,0.25)] hover:brightness-105 active:scale-[0.98]"
  ```
* **Secondary Variant（次级按钮）**：
  统一使用 `bg-white/[0.04] border border-white/[0.08] text-[var(--fg-0)] hover:bg-white/[0.08] hover:border-white/[0.15]`。
* **Ghost Variant（图标按钮）**：
  透明底，hover 时出现 `bg-white/[0.06]`，严禁 hover 变刺眼纯黄色。

#### Select / FormField（全面替换裸标签）
* 所有的下拉全部强制采用 `primitives/Select.tsx`，包装统一的深色背景、1px 细微光边框、自绘简洁的 14px Chevron 图标，彻底抹除操作系统原生的塑料感。

#### Avatar（用户头像彻底重构）
* 当没有用户真实头像时：
  * **禁止使用“账”字**！
  * 改为优雅的渐变单色中性圆（例如 `bg-gradient-to-br from-[#2A2B35] to-[#15161C]`），内嵌简洁的 User 图标或根据用户邮箱前缀提取的大写英文字母。

---

## 三、 五大核心业务模块 UI/UX 改造蓝图

### 模块 1：全局 Shell 与导航架构（DesktopTopNav, Sidebar, MobileBar）

#### 痛点诊断
* 顶栏在图库页出现“双搜索图标”；
* 侧边栏“新建会话”是大黄长条，压迫感极重；
* 移动端顶部标题栏与浮动状态条重叠遮挡；
* 账户菜单展开位置生硬。

#### 改造规格与代码重构方向
1. **顶栏去重与流线化（`DesktopTopNav.tsx`）**：
   * 将右侧的全局搜索图标与页面私有工具栏解耦。页面级搜索收纳在页面内容区，顶栏右侧严格保持标准三件套：`[⌘K 命令面板] -> [全局任务托盘 TaskIsland] -> [账户/设置 Avatar]`。
   * 顶栏整体赋予 `surface-glass-v2`（微光毛玻璃），高度固定为 52px，去除死板底色。
2. **侧边栏轻量化（`Sidebar.tsx`）**：
   * 将顶部「新建会话」从刺眼黄条改为：**细腻的磨砂灰底 + 琥珀色加号图标 + 微弱边框**：
     `bg-white/[0.04] border border-white/[0.08] text-[var(--fg-0)] hover:border-accent-border hover:bg-white/[0.07]`。
   * 会话列表项（`ConversationItem`）激活态：改掉大面积高亮，使用左侧 3px 琥珀竖条 + 微弱高亮背景 `bg-white/[0.04]`。
3. **异常状态条规范化（`RuntimeResilienceStatus.tsx`）**：
   * 从暴力悬浮的黄框，降级为桌面端右下角轻量浮动 Pill 徽章（`z-[var(--z-toast)]`），移动端收纳在网络状态指示点中，绝对不允许骑在可点击的 UI 上。

---

### 模块 2：Studio 创作主场与 Hero 区（首页对齐 Midjourney/Claude 级质感）

#### 痛点诊断
* 土味的“慢慢把画面定下来”居中大黄字；
* 4 个纯文字方框，毫无视觉启发力；
* 底部输入框在移动端被 TabBar 遮挡切角。

#### 改造规格与设计方案
1. **废黜居中文案，引入“灵感画廊（Inspiration Grid）”**：
   * 顶部保持轻量徽标：`Lumen Studio`。
   * 主标题改为克制大气的标语：「探索画面的无限可能」或直接进入创作状态。
   * 将枯燥的 4 个文字卡片，升级为 **4 幅精美高质量的预设缩略图卡片**：
     * 卡片包含高清视觉样图（电影级雨夜、3D极简海报、质感模特肖像、概念设定图）；
     * 鼠标悬浮在卡片上时，卡片微幅上浮并浮现「一键应用提示词」按钮；
     * 点击直接将精心调校的提示词注入输入框，大幅降低新用户的启动阻力。
2. **底部 Composer（输入胶囊）精致化**：
   * 材质升级：`bg-[#121318]/90 backdrop-blur-xl border border-white/[0.12] rounded-2xl shadow-[0_8px_32px_rgba(0,0,0,0.6)]`。
   * 模式切换（[对话] [生图]）采用精致的 Segmented Control，选中的滑块带平滑 spring 动画。
   * 发送按钮：仅在有输入内容时激活为琥珀微光，输入为空时保持沉静的失能中性态。

---

### 模块 3：Projects 项目中心（从“破旧表格”到“现代化工作流仪表盘”）

#### 痛点诊断
* 满屏贯穿细横线，重复 4 遍“项目”标题；
* “服饰模特图”等功能被拆成散漫的三列，右侧突兀地摆放巨大黄色按钮；
* “最近项目”在无数据时只有一句苍白的灰字。

#### 改造方案
1. **消灭多余标题与横线**：
   * 页面顶部只保留单一清晰的 Header：
     * 标题：`创作工作流`（或 `项目中心`）
     * 副标题：`选择适合的商业创作流，或继续未完成的工作`。
   * 彻底删除切碎页面的全屏 `<hr>` 分割线，改用**卡片容器网格（Card Grid）**组织信息。
2. **工作流卡片升级为“商业级功能矩阵（Feature Matrix Card）”**：
   * 采用 2 列或 3 列现代化卡片网格布局（每张卡片独立包裹）：
     * **卡片头部**：功能图标（带微光圆角方块）+ 标题（如「服饰模特图」）+ 状态徽章（「正式」/「自由」）。
     * **卡片中部**：一句精炼的产品价值描述 + **流程步骤胶囊链**（例如：`①上传原图` ➔ `②选择模特` ➔ `③智能交付`，采用小尺寸 Pill 链接）。
     * **卡片指标区**：紧凑的三列微标签：`输入: 1-3张` | `输出: 4K商用图` | `耗时: 3-5分`。
     * **卡片底部**：右下角放置尺寸克制、精致的「开启项目」与「历史记录」按钮。
3. **“最近项目”模块空状态优化**：
   * 无项目时：展示一个精致居中的微光卡片，带有优雅的灰度插画图标，提示「暂无进行中的项目，从下方选择一个工作流开始创作」。
   * 有项目时：展示水平滚动的项目卡片，带有生成图预览、完成百分比进度环与时间戳。

---

### 模块 4：Video 视频工作台（从“行政表单”到“专业级导演台”）

#### 痛点诊断
* 中间是一个空洞的大 `textarea`，下方堆积着像热搜词一样的药丸标签；
* 右侧参数面板充斥着未经封装的系统原生 `<select>`；
* “生成视频”在浅色下是土黄色大块，暗色下缺乏执行感。

#### 改造方案
1. **重构工作台双栏结构**：
   * **左侧/主创作区（Director Viewport）**：
     * 上方为**视频画布/预览区**：未生成时展示电影画幅（16:9）的暗色微光舞台，带有运镜动效示意图或最近生成成果循环预览；
     * 下方为**分镜描述与运镜控制器**：文本框采用代码编辑器质感；提示词标签（近景、推镜、跟拍等）按分类收纳为可折叠的“导演运镜库”。
   * **右侧/参数总控台（Inspector Panel）**：
     * 全面弃用原生 `<select>`，替换为 `primitives/Select.tsx`；
     * 分辨率与画面比例（16:9, 9:16, 1:1）改为**可视化图标比例选择器**（带有小方块示意图，而不是冷冰冰的文字下拉菜单）；
     * 预计预扣积分与计费做成紧凑的数字卡片，使用表格对齐数字字体（`font-mono tabular-nums`）；
     * 底部「生成视频」按钮：升级为带有微光和粒子感的 Master Action Button。

---

### 模块 5：Assets & Library 素材与模特库（沉浸式 Lightbox 与瀑布流）

#### 痛点诊断
* 列表骨架屏比例失调、色块过大；
* 缺少直观的大图 Hover 浮动操作；
* 模特库分类浏览像文件管理器，缺乏电商模特选角（Casting）的专业感。

#### 改造方案
1. **瀑布流与卡片悬浮（Card Hover Actions）**：
   * 图片卡片保持 8px 圆角与细腻阴影；
   * Hover 时图片微幅放大（`scale-[1.02]`），自底向上淡出半透明黑色渐变遮罩，露出快速操作栏：`[放大预览] [一键变同款] [下载] [删除]`；
   * 移除顶栏多余的搜索框，搜索与筛选条紧密结合在素材瀑布流的正上方。
2. **模特库（Model Library）选角体验**：
   * 顶部增加模特维度过滤栏（年龄段：儿童 / 青年 / 中年；风格：欧美商拍 / 东亚日常 / 极简国风等）；
   * 每个模特卡片展示精致的高清正面肖像，标明模特标签与已匹配生成套数，点击直接弹出「选定模特并开拍」向导。

---

### 模块 6：Agent 智能体工作台（多模态交互与执行透明度升级）

#### 痛点诊断
* **上下文栏（ContextBar）碎片化**：
  顶部一条塞满了标题、运行中 Spinner、网络状态 Radio、生图未就绪文字警告、设置齿轮，各状态胶囊高度、字体各不相同，缺乏统一对齐与视觉节奏。
* **空状态（EmptyState）极其单薄简陋**：
  中间一个生硬的圆圈 Bot 图标，下面一个简单的“Agent”大字，跟了 3 个长条文字药丸（“整理一个极简产品海报方向”等），完全无法体现 Agent 作为“多模态商业创意智能体”的强大能力（读文件、看图、联网、生图全流程）。
* **工具调用（ToolCall）像一条冰冷的死胶囊，黑盒无细节**：
  目前 `AgentToolCall.tsx` 只有 44px 高的一个灰条，仅显示“联网搜索 · 等待执行”或“文生图 · 1 个任务”。用户完全无法展开查看：**搜索了哪些关键词？读取了哪个文件？生图传递了哪些参数？执行耗时是多少？** 缺乏专业 AI Agent 必备的过程透明度与信任感。
* **输入区（Composer）结构容易失衡**：
  同时支持参考图托盘（`AgentAttachmentTray`）和文件托盘（`AgentFileTray`），当用户拖入较多文件或图片时，托盘从底部生硬向上顶起，输入区域高度剧烈跳动；底部的工具开关（联网搜索、生图开关、文件读取）只是一排低对比度的灰图标，开关激活状态极不明显。
* **消息气泡（Turn）冷淡干瘪**：
  用户消息仅在左侧有一条硬邦邦的灰线（`border-l border-[var(--border-strong)]`），在深色背景下与整个背景黏连在一起，没有现代会话界面清晰的容器感与视线流。

#### 改造方案
1. **上下文栏（ContextBar）一体化与呼吸微指示**：
   * 移除散乱的圆圈状态胶囊，整合为紧凑的高级状态条：
     * 左侧：会话标题（点击可原地重命名）+ 快捷收藏/分支图标；
     * 中部：智能体运行状态，采用**微光呼吸指示点（Pulsing Dot）**替代大面积警告色（绿色常亮为网络就绪，琥珀慢脉冲为思考中，柔和黄色为能力降级，带有轻量 Tooltip 说明）；
     * 右侧：集成参数快捷抽屉入口（模型、思考强度、生图配置）。
2. **可折叠展开的“智能体思考与工具链”（Step-by-step Tool Accordion）**：
   * 参考 Cursor / Claude Artifacts / Perplexity 的过程展示：
     * **收起态**：精致的轻量药丸卡片，带微光描边，左侧为动态步骤图标（如旋转微光环、绿色勾选），显示一句优雅的过程描述（如 `已检索 4 个相关网页 · 耗时 1.2s` 或 `正在根据参考图分析主体轮廓`）；
     * **展开态（点击展开）**：平滑手风琴动画展开，展示具体的检索关键词、引用的文件路径及代码片段、或传递给画图引擎的具体 Prompt 参数；
     * 极大提升专业感与可信赖度，彻底告别现在的黑盒灰条。
3. **富有启发性的 Agent 探索起点（Empty State 2.0）**：
   * 废弃干瘪的单一大图标；
   * 设计成 **3 大核心能力探索卡片**：
     * 🖼 **多模态视觉企划**：分析输入图片色彩构图，生成同风格系列商品图；
     * 🌐 **商业与竞品联网调研**：搜集流行视觉风格趋势，撰写营销脚本；
     * 📁 **设计素材批量分析**：读取本地提示词与设计文件，执行自动化整理；
   * 每张卡片包含示意微缩图与一键启动 Prompt，直观展示 Agent 与普通单轮生图的降维级差异。
4. **AgentComposer 2.0（更平滑的专业输入底座）**：
   * **附件托盘平滑过渡**：将参考图与文件托盘合并为统一的自适应媒体抽屉，带横向滑动与移除动效，高度变化增加平滑 CSS 动画，避免布局弹跳；
   * **功能开关状态显式化**：底部的「联网搜索 🌐」、「生图联动 🎨」、「代码/文件读取 📁」采用 Segmented Pill 状态：开启时带有淡琥珀色微光底（`bg-accent-soft text-accent border-accent-border`），关闭时为柔和中性灰，状态一目了然；
   * 支持多模态拖拽微反馈（Drag-over 时整框发光）。
5. **用户消息与产物气泡（Turn 2.0）容器化**：
   * 用户消息改为靠右或带圆角深灰微光容器（`bg-white/[0.04] border border-white/[0.08] rounded-2xl`），与 Assistant 的宽幅输出形成明确的视觉节奏与对话深度。

---

## 四、 实施路线图与验收命令（分阶段落地计划）

为确保改动安全、可控、不引起回归，将整个 UI 重构拆分为 4 张自包含工单（Work Order）：

### W1：核心设计底座与视觉刺眼项治理（Day 1）
* **任务清单**：
  1. 修改 `apps/web/src/components/ui/primitives/Button.tsx`：重构 `primary`、`secondary` 和 `glass` 的配色，废黜刺眼大黄砖；
  2. 修改 `apps/web/src/components/ui/Sidebar.tsx`：新建会话按钮质感化，会话高亮条轻量化；
  3. 修改 `apps/web/src/components/ui/shell/DesktopAccountMenu.tsx`：彻底删除默认“账”字头像，替换为质感 User 图标；
  4. 修复 `DesktopTopNav.tsx` 与 `DesktopAssetStream.tsx` 的“双搜索图标”冲突；
  5. 修复 `RuntimeResilienceStatus.tsx` 的布局打架与异常遮挡。
* **验收命令**：
  ```bash
  cd apps/web && npm run type-check && npm run lint
  node scripts/check-ui-governance.mjs
  ```

### W2：全局卡片质感与表单原语收敛（Day 2-3）
* **任务清单**：
  1. 在 `apps/web/src/app/globals.css` 中注入 `surface-card-v2` 和 `surface-glass-v2`；
  2. 清理 `apps/web/src/app/video/video-workbench-controls.tsx` 等文件中的裸 `<select>`，全面接入 `Select.tsx`；
  3. 全面清理中文字符上的 `font-mono`、`uppercase` 与非法 `tracking-widest`。
* **验收命令**：
  ```bash
  cd apps/web
  # 验证全站无裸 select（除 primitives 自身）
  ! git grep -n "<select" src/ | grep -v "primitives/Select"
  npm run build
  ```

### W3：首页 Studio 与 Projects 仪表盘重塑（Day 4-5）
* **任务清单**：
  1. 重构 `apps/web/src/components/Onboarding.tsx`：删除土味大黄标语，重构 4 组高质量灵感预设卡片；
  2. 重构 `apps/web/src/components/ui/projects/ProjectFunctionHub.tsx`：
     * 删除重复的 4 个标题与穿屏细线；
     * 将 5 大功能列表重构成现代模块化卡片网格（Feature Matrix Card）；
     * 优化“最近项目”的空态与卡片状态。
* **验收命令**：
  ```bash
  cd apps/web && npm run type-check
  npm run test:e2e
  ```

### W4：Video 工作台与素材库/模特库质感升级（Week 2 前期）
* **任务清单**：
  1. 重构 `app/video`：参数面板可视化（画幅比例、分辨率），运镜标签结构化；
  2. 优化 `assets` 与 `library`：瀑布流卡片 Hover 微光与悬浮操作栏；
  3. 移动端 Safe Area 与 BottomSheet 交互全量测试与手势优化。
* **验收命令**：
  ```bash
  node apps/web/scripts/verify-mobile-visual-cdp.mjs
  cd apps/web && npm run build
  ```

### W5：Agent 智能体工作台过程透明度与体验重塑（Week 2 后期）
* **任务清单**：
  1. 重构 `features/agent/ui/AgentToolCall.tsx`：实现手风琴折叠展开（Collapsible Accordion），透明化展示联网搜索关键词、文件路径与生图参数；
  2. 重构 `features/agent/ui/AgentConversation.tsx` 空状态：设计 3 大多模态探索卡片，带生动微缩图与一键启动；
  3. 重构 `features/agent/ui/AgentContextBar.tsx`：整合碎裂的药丸标签，引入微光呼吸指示灯；
  4. 优化 `features/agent/ui/AgentComposer.tsx`：平滑双托盘展开动效，显式化工具开关（联网/生图/文件）激活状态。
* **验收命令**：
  ```bash
  cd apps/web && npm run type-check && npm run test:e2e
  ```

---

## 五、 总结与结语

视觉上的“难看”，本质是**设计语义的脱节、控件品质的不达标，以及对 AI 创作场景理解的生硬搬运**。

只要按照本规范：
1. **拔掉“大黄砖”与裸露控件**；
2. **给暗房注入微妙的微光与空气深度**；
3. **把 ERP 填表重塑为可视化灵感工作台**；

Lumen Web 将能够从目前的“毛坯工程感”，蜕变为一个兼具**商业说服力、艺术质感与丝滑体验**的现代化顶级 AI 创作工作台。
