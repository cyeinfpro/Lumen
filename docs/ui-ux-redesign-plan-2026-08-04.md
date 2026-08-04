---
status: proposal
owner: web
created: 2026-08-04
scope: apps/web 全部页面 / 子页面 / 弹窗 的 UI/UX 视觉重构与体验优化
nature: 深度扫描 + 重构方案（不含代码改动）
related:
  - apps/web/DESIGN.md
  - apps/web/src/app/globals.css
  - docs/ui-optimization-review-2026-08-03.md
  - docs/frontend-theme-dialog-standards.md
  - docs/ui-ux-implementation-handbook-2026-08-04.md   # 给执行 AI 的施工手册（配套）
method: 8 个并行扫描代理逐文件通读 src 下全部可视 TSX（chat/canvas/projects/admin/settings/auth/shell/弹窗原语），叠加全站 token 采用度量化统计
---

# Lumen Web UI/UX 视觉重构与体验优化方案

> 一句话：**这套 UI 不是"配色丑"，而是"系统不一致"。** 设计系统写了 80 分，落地执行只有 40 分——token、原语、布局工具全都造好了，但 feature 开发时大量绕过，导致同一个按钮、同一档字号、同一种卡片、同一个弹窗在不同页面长得都不一样。用户感受到的"丑"，本质是这种**系统性的不统一**叠加**中文排版被当拉丁文处理**带来的"脏"和"糙"。

---

## 0. 30 秒读懂这份文档

| 你关心的 | 结论 |
|---|---|
| **为什么丑？** | 不是主题丑（暗色 + 琥珀是高级配色）。是 ① 设计系统被架空、② 中文排版失控、③ 同义控件 N 套、④ 层级（z/阴影/遮罩）失守、⑤ 桌面移动两张皮。见 §2。 |
| **要不要换皮？** | **不要。** 保留 Darkroom 暗色 + 琥珀品牌。问题不在视觉方向，在执行一致性。 |
| **怎么改？** | 三个字：**收敛**。补齐缺失原语 → 全站收敛到原语 → 加门禁防回归。见 §3。 |
| **先改哪？** | 先修 5 个"失效 token"硬 bug（P0），再补 Dialog/Switch/Select 等地基原语（P1）。见 §7 路线图。 |
| **覆盖面** | 8 大模块、34 个路由页、全部弹窗/灯箱/抽屉/浮层，逐文件扫描。见 §5 逐模块方案 + §9 索引。 |

### 与 2026-08-03 那份报告的分工

`docs/ui-optimization-review-2026-08-03.md` 聚焦**工程一致性**（原语采用率、模块拆分、loading 覆盖、copy.ts、治理门禁），并明确"不重做视觉"。**本文档聚焦"丑→美"**：视觉语言、排版、层级、密度、跨端一致性、逐页面/逐弹窗的具体改造。两者互补——工程债请参照旧文档的 P0–P2，视觉债按本文推进。凡涉及"架构迁移 / loading 补齐 / copy.ts 推广"，本文只给结论、细节引旧文档，不重复展开。

### 给执行 AI 的施工手册

要把本方案落地，**不要**直接把本文喂给执行 AI——它是"为什么"的诊断，不是"怎么改"的施工规格。执行请用配套的 `docs/ui-ux-implementation-handbook-2026-08-04.md`：审美宪法 + token 决策表 + 金色标准 + before→after 反模式 + 13 张自包含工单，按 `W0 → W1 → W2 → …` 分批喂养、逐张验收。

---

## 1. 总诊断：设计系统"名存实亡"

`DESIGN.md` 和 `globals.css` 提供了一套相当完整的设计语言：5 语义色槽、4 阶中性灰、5 档圆角、4 档阴影、14 档 `type-*` 排版、`surface-card/panel/dialog` 表面原语、`page-shell/page-frame/list-row/dialog-layout` 布局原语、完整的亮暗双主题与 safe-area/reduced-motion 适配。**底座是 80 分。**

但全站量化采用度揭示了执行的真实现状（对 `src/**/*.tsx` 非测试文件统计）：

| 设计系统提供了 | 实际采用 | 后果 |
|---|---|---|
| `surface-card/panel/dialog` 统一表面语法 | **仅 23 次 / 15 个文件** | 卡片、面板、弹窗基本都在手拼 `bg-… border-… rounded-…`，质感全站不统一 |
| `var(--space-1…20)` 间距阶梯 | **仅 4 次 / 4 个文件** | 间距全是 Tailwind 任意值和魔法数，疏密无律 |
| `dialog-header/body/footer` 弹窗布局原语 | **0 次采用** | 每个弹窗的标题区/内容区/操作区间距各写一套 |
| `type-*` 14 档排版 | 791 次，**但硬编码 `text-*`/`text-[Npx]` 592 次并存** | 排版两套并行，字号字重乱跳 |
| `<Button>` / `<IconButton>` 按钮原语 | 314 次，**但裸 `<button>` 379 次** | 一半以上按钮绕过原语，hover/disabled/焦点环/命中区各漂移 |
| `--z-*` 层级 token | 被 `z-30/40/50/60/61/95/1000` 硬编码**打穿** | 菜单与托盘同级、命令面板与灯箱撞层 |
| `--surface-scrim` 遮罩 token | 被 `bg-black/40/45/55/60/72/76` 各自为政 | 遮罩透明度/模糊全站 4+ 种配方 |
| `accent-soft/border` 语义槽 | 被 `bg-[var(--amber-400)]/N`、`rgba(242,169,58,N)` 字面量架空 | 品牌色的弱底/描边写法全站 20+ 处不统一 |

**对照：执行得好的部分。** 阴影 token 采用健康（`shadow-[` 共 226 次中 225 次是合法的 `shadow-[var(--shadow-*)]`，真正 inline 数值阴影仅 1 次）；原生 Tailwind 强调色 0 违规；`var(--fg-*)` 前景色用得很规范；ESLint 已挡住"原生色 / 违禁圆角 / 中性色"三类硬伤；UI 治理基线 findings 为空。**这说明：团队有能力守规范，缺的是"把高级原语真正用起来 + 对漏网之鱼加门禁"。**

---

## 2. "丑"的六大根源（跨模块共性）

下面每一条都在 ≥3 个模块里复现，是"丑"的结构性来源，而非个别页面的手滑。

### 2.1 排版失控：中文被当拉丁文排（最"显脏"的来源）

这是全站**对质感伤害最大**的一类问题。

- **中文被套 `font-mono + uppercase + tracking-[0.16em~0.22em]`。** 中文没有大小写，`uppercase` 完全无效；IBM Plex Mono 渲染 CJK 再叠加 0.2em 字距，汉字被**拉散、发虚、显脏**。`poster-styles` 模块系统性使用 **53 处**，`projects`（服饰/海报/模特库）的表单 label、状态徽章、空态大面积使用，`canvas`/`settings`/`invite` 也有。代表：`stages/ShowcaseSetupFields.tsx` 的 SelectField 中文 label、`library/ModelLibraryBrowserView.tsx` 的 Chip（"幼儿/温柔亲和"）、`poster-styles/PosterStyleBrowser.tsx` 全模块。
- **阶梯外的任意像素字号泛滥。** 实测出现 `text-[8px]`（附件角标，几乎不可读）、`text-[9px]`（移动参数标签）、`text-[10.5px]`、`text-[12.5px]`、`text-[17px]`、`text-[26px]`、`text-[40px]/[48px]`（404）等，**全部不在 14 档 type 阶梯内**。
- **`type-*` token 被任意 px 覆盖。** `type-metric text-[28px]`、`type-page-title md:text-[28px]`、`type-card-title md:text-[18px]` 等——既然用了 token 又在上面压裸值，阶梯形同虚设。
- **kicker/overline 被当装饰滥用。** 多个页面 overline 与主标题文字机械重复（"风格库/风格库"、"任务中心/任务中心"、"筛选/筛选"），`type-page-kicker`/`type-mono-meta` 这类为大写拉丁文设计的样式被直接套在中文标题上。

### 2.2 同义控件 N 套实现："一个产品，多套五官"

同一类基础控件被反复重造，是全站"不精致"的直观原因：

| 控件 | 套数 | 证据 |
|---|---|---|
| **开关 Switch** | **5+** | 原生 checkbox（Byok/视频总开关/记忆批量）/ 自绘滑块（旋钮白、`bg-0`、`fg-0` 三色，36/44/48px 三尺寸）/ 按钮式（providers）；记忆 `SettingToggle` 甚至无 `role="switch"` |
| **输入框 Input** | **5** | adminUi / settings / Telegram / Pricing / providers editor 各一套，`bg` 透明度与 focus 透明度（/20 /25 /30）互不相同 |
| **指标卡 MetricCard** | **7** | Billing / Health / providers / video / RequestEvents / settings×2，结构雷同但字号、圆角、边框全不统一 |
| **"新建会话"主 CTA** | **4** | 桌面反白、移动琥珀实底、空态琥珀+黑字、窄栏图标按钮（`Sidebar.tsx`/`MobileConversationDrawerView`/`...States`/`DesktopStudio`） |
| **"当前选中"态** | **5** | 琥珀下条 / 中性 pill / 琥珀左竖条 / 中性 surface / 琥珀 soft（`DesktopTopNav`/`MobileTabBar`/`ConversationItem`/`SettingsShell`×2） |
| **确认弹窗** | **4** | `ConfirmDialog` 原语 / `window.confirm` 原生框 / 行内两步确认 / 手写 modal，原生框与暗色 UI 严重脱节 |
| **用户头像** | **3** | `AccountSheet`/`DesktopMe`/`MobileMe` 圆/方、渐变段数、阴影、字号（含 inline `style={{fontSize:"24px"}}`）全不同 |
| **空状态** | **N** | `EmptyState` 原语 / `AdminFeedback.EmptyBlock` / 手写 hero / `border-dashed` 框，文案层级、圆角、按钮样式互不一致 |

### 2.3 色彩语义被架空

5 语义槽 + 中性灰的铁律，在"弱底/描边/on-color"这些边角被系统性绕过：

- **accent 槽被裸值架空。** `bg-[var(--amber-400)]/10`、`border-[var(--amber-400)]/60`、`bg-[rgba(242,169,58,0.15)]`、`border-[rgba(242,169,58,0.32)]` 等散布 chat、composer、projects、settings、poster-styles，**全站 20+ 处**，而 `accent-soft`/`accent-border` 语义槽闲置。最刺眼的是 `ConversationImageGalleryActions.tsx` 同一组件里 `tone="danger"` 走规范、`tone="accent"` 走裸 rgba 两套标准。
- **任意 alpha 无规律。** `/8 /10 /12 /15 /18 /25 /30 /32 /35 /40 /55 /72 /76 /88 /92 /96 /97` 等随处可见，同类"浮层底"alpha 从 55 跳到 97。
- **调色板外强调色入侵。** `admin/_panels/providers/model.ts` 的 `WEIGHT_COLORS` 硬编码 7 种彩虹 hex（靛/粉/青/橙/紫/青蓝/黄绿）画权重条，在琥珀单色后台里像一块补丁。
- **装饰性琥珀滥用。** 卡片 hover 变琥珀、区块图标染琥珀、404 大数字上琥珀、`ContextWindowMeter` 出现蓝→琥珀→蓝彩带——违反 DESIGN §8.1"琥珀只用于当前动作/焦点/运行态"。
- **`text-black`/`text-white` 顶替 on-color token。** accent/danger/success 实底上的文字大量写死 `text-black`/`text-white`，而非 `--accent-on`/`--danger-on`/`--success-on`，亮色/未来调色下会断。
- **媒体覆盖层不走 `--media-control-bg/fg`。** lightbox、share 画廊、poster、模特库大量手写 `bg-black/N`+`text-white/N`，仅 lightbox 一处就散落 `black/35/45/48/50/55/60/62/80/90`、`white/5~86` 共 **30+ 个一次性透明度值**，而现成的 media-control token（composer/canvas 已在用）被无视。

### 2.4 层级体系失守：z / 阴影 / 遮罩各自为政

- **z-index 被打穿。** `--z-base…--z-toast` 已定义，却被 `z-30/40/50/60/61/95` 硬编码绕过（菜单 50 与托盘 50 同级、命令面板 95 与灯箱 95 撞层），`DesktopConversationImageMenu` 甚至用 `z-[1000]` 裸魔法值。
- **阴影档位误用。** 菜单/Popover 错用 `shadow-3`（弹窗档）、弹窗错用 `shadow-2`（浮起档）；canvas 所有**静止节点**都挂 `shadow-2` 浮起档，整幅画布一片"浮起"、层级失真；个别地方还有违禁的 `shadow-2xl`/`shadow-lg`。
- **遮罩配方 4+ 种。** `bg-black/40/45/55/60/72/76` 配 `blur-[2px]/[3px]/sm/xl` 各种组合，多数绕过 `--surface-scrim`（且亮模式下这些固定黑遮罩过重，token 本是主题感知的）。

### 2.5 桌面 / 移动"两张皮"

`desktop/` 12 个文件 vs `mobile/` 21 个文件的双树实现是合理产品选择，但**共享原子太薄**，导致同一产品元素两端长得不一样：

- **核心对话元素分裂。** 同一用户消息：桌面左对齐 + 左竖 accent 线，移动右对齐 + 右侧悬浮琥珀条（还带 `left:-15px; top:25%; height:50%` 魔法值）；同一张生成图：桌面 `object-contain` 带框、移动 `object-cover` 无框。
- **功能不对称。** 桌面折叠 Composer 可切对话/生图模式，移动折叠态只有不可点的装饰标签；桌面有完整图片右键菜单，移动端下载/定位/单图再生无入口；附件用途角标桌面三色语义、移动统一深色条。
- **细节漂移。** meta 尾行字号桌面 11px / 移动 10px；附件计数桌面 `{n}x` / 移动 `{n}`；SceneDivider 桌面用 `type-mono-meta`、移动手拼 mono；回到底部按钮定位两套。
- **hover 才显的操作在触屏隐身。** providers 探活按钮、memory 作用域"改/删"、Admin 多处 `opacity-0 group-hover:opacity-100`，触屏上完全看不到入口。

### 2.6 微文案与真实 Bug

- **违禁词泛滥。** DESIGN §5.2 明令禁止的"**正在 / 尚未**"出现 12+ 处（正在压缩/正在思考/正在载入…/画布尚未就绪）；§5.3 禁止的"**请…**"前缀遍布登录/注册/上传/重试；按钮文案超 6 字上限（"创建项目并开始分析"）；"确定"误用（动词表是"确认"）。
- **中英混排失控。** 中文界面散落 `AI Reading / Enabled / Disabled / Reset / Download / free / pts / Developing / Output Setup`，像没翻译完；同一动作多词并存（再生一张/重新生成/重试、改名/重命名）。
- **尺寸/单位格式不一。** `1080 x 1920` / `1920x2560` / `3840x2160` 三种写法，规范要求 `1080 × 1920`；`token`/`tokens` 单复数两端不一。
- **5 个"失效 token"硬 bug（引用不存在的变量）：** `--amber-soft`（性别选中钮无填充）、`--ok`（已复制态靠裸 hex 兜底）、`--focus-ring`（附件钮焦点环透明不可见）、`--z-popover`（画布下拉/检查器浮层 z-index 失效）、`adaptive-material`（多个 shell 引用的 class 只在媒体查询里存在、从未定义基础样式，等于 no-op）。另：`drop-shadow-[var(--shadow-2)]` 把含 inset 的多段 box-shadow 塞进 drop-shadow 滤镜、语法非法；`min-h-11` 恒胜过 `h-7` 导致桌面记忆触发器被强制撑大到 44px。

---

## 3. 重构总策略：收敛，而非重画

**不换皮、不改品牌色、不动 Darkroom 方向。** 视觉问题 90% 来自"同义实现太多 + 高级原语没被采用"，所以策略是**收敛**：

```
第 1 步  补地基    把缺失的原语造出来（Dialog 基座 / Switch / Select / Badge /
                  Slider / Tabs / MediaControlButton / Avatar / MetricCard / StatusBadge）
第 2 步  全站收敛  把 N 套同义实现逐模块替换成原语，顺手修排版/色彩/层级
第 3 步  加门禁    扩展 ESLint + UI governance，让"新增裸 select/switch/第二套
                  弹窗/text-[Npx]/mono 中文"直接 lint 失败，防止回潮
```

**四条贯穿全程的原则：**

1. **先 token 后组件**：能靠改 token/原语解决的，不去改 N 个页面。
2. **收敛优先于新增**：每加一个原语，必须删掉对应的 N 个同义实现，否则雪上加霜。
3. **中文优先**：所有为大写拉丁文设计的样式（mono/uppercase/宽字距/kicker）默认不用于中文。
4. **门禁兜底**：没有 lint 挡住的规范等于没有规范，每条收敛都要配一条防回归规则。

---

## 4. 设计系统层：先补这些"地基"

> 这一层是杠杆——地基补好，后面逐模块替换才有落点。

### 4.1 补齐缺失的原语（`components/ui/primitives/`）

| 优先级 | 原语 | 解决的不一致 | 收敛目标 |
|---|---|---|---|
| ★★★ | **`Dialog` 基座** | 4 个弹窗各造轮子：遮罩 `black/55/60/72/76`、header/footer 间距各一套、有的无焦点陷阱/Esc/滚动锁。统一 `--surface-scrim` + `surface-dialog` + `dialog-layout` + `useModalLayer`（focus trap + Esc + 背景点击关闭 + 移动 `mobile-dialog-*`） | `ConfirmDialog`/`InpaintModal`/`SystemPromptManager`/`MemoryCapabilityModal`/`RenameDialog` 全部迁移 |
| ★★★ | **`Switch`** | 5+ 套开关（原生 checkbox / 滑块 / 按钮式），旋钮三色三尺寸、有的无 `role="switch"` | 全站唯一开关，统一尺寸/配色/`role="switch"`/`aria-checked`/44px 命中区 |
| ★★★ | **`Select`**（含统一样式 chevron） | 原生 `<select>` 散落 admin/providers/composer/storyboard，箭头样式靠 inline SVG data-uri 写死灰色、亮模式失效 | 统一盒式 `control-shell` 下拉 |
| ★★ | **`MediaControlButton`** | lightbox/share/poster/模特库 30+ 个一次性 `bg-black/N`/`text-white/N` | 统一走 `--media-control-bg/fg` |
| ★★ | **`Badge` / `StatusBadge`** | 状态徽章各处手搓（10/11px、padding 不一、彩色/纯灰/英文混排） | 统一语义色 + 中文 + 固定尺寸 |
| ★★ | **`MetricCard`** | 7 套指标卡 | 统一图标+label+`type-metric`+sub 结构 |
| ★★ | **`Avatar`** | 3 种头像（圆/方、渐变段数、阴影、字号） | 统一一款，尺寸分档 |
| ★ | **`Slider`** | inpaint/canvas 用原生 `input[type=range]` 只有 accent-color、轨道滑块浏览器默认 | 统一轨道/拇指视觉 |
| ★ | **`Tabs` / `Segmented`（桌面）** | TaskCenter 等手搓筛选器；`SegmentedControl` 只在 mobile 目录 | 桌面/移动共享分段控件 |
| ★ | **`Skeleton` 组合**（text/circle/card） | 骨架只有 block 单形态，各页自拼、圆角三种 | 提供组合骨架 |

### 4.2 修复失效 token 与硬 bug（P0 止血，半天工作量）

| 文件 | 问题 | 修法 |
|---|---|---|
| `globals.css` | 缺 `--z-popover`、`--focus-ring`、`--ok`、`--amber-soft`、`adaptive-material` 基础定义 | 要么补定义并归位到 token 表，要么把引用改成现有 token（推荐后者：分别改 `--z-dialog`、`--ring`、`--success`、`--accent-soft`、删除死 class） |
| `ConversationImageGalleryActions.tsx:122` | `bg-[var(--amber-soft)]` 失效 | 改 `bg-accent-soft` |
| `DesktopConversationTurns.tsx:208` | `text-[var(--ok,#30A46C)]` | 改 `text-success bg-success-soft` |
| `MobileComposerExpanded.tsx` | `focus-visible:ring-[var(--focus-ring)]` | 改 `focus-visible:shadow-[var(--ring)]` |
| `CanvasSelectionToolbar.tsx:306`、`CanvasWorkspace.tsx:608` | `z-[var(--z-popover)]` | 改 `z-[var(--z-dialog)]` |
| `DevelopingCard.tsx` | `drop-shadow-[var(--shadow-2)]` 非法 | 去掉或改 `filter`/静态阴影 |
| `ConversationMemoryButtonView.tsx` | `min-h-11` 恒胜过 `h-7` | 桌面紧凑态不套 `min-h-11`（命中区交给移动端媒体查询） |

### 4.3 中文排版修正（改 token 层，一次到位）

- **给 `type-overline`/`type-mono-meta`/`type-page-kicker` 加 CJK 判定**：`:lang(zh)` 下去掉 `uppercase`、收紧 `letter-spacing`（0.16em → 0.02em 以内）、改用 `font-body` 而非 mono。或在组件层约定"中文分组标签一律用 `type-caption`/新增 `type-group-label`（sans、非大写）"。
- **明确最小字号**：UI 文本不低于 10px（`type-overline`），杜绝 8/9px。
- **规则入 lint**：禁止 `font-mono ... uppercase` 组合、`text-[Npx]` 任意值（见 §8 门禁）。

### 4.4 强制高级原语被采用

- `surface-card/panel/dialog`、`dialog-header/body/footer`、`page-frame`/`--content-*`、`var(--space-*)` 目前几乎零采用——它们是"统一质感"的关键。配合 §8 门禁，把"手拼 `bg-… border-… rounded-…`"变成 lint 警告，引导到原语。

---

## 5. 逐模块重构方案（覆盖全部页面 / 子页面 / 弹窗）

> 每个模块：**覆盖范围 → 现状丑点（精炼）→ 改造清单（按优先级，带文件）**。
> 严重度标注：🔴 高 / 🟡 中 / ⚪ 低。

### 5.1 全局外壳与导航（App Bar / 侧栏 / 命令面板 / 任务托盘）

**覆盖**：`layout.tsx`、`LumenAppShell`、`shell/`（DesktopTopNav/DesktopStudio/MobileTopBar/MobileStudioTopBar/MobileTabBar/StudioContextBar/SettingsShell/MobileConversationDrawer 等）、`Sidebar.tsx` + `sidebar/`、`CommandPalette.tsx`、`GlobalTaskTray.tsx` + `tray/`、`brand/`。

**现状丑点**：主 CTA 4 套、选中态 5 套、两套移动会话抽屉并存、搜索框 3 种圆角、字体 token 被 `text-[10~18px]` 打穿、z-index/遮罩/侧栏宽度各自为政、头像 inline style。

**改造清单**：

| # | 动作 | 关键文件 | 严重度 |
|---|---|---|---|
| 1 | 统一"新建会话"主 CTA：全端 `bg-accent text-accent-on` 实底（对齐 §8.1），删除反白/琥珀+黑字/图标按钮等其余 3 套 | `Sidebar.tsx:185`、`MobileConversationDrawerView.tsx:74`、`...States.tsx:163`、`DesktopStudio.tsx:720` | 🔴 |
| 2 | 统一"当前选中"为 1 套规则：导航用 accent 下条/左条、列表用 `accent-soft`，删其余 4 种 | `DesktopTopNav.tsx:163`、`MobileTabBar.tsx:113`、`ConversationItem.tsx:112`、`SettingsShell.tsx` | 🔴 |
| 3 | 删除 `MobileConversationDrawer` 重复实现，收敛到 `Sidebar` 一套抽屉（统一宽度/遮罩/z/圆角） | `MobileConversationDrawer.tsx`、`Sidebar.tsx:435-458` | 🔴 |
| 4 | 字体硬编码 `text-[Npx]`/`text-sm/xs` 全收敛 `type-*`（含 `text-[12.5px]` 等非标值） | `DesktopAccountMenu`、`MobileMe`、`TaskCenter`、`CommandPalette`、`Sidebar` 等遍布 | 🔴 |
| 5 | z-index 全部回收 `--z-*` token；遮罩统一 `--surface-scrim`；侧栏宽度统一 `--sidebar-panel-w`（248px），删 288/320/360 | `ConversationItem.tsx:158`、`CommandPalette.tsx:558`、`GlobalTaskTray.tsx:146`、`MobileConversationDrawer.tsx:316` | 🔴 |
| 6 | 头像抽 `Avatar` 原语，删 inline `style={{fontSize:"24px"}}` | `DesktopMe.tsx:96`、`MobileMe.tsx:103`、`AccountSheet.tsx` | 🟡 |
| 7 | 搜索框圆角统一 `--radius-control`；空态图标容器统一一款 | `sidebar/SearchBox.tsx`、`MobileConversationDrawerView.tsx:93`、`DesktopMe.tsx:48` | 🟡 |
| 8 | 修复 `--z-popover`/`--focus-ring`/`adaptive-material` 失效引用 | 见 §4.2 | 🔴 |

### 5.2 Chat 主界面（对话画布 + Composer）——产品门面

**覆盖**：`page.tsx`→`ResponsiveStudio`；`chat/`（desktop/Mobile ConversationCanvas、Turns、SceneDivider、DevelopingCard、MobileEmptyStudio、ConversationImageGallery(Actions)、CompletionStatusLine、ContextWindowMeter、CompactionToast、ConversationMemoryButton(View)）；`composer/`（desktop/Mobile ComposerPill、Buttons、ExecutionControls、AdvancedSettings、AttachmentTray、Popover、shared/*、MaskCanvas）。

**现状丑点**：2 个失效样式 bug、8/9px 不可读文字、accent 裸 alpha 20+ 处、UserTurn/成图两端两张皮、焦点环 22 处手拼 + 阴影/z 错位、gallery 裸 button 成片、移动 composer 5 列 9px 拥挤。

**改造清单**：

| # | 动作 | 关键文件 | 严重度 |
|---|---|---|---|
| 1 | 修失效变量：`--amber-soft`/`--ok`/`--focus-ring` | `ConversationImageGalleryActions.tsx:122`、`DesktopConversationTurns.tsx:208`、`MobileComposerExpanded.tsx` | 🔴 |
| 2 | 最小字号 10px：消灭 `text-[8px]`（AttachmentRoleBadge）、`text-[9px]`（移动参数条）；附件角标放大重排 | `AttachmentRoleBadge.tsx:39`、`MobileComposerExecutionControls.tsx` | 🔴 |
| 3 | accent 裸 alpha/字面量全收敛 `accent-soft`/`accent-border`；同组件 danger/accent 双标统一 | `MobileComposerPill`、`CompletionStatusLine`、`ConversationImageGallery`、`ConversationImageGalleryActions.tsx:52` | 🔴 |
| 4 | 抽 `ConversationTurn`/`FinalImage` 共享原子：统一 UserTurn 对齐与装饰、统一成图 `object-fit` 与描边；删魔法值与腐烂注释 | `DesktopConversationTurns.tsx:237-282`、`MobileConversationCanvas.tsx:442-516` | 🔴 |
| 5 | 焦点环统一 `focus-visible:shadow-[var(--ring)]`，删 22 处 `focus-visible:ring-[var(--amber-400)]/60` 手拼 | 全 chat/composer 14 文件 | 🟡 |
| 6 | 浮层语法归位：菜单/Popover 用 `shadow-2`、弹窗用 `shadow-3`、z 收敛 `--z-*`（删 `z-[1000]`） | `DesktopConversationImageMenu.tsx`、`ConversationMemoryButtonView.tsx`、`MaskCanvas.tsx` | 🟡 |
| 7 | gallery/turns 裸 `<button>` 换 `Button`/`IconButton`；统一相邻按钮圆角 `--radius-control` | `ConversationImageGallery.tsx`、`ConversationImageGalleryActions.tsx`、`DesktopConversationTurns.tsx` | 🟡 |
| 8 | 移动生图参数条降密度：5 列 → 摘要层 + 设置层（对齐 §8.3 三层渐进披露），字号 ≥10px | `MobileComposerExecutionControls.tsx` | 🟡 |
| 9 | 状态语义分层：`CompletionStatusLine` 的 active(accent)/warn(warning) 区分；成本警告从 danger 改 warning 槽；图像卡圆角 `--radius-md`→`--radius-card` | `CompletionStatusLine.tsx`、`ExecutionSummaryBar.tsx`、`DesktopConversationTurns.tsx` | 🟡 |
| 10 | 微文案：清"正在/尚未/请…"，统一 `1080 × 1920`、`token` 单复数、推理档标签（摘要条 vs 设置项同名） | `ContextWindowMeter.tsx`、`DevelopingCard.tsx`、`executionSummary.ts` | ⚪ |

### 5.3 Canvas 画布编辑器

**覆盖**：`projects/canvas/`（列表、`[canvasId]` 编辑器、`new` 模板、loading）；`canvas/`（Workspace、TopBar、CommandMenu、ShortcutsDialog、VideoPreviewDialog、Viewport*、Controls、Overlays、nodes/*、Inspector*、mobile/CanvasMobileToolbar）。

**现状丑点**：`--z-popover` 失效（bug）、检查器两套开关同屏、`RenameDialog` 野生模态、`window.confirm`、静止节点全挂浮起阴影、空画布态自绘一套、Frame 与节点圆角不一、端口配色滥用琥珀。

**改造清单**：

| # | 动作 | 关键文件 | 严重度 |
|---|---|---|---|
| 1 | 修 `z-[var(--z-popover)]` → `--z-dialog`（或补 token），恢复下拉/检查器浮层层级 | `CanvasSelectionToolbar.tsx:306`、`CanvasWorkspace.tsx:608` | 🔴 |
| 2 | 检查器开关统一 `Switch` 原语（原生方框 checkbox 与滑块二选一，删另一套） | `CanvasInspectorFields.tsx:186`、`CanvasNodeConfigFields.tsx:170` | 🔴 |
| 3 | `RenameDialog` 迁移 `Dialog` 基座（补 Esc/焦点陷阱/滚动锁/背景点击/移动适配）；删除确认 `window.confirm` 换 `ConfirmDialog` | `CanvasProjectIndex.tsx:293,179` | 🔴 |
| 4 | 节点静态用 `shadow-1`、仅浮起/运行用 `shadow-2`/`shadow-amber`，消除"整幅画布一片浮起" | `nodes/CanvasNodesPresentation.tsx:78` | 🟡 |
| 5 | 空画布态用 `EmptyState` + `type-*` + `Button`，删 CSS module 硬编码 16/13px 与自绘按钮 | `CanvasViewportOverlays.tsx`、`canvas.module.css:87-136` | 🟡 |
| 6 | Frame 与普通节点统一圆角语言；端口按数据类型用语义色（非琥珀绑死图片/遮罩），区分 video/deliver | `nodes/CanvasNodes.tsx:167-229`、`canvas.module.css:149`、`CanvasNodePalette.tsx:35` | 🟡 |
| 7 | `RangeField`/滑杆换 `Slider` 原语；`CanvasTitleInput` 加可编辑提示（hover/focus 边框或 `control-shell`） | `CanvasNodeConfigFields.tsx:139`、`CanvasTopBar.tsx:204` | 🟡 |
| 8 | 列表 loading 骨架贴合"顶栏+卡片网格"真实布局；移动端补多选对齐/分布入口 | `projects/canvas/loading.tsx`、`CanvasWorkspace.tsx:511` | ⚪ |
| 9 | 弹窗进场动效归位（`AnimatePresence` 别用 `duration:0`，走 `--dur-dialog`）；节点三重状态信号降噪 | `CanvasCommandMenu.tsx:233`、`CanvasNodesPresentation.tsx` | ⚪ |

### 5.4 Projects 项目中心（服饰 / 海报 / 分镜 / 模特库）

**覆盖**：`projects/`（Hub、`new`、`[projectId]`、apparel-model-showcase、poster-design、storyboard、library）；`projects/`（components、stages、storyboard、library 子目录，62 个组件）。

**现状丑点**：**中文 label 被系统性套 mono+uppercase+宽字距（最显脏）**、apparel/poster（hairline editorial）与 storyboard（盒式卡片）两套设计语言并存、中英混排失控（AI Reading/Enabled/Download）、主 CTA 无圆角+`text-black`、约束面板倒原始 JSON、输入控件 3 形态、`/projects` 与 `/projects/new` 职责重叠。

**改造清单**：

| # | 动作 | 关键文件 | 严重度 |
|---|---|---|---|
| 1 | 中文 label/Chip/状态徽章全部去掉 `font-mono uppercase tracking-[0.2em]`，改 `type-overline`(CJK 修正后) 或 `type-caption` | `stages/ShowcaseSetupFields.tsx`、`library/ModelLibraryBrowserView.tsx`、`ModelLibraryGenerator.tsx`、`ProductAnalysisStageView.tsx` 等数十处 | 🔴 |
| 2 | 统一工作流表单语言为 1 套：盒式 `control-shell`（选定后把 hairline 下划线输入全迁移），storyboard 同步 | `stages/*`、`storyboard/StoryboardShared.tsx:100,124` | 🔴 |
| 3 | 清英文微文案（AI Reading/Enabled/Disabled/Reset/Download/free），走 `copy.ts`；同动作统一动词 | `stages/*`、`ModelSettingsStage.tsx`、`DeliveryStage.tsx` | 🔴 |
| 4 | 主 CTA 统一：`bg-accent text-accent-on` + `--radius-control`，删无圆角/`text-black`/胶囊混杂 | `ProjectFunctionHub.tsx:317`、`ProjectsIndex.tsx:206,190`、`ApparelWorkflowFormViews.tsx:189` | 🔴 |
| 5 | 约束面板结构化渲染（键值/列表/标签），不再 `jsonValue(...)` 倒原始对象 | `PosterConstraintPanel.tsx:60-72`、`ConstraintPanel.tsx:42-58` | 🔴 |
| 6 | 合并 `/projects`(Hub) 与 `/projects/new` 两个"选工作流"入口，统一版式 | `ProjectFunctionHub.tsx`、`projects/new/page.tsx` | 🟡 |
| 7 | 输入控件收敛（下划线/盒式/原生 select 三形态 → 1）；select chevron 用主题感知 SVG | `ApparelWorkflowFormViews.tsx:522`、`ShowcaseSetupFields.tsx`、`ModelSettingsStage.tsx` | 🟡 |
| 8 | 单卡动作按钮降噪（PosterRenderCard 5 钮 → 主+次+溢出菜单）；空态统一 `EmptyState`；图片圆角统一 | `PosterRenderCard.tsx:157-199`、`ImageGrid.tsx`、`CandidateCard.tsx` | 🟡 |
| 9 | 序号标签放弃 `mix-blend-difference`（浅色图发灰），改 `--media-control-bg` 底；`SaveCandidateDialog` 不再把表单塞 `ConfirmDialog` description | `ProjectsIndex.tsx:473`、`SaveCandidateDialog.tsx:190` | ⚪ |
| 10 | 微文案：清"正在/请…"，按钮 ≤6 字；弹窗圆角归位（dialog=12/sheet=16，别用 card=8） | `ApparelWorkflowFormViews.tsx:217`、`PosterStyleSelector.tsx` 等 | ⚪ |

### 5.5 Video 视频工作台

**覆盖**：`app/video/`（page、view、task、workbench、volcano-asset-manager 等约 1.8 万行）。

**现状丑点**：95+ 处硬编码 `text-sm/xs`、任意 alpha、手搓卡片（非 `surface-card`）、区块图标装饰性琥珀、架构异位（详见 2026-08-03 报告 §3.5）。

**改造清单**：

| # | 动作 | 关键文件 | 严重度 |
|---|---|---|---|
| 1 | 字号 `text-sm/xs font-*` 全收敛 `type-*`（95+ 处） | `video-page-view.tsx` 等 | 🔴 |
| 2 | 卡片/表面统一 `surface-card`；去装饰性琥珀（图标/hover） | `video-page-view.tsx:472,249,318` | 🟡 |
| 3 | 任意 alpha 归位 token；圆角统一 | `video-page-view.tsx:213,261,289` | 🟡 |
| 4 | 架构迁移 `app/video`→`components/ui/video` + 补 `loading.tsx`（**按 2026-08-03 报告 §3.5/§3.6 执行**，此处不重复） | `app/video/*` | 🟡 |

### 5.6 Admin 管理后台

**覆盖**：`admin/`（page、loading、_components、_panels 含 billing/providers/request-events/settings/users/video-providers/proxies/telegram/storage/backups/update 等）+ `components/admin/`。

**现状丑点**：`WEIGHT_COLORS` 彩虹条、`radius-dialog` 当通用卡片圆角（圆角无政府）、`window.confirm`+原生 checkbox 混入精致 UI、开关 5 种/输入框 5 种/指标卡 7 种、状态语义断裂（彩色徽章 vs 纯灰文字 vs 英文徽章）、密集超宽表格（min-w-1320）。

**改造清单**：

| # | 动作 | 关键文件 | 严重度 |
|---|---|---|---|
| 1 | `WEIGHT_COLORS` 彩虹条换 5 语义槽或单色相明度渐变 | `providers/model.ts:9-18`、`providers/views.tsx:157-217` | 🔴 |
| 2 | 圆角归位：卡片 `--radius-card`、容器 `--radius-panel`、弹窗 `--radius-dialog`（清掉卡片用 12px） | `ByokPanel.shared.tsx:86,133`、`RequestEventsPanel.tsx:148` 等十几个面板 | 🔴 |
| 3 | `window.confirm` 全换 `ConfirmDialog`；原生 checkbox 换 `Switch`（尤其"启用视频生成"总开关） | `RedemptionPanel.tsx:198-425`、`video-providers/ProviderEditorView.tsx:268` | 🔴 |
| 4 | 收敛 Switch/Input/MetricCard/StatusBadge 各 1 套（删 5 套开关、5 套输入框、7 套指标卡） | `settings/views-controls.tsx`、`AdminUpdatePanel.network.tsx`、`BillingPanelParts.tsx`、`providers/*` | 🔴 |
| 5 | 状态徽章统一：语义色 + 中文（清英文 valid/used/revoked、wallet/byok），纯灰文本列补徽章 | `InvitesPanel.tsx:604-631`、`PricingSections.tsx:236,366`、`UserDialogs.tsx:273-289` | 🟡 |
| 6 | 超宽可编辑表格降密度（行高/斑马/hover），移动端走 `data-stack-on-mobile`；单元格输入框统一 | `PricingSections.tsx:417,615`、`admin-mobile.module.css` | 🟡 |
| 7 | hover 才显的探活/操作改常显或溢出菜单（触屏可见）；主按钮统一 accent（删反白/success 混用） | `providers/card.tsx:156`、`UserDialogs.tsx:262`、`AdminUpdatePanel.status.tsx:366` | 🟡 |
| 8 | 空态/骨架统一原语（删 EmptyBlock 平行实现 → 封装 `EmptyState`/`ErrorState`/`Skeleton`） | `AdminFeedback.tsx`、`ByokPanel.suppliers.tsx:156` | ⚪ |
| 9 | 遮罩统一 `--surface-scrim`（删 black/55/60/65）；`divide-white/5` 改 `divide-[var(--border-subtle)]` | `TelegramPanel.tsx:428`、`BackupsPanel.tsx:343`、`RequestEventsPanel.tsx:257` | ⚪ |
| 10 | 辅助信息（时间戳/ID）少用失能色 `fg-3`，改 `fg-2` 保证可读；`text-black`→`--accent-on` | `RedemptionWalletViews.tsx`、`providers/editor.tsx:777`、`BillingPanel.tsx:71` | ⚪ |

### 5.7 Settings 设置中心 + 账户钱包

**覆盖**：`settings/`（api-key、memory 含 modal、privacy、prompts、providers、telegram、usage）、`me/`（wallet）、`components/ui/me/`。

**现状丑点**：**7 个页面用 5 种内容宽度、无一命中规范 1080px**、providers 页是"另一套语言的飞地"（嵌 admin 面板 + WEIGHT_COLORS）、记忆弹窗手写无焦点陷阱、开关 3 套、hover 才显按钮移动端隐身、头像 3 种、`bg-[var(--amber-400)]` 绕过 accent 槽。

**改造清单**：

| # | 动作 | 关键文件 | 严重度 |
|---|---|---|---|
| 1 | 内容宽度统一 `--content-settings`(1080px)，删 `max-w-3xl/4xl/6xl` 混用 | `SettingsShell.tsx` + 各设置页 | 🔴 |
| 2 | providers 页对齐主设计语言（随 §5.6 一并收敛：type-*/圆角/accent/WEIGHT_COLORS） | `settings/providers/page.tsx`、`admin/_panels/providers/*` | 🔴 |
| 3 | `MemoryCapabilityModal` 迁移 `Dialog` 基座（补焦点陷阱/Esc）；关闭按钮动词"确认"→"关闭" | `settings/memory/modal/MemoryCapabilityModal.tsx` | 🔴 |
| 4 | 统一 `Switch`：记忆 `SettingToggle` 补 `role="switch"`/`aria-checked`；行内操作（改/删/探活）常显不 hover 才显 | `MemoryOverviewSections.tsx:161`、`MemoryScopeSidebar.tsx:142`、`providers/card.tsx:156` | 🔴 |
| 5 | 头像统一 `Avatar` 原语；`bg-[var(--amber-400)]`/`text-black` 全收敛 `accent`/`--accent-on` | `AccountSheet.tsx`、`DesktopMe.tsx`、`MobileMe.tsx`、`AccountRow.tsx`、`ConversationRowMobile.tsx` | 🟡 |
| 6 | 表单控件统一 `control-shell` + `type-*`（高度/圆角/focus 一致）；api-key 表单补可见 label | `settings/api-key/page.tsx`、`settings/memory/*`、`me/wallet/*` | 🟡 |
| 7 | 桌面"返回我的"冗余导航收敛（壳层已有侧栏导航）；壳侧栏标题与页内标题去重 | `settings/*` 各页头部 | 🟡 |
| 8 | 记忆库原生 checkbox 主题化；select 下拉箭头去 inline 写死灰色；`type-metric` 不被任意 px 覆盖 | `MemoryLibrarySection.tsx`、`providers/views.tsx:266`、`settings/usage/*` | ⚪ |
| 9 | 微文案：清"正在退出…"；动词统一（改名/重命名二选一）；英文徽标中文化 | `AccountCenterMenu.tsx:152`、`ConversationList.tsx` | ⚪ |

### 5.8 认证 + 公开 / 分享 / 错误页

**覆盖**：`login`、`signup`、`reset-password(/[token])`、`invite/[token]`、`share/[token]`、`library`、`poster-styles`、`not-found`、`error`、`global-error`。

**现状丑点**：**`poster-styles` 整模块是脱离 DESIGN 的"拉丁 editorial 皮肤"**（mono+uppercase 中文 53 处、`N°01` 序号、10.5px 阶梯外字号）、分享成功 toast 用琥珀（应 success）、info 用裸黑白、认证/视频（盒式）与 poster-styles（下划线）两套控件语言、`global-error` 用另一套品牌（zinc 灰 + `#f5a623` + system-ui）、`adaptive-material` 死 class、kicker 与标题机械重复、邮箱 placeholder 3 种。

**改造清单**：

| # | 动作 | 关键文件 | 严重度 |
|---|---|---|---|
| 1 | `poster-styles` 全模块去 mono+uppercase，对齐主设计语言（type-*/语义色/圆角），删 `N°01`/零补全数字/10.5px | `poster-styles/*`（6 文件） | 🔴 |
| 2 | toast/notice 语义色修正：成功用 success、中性提示用 info，删裸黑白 `bg-black/0.68 text-white/0.86` | `ShareContentClient.tsx:363,261` | 🔴 |
| 3 | 统一控件语言：poster-styles 下划线输入 → `control-shell`（与认证页 auth-control 一族） | `PosterStyleGenerator.tsx:127-427`、`login/page.tsx:162` | 🔴 |
| 4 | `global-error` 对齐品牌 token（暖灰 `--fg-0`、`#F2A93A`、Geist），不用 zinc/`#f5a623`/system-ui | `global-error.tsx:70-165` | 🟡 |
| 5 | 修 `adaptive-material` 死 class（补基础定义或删除，玻璃效果显式写 `bg-…/96 backdrop-blur`） | `share/[token]/page.tsx:204`、多个 shell | 🟡 |
| 6 | kicker 不重复主标题（"风格库/风格库"），kicker 仅作层级标签或删 | `PosterStylePage.tsx:136`、`PosterStyleJobsPanel.tsx:67`、`PosterStyleBrowser.tsx:723` | 🟡 |
| 7 | 邮箱 placeholder 统一 `name@example.com`；标点统一全角；"显示密码"眼睛钮圆角统一 | `login/page.tsx:156,190`、`signup:442`、`reset:103` | ⚪ |
| 8 | 微文案：清"请…"前缀；按钮 ≤6 字（"创建账号并进入 Lumen"→"创建账号"）；认证页外壳结构统一 | `login/signup/reset/invite` 各页 | ⚪ |
| 9 | 分享网格 tile 阴影降档（`shadow-3`→`shadow-1/2`）、去 hover 琥珀光晕；媒体控件走 media-control token | `ShareContentClientGallery.tsx:63` | ⚪ |
| 10 | 错误/空状态卡圆角统一；404 大数字用 `type-display`（≤32px）且去琥珀装饰 | `not-found.tsx:14`、`invite/[token]/page.tsx:506` | ⚪ |

### 5.9 弹窗 / 灯箱 / 局部重绘体系（专项）

**覆盖**：`ConfirmDialog`、`InpaintModal(View)`、`SystemPromptManager(.Presentation)`、各 feature 弹窗、`lightbox/`（desktop/Mobile）、`inpaint/`（含 mask-board）、`BottomSheet`/`ActionSheet`、`Toast`、`Tooltip`。

**现状丑点**：**弹窗无 `Dialog` 基座、4 个弹窗各造轮子**（遮罩 55/60/72/76）、`dialog-header/body/footer` 零采用、媒体控制按钮无原语（lightbox 30+ 一次性透明度）、缺 Badge/Slider/Tabs、灯箱 6 个同权重按钮糊图 + 移动琥珀实底反客为主、违禁 `shadow-2xl`/`shadow-lg` 与"正在"泛滥。

**改造清单**：

| # | 动作 | 关键文件 | 严重度 |
|---|---|---|---|
| 1 | 建 `Dialog` 基座并迁移全部弹窗（统一遮罩 `--surface-scrim`、`surface-dialog`、focus trap、Esc、滚动锁、背景点击、移动 `mobile-dialog-*`） | `ConfirmDialog.tsx`、`InpaintModalView.tsx`、`SystemPromptManager.tsx`、`MemoryCapabilityModal.tsx`、`RenameDialog.tsx` | 🔴 |
| 2 | 弹窗强制 `dialog-header/body/footer` 布局（统一标题区/内容区/操作区间距），删手写 `p-5`/`px-4 py-3`/`px-5 py-4` | 同上 | 🔴 |
| 3 | 建 `MediaControlButton` 原语（`--media-control-bg/fg`），lightbox/share/poster 30+ 一次性透明度全收敛 | `lightbox/*`、`ShareContentClientGallery.tsx`、`poster-styles/*` | 🔴 |
| 4 | 灯箱操作区降权：6 个同权重按钮 → 主操作 + 溢出菜单，内容优先；移动端创作钮去琥珀实底（中性、仅当前动作用 accent） | `DesktopLightboxView.tsx:112-192`、`MobileLightboxView.tsx:587,749-793` | 🔴 |
| 5 | 补 `Badge`/`Slider`/`Tabs`；`Kbd` 统一（Inpaint 本地 Kbd 换原语）；`Spinner` 统一（删自绘 CSS 圆环） | `MaskBoardToolbar.tsx:181`、`InpaintModalView.tsx:431`、`LazyInpaintModal.tsx:16` | 🟡 |
| 6 | 灯箱加载失败态用 `ErrorState`（补重试）；移动底部操作区压缩（缩略图+主操作一层，其余入溢出） | `DesktopLightboxView.tsx:222-230`、`MobileLightboxView.tsx:229-237` | 🟡 |
| 7 | 违禁样式清理：`shadow-2xl`/`shadow-lg`→token、`rgba(242,169,58,…)`→accent 槽、"正在…"→"…中"、`z-[60]`(Tooltip) 归位 | `DesktopLightboxView.tsx:271`、`MobileLightboxNotice.tsx:33`、`mobile/Chip.tsx:37`、`Tooltip.tsx:141` | 🟡 |
| 8 | 弹窗圆角归位（桌面统一 `--radius-dialog`，`SystemPromptManager` 误用 sheet 16px 改回）；760px 高度魔数抽共享常量 | `SystemPromptManager.tsx:554`、`InpaintModalView.tsx:100` | ⚪ |
| 9 | `ErrorState` 减轻（去整卡 danger-soft+blur，与 EmptyState 同族轻量风）；`SegmentedControl` 补 tabpanel 关联或改 role | `ErrorState.tsx:43-47`、`mobile/SegmentedControl.tsx` | ⚪ |
| 10 | 死代码清理：`GlobalGsapMotion` 空壳、`Card` 的 `data-lumen-reveal` 无消费者——要么接线要么删净 | `motion/GlobalGsapMotion.tsx`、`Card.tsx:65` | ⚪ |

---

## 6. 三个横切专项（贯穿所有页面）

### 6.1 排版专项

| 动作 | 目标 |
|---|---|
| 消灭 `text-[Npx]` 任意值与 `text-sm/xs font-*` 硬编码 | 全收敛 14 档 `type-*`；`type-*` 上不再叠 px |
| 中文去 mono/uppercase/宽字距 | token 层 CJK 修正（§4.3）+ 逐模块替换（§5.2/5.4/5.8） |
| 最小字号 10px | 清 8/9px；高密度区（移动参数条、admin 表格）重排而非缩字 |
| `type-metric` 专用化 | 所有大数字指标统一 `type-metric`(22px tabular)，不被 `text-[28px]` 覆盖 |

### 6.2 色彩专项

| 动作 | 目标 |
|---|---|
| accent 裸 alpha/字面量清零 | 全走 `accent`/`accent-soft`/`accent-border` |
| on-color token 化 | 实底文字统一 `--accent-on/--danger-on/--success-on`，删 `text-black`/`text-white` |
| 语义色用对槽 | 成本警告→warning、成功→success、中性提示→info（现在大量错用 danger/accent） |
| 装饰性琥珀收敛 | 琥珀只给当前动作/焦点/运行态；卡片 hover、区块图标、404 数字去琥珀 |
| 媒体 chrome token 化 | 全走 `--media-control-bg/fg`，清 30+ 一次性透明度 |
| 调色板外颜色清零 | `WEIGHT_COLORS` 彩虹、节点颜色标签任意 hex 注入，收敛语义槽 |

### 6.3 微文案专项

| 动作 | 目标 |
|---|---|
| 清违禁词 | "正在/尚未"→"…中/未"，"请…"→直接句式，"确定"→"确认" |
| 按钮 ≤6 字、动词表内 | 复合动词"宾语 ≤2 字"；同动作全站一词 |
| 中英混排治理 | 状态/按钮/徽标中文化；仅专有名词保留英文 |
| 数字单位规范 | `1080 × 1920`（× 非 x）、`token` 单复数统一、英文数字间 1 空格 |
| 推广 `copy.ts` | 新代码强制 `copy.action/state/error.*`（细节见 2026-08-03 报告 §3.7） |

---

## 7. 实施路线图（可执行）

> 原则：每阶段独立可交付、可验证，避免大爆炸 PR。

### P0 — 止血（0.5–1 天，纯 bug 修复，零风险）

修 5 个失效 token 硬 bug + 非法语法（§4.2）：`--amber-soft`/`--ok`/`--focus-ring`/`--z-popover`/`adaptive-material`/`drop-shadow(var(--shadow-2))`/`min-h-11` 撑大桌面控件。**验收**：这些元素的实际渲染恢复设计意图（性别选中有填充、焦点环可见、画布浮层能压住、灯箱玻璃生效）。

### P1 — 地基（3–5 天）

1. 建 `Dialog` 基座 + 迁移 5 个手写弹窗（§5.9）。
2. 建 `Switch`/`Select`/`Badge`/`StatusBadge`/`MetricCard`/`Avatar`/`MediaControlButton`（§4.1）。
3. token 层中文排版修正（§4.3）。
4. **验收**：原语进 `primitives/index.ts`；5 个弹窗走基座；`dialog-header/body/footer` 开始被引用。

### P2 — 全站收敛（2–3 周，按模块逐个来）

按 §5 顺序推进，建议顺序：**外壳导航 → chat 门面 → admin → settings → projects → canvas → 认证/公开 → video**（先改用户最高频看到的）。每个模块：替换同义控件为原语 + 修排版/色彩/层级 + 清微文案。**验收**：该模块裸 button/裸 select/`text-[Npx]`/mono 中文/accent 裸 alpha 清零。

### P3 — 打磨（持续）

信息密度（灯箱/移动 composer/admin 表格降权）、空态/加载态统一原语、动效归位（`--dur-*`）、对比度（`--danger-fg`/`fg-2` 替代 `fg-3`）、跨端共享原子（`ConversationTurn`/`FinalImage`/`ComposerSendButton`）。**验收**：跨端同一元素视觉一致；亮模式 AA 达标。

### 门禁（与 P2 并行上线，防回潮）

扩展 `eslint.config.mjs` 与 `check-ui-governance.mjs`：

- 禁 `text-[Npx]` 任意值、`text-{size} font-{weight}` 组合（→ `type-*`）。
- 禁 `font-mono` 与 `uppercase` 组合用于非代码上下文（→ 中文排版）。
- 禁新增裸 `<select>`/`role="switch"`/`role="dialog"`（→ 走原语）。
- 禁 `bg-[var(--amber-400)]`/`rgba(242,169,58,…)` 字面量（→ accent 槽）。
- 禁 `z-[数字]` 裸值（→ `--z-*`）、禁 `bg-black/N` 非媒体/scrim 上下文（→ `--surface-scrim`/media token）。
- 违禁文案扫描："正在/尚未/请…/确定"。

---

## 8. 度量目标（改完用什么衡量）

| 指标 | 现状 | 目标 |
|---|---|---|
| `surface-*` 采用 | 23 次 / 15 文件 | 卡片/面板/弹窗 100% 走原语 |
| `var(--space-*)` 采用 | 4 次 / 4 文件 | 页面骨架间距 token 化 |
| `dialog-header/body/footer` | 0 次 | 全部弹窗采用 |
| 硬编码字号 `text-[Npx]`/`text-sm/xs` | 592 次 / 107 文件 | **0**（全 `type-*`） |
| 裸 `<button>` | 379 次 | 仅剩合理例外（拖拽把手/媒体覆盖），有 lint 豁免注释 |
| 同义控件 | 开关 5+/输入框 5/指标卡 7/CTA 4/选中 5/弹窗 4 | **各 1 套** |
| 失效 token 引用 | 5 处 | 0 |
| 中文 mono+uppercase | poster-styles 53 + projects 数十处 | 0 |
| 违禁文案（正在/请…/确定） | 12+ / 多 / 1 | 0 |
| `window.confirm` / 原生 checkbox | 多处 | 0 |

---

## 9. 附录：覆盖面与重点文件

### 9.1 已扫描覆盖（8 大模块，逐文件）

| 模块 | 路由/页面 | 弹窗/浮层 | 组件量级 |
|---|---|---|---|
| 外壳导航 | 全局 layout + Studio/Me 骨架 | 命令面板、任务托盘、移动抽屉 | shell 20 + sidebar + tray |
| Chat 门面 | `/`（对话+生图） | 右键菜单、mask、popover、BottomSheet | chat 17 + composer 18 |
| Canvas | `/projects/canvas(/new/[id])` | 命令菜单、快捷键、视频预览、重命名 | canvas 35 |
| Projects | `/projects` 全系 + 服饰/海报/分镜/模特库 | 约束抽屉、保存候选、风格选择、上传 | projects 62 |
| Video | `/video` | 素材管理、预览 | app/video ~18k 行 |
| Admin | `/admin` 14 tab | 各 panel 确认/编辑弹窗 | _panels 30+ |
| Settings/账户 | `/settings/*` 7 页 + `/me/wallet` | 记忆引导、退出、钱包 | settings + me |
| 认证/公开 | login/signup/reset/invite/share/library/poster-styles/404/error | 详情抽屉、编辑、筛选 sheet | poster-styles 6 + 各页 |

### 9.2 最该优先改的 Top 文件（丑点密集 / 用户高频）

1. `components/ui/chat/desktop/DesktopConversationTurns.tsx` — 对话主体，失效变量 + 两张皮 + 裸 button。
2. `components/ui/chat/desktop/ConversationImageGallery(Actions).tsx` — 失效变量 + 成片裸 button + accent 双标。
3. `components/ui/poster-styles/*`（6 文件）— 整套拉丁 editorial 皮肤，脱离 DESIGN。
4. `app/admin/_panels/providers/*` — WEIGHT_COLORS 彩虹 + 飞地语言 + 开关/输入框乱。
5. `components/ui/SystemPromptManager(.Presentation).tsx` — 弹窗圆角错 + 表单三半径 + 不用 Input 原语。
6. `components/ui/lightbox/DesktopLightboxView.tsx` / `MobileLightboxView.tsx` — 6 钮糊图 + 30+ 透明度 + 移动琥珀反客为主。
7. `components/ui/Sidebar.tsx` / `shell/MobileConversationDrawer.tsx` — CTA 4 套 + 重复抽屉。
8. `components/ui/projects/stages/*` + `library/ModelLibraryBrowserView.tsx` — 中文 mono+uppercase 重灾区。
9. `components/ui/canvas/nodes/CanvasNodesPresentation.tsx` — 全节点浮起阴影 + 状态信号冗余。
10. `components/ui/shell/SettingsShell.tsx` + 各设置页 — 5 种内容宽度。

### 9.3 可立即开工的第一张工单（建议）

> **工单：修复 5 个失效 token + 统一"新建会话"CTA**（P0 + 外壳导航第 1 项）。改动小、见效快、用户第一眼就能感知"变整齐了"，适合作为整个重构的启动。

---

## 修订记录

| 日期 | 说明 |
|---|---|
| 2026-08-04 | 初版：8 模块逐文件深度扫描 + 全站 token 采用度量化，聚焦"丑→美"的视觉/体验重构。与 2026-08-03 工程一致性报告互补。 |
