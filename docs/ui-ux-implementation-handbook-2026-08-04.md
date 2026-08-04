---
status: active-runbook
owner: web
created: 2026-08-04
audience: 执行重构的 AI（GPT 5.6 等），不是人类设计评审
purpose: 把"好看的 UI"操作化成可机械执行的规则/数值/正反例/验收命令，让执行者在不做审美判断的前提下产出一致、不丑的结果
paired_with:
  - docs/ui-ux-redesign-plan-2026-08-04.md   # 诊断与"为什么"，给人看
  - apps/web/DESIGN.md                        # 设计语言 SoT
  - apps/web/src/app/globals.css              # token 实现
how_to_feed: 每次喂「§0–§4 全局部分」+「一张工单」，改完跑验收，再下一张。不要一次给全部工单。
---

# Lumen Web UI/UX 重构施工手册（给执行 AI）

> **这份手册是写给"你"（执行重构的 AI）的施工指令，不是设计讨论。**
> 你的目标不是"设计出惊艳的界面"，而是**"严格按规格，把不一致的界面收敛成一致"**。一致性本身就会产生"好看"。

---

## §0 给执行者的工作纪律（先读，必须遵守）

1. **你不是设计师，是施工员。** 本手册已把所有审美判断提前做完了。你的任务是**照规格替换**，不是创作。任何"我觉得这样更好看"的念头都是错的——按手册来。
2. **一次只改一张工单。** 改完 → 跑该工单的验收命令 → 全绿 → 才允许碰下一张。禁止一次大改多个模块。
3. **能用 token 就不用裸值，能抄原语就不手写。** 这是本手册的第一原则。每次想写颜色/圆角/阴影/字号/间距/z-index 前，先查 §1 决策表和 §2 金色标准。
4. **宁删勿加。** 拿不准的装饰、边框、阴影、图标、文字，**先删掉**，不要加新的。信息层级靠删减建立，不靠堆料。
5. **规格之外，停下问。** 遇到本手册没覆盖的情况，**不要自由发挥**。提出 2 个候选方案 + 各一句话利弊，交给决策者选，再继续。
6. **不破坏功能。** 只改视觉（className / 结构 / 文案），不改数据流、状态逻辑、接口、路由。行为必须逐字节等价。
7. **改完必跑验收。** 没有验收命令确认通过的改动，视为没做。

---

## §1 设计 Token 决策表（什么情况用什么值，查表，禁止发明）

> 用法：写一个样式前，先在这里找到对应"场景"，用"✅ 用"列的值。"❌ 禁止"列是常见错误。

### 1.1 背景层级（`--bg-*`）

| 场景 | ✅ 用 | ❌ 禁止 |
|---|---|---|
| 页面最底层 | `bg-[var(--bg-0)]` | `bg-black`、`bg-neutral-900/950` |
| 卡片 / 面板主体 | `surface-card` 或 `bg-[var(--bg-1)]` | 手拼 `bg-+border+rounded` |
| 嵌套 / 下沉区域 | `bg-[var(--bg-2)]` | 任意透明度 `bg-[var(--bg-1)]/73` |
| 最浅浮起层 | `bg-[var(--bg-3)]` | — |
| 浮层玻璃（菜单/抽屉/弹窗） | `surface-panel` / `surface-dialog` | 手拼 `bg-…/95 backdrop-blur` |

### 1.2 前景 / 文字色（`--fg-*`）

| 场景 | ✅ 用 | ❌ 禁止 |
|---|---|---|
| 标题、最强文字 | `text-[var(--fg-0)]` | `text-white`、`text-neutral-100` |
| 正文 | `text-[var(--fg-1)]` | — |
| 辅助 / 说明 / 占位 | `text-[var(--fg-2)]` | — |
| 失能（仅真正 disabled） | `text-[var(--fg-3)]` | **禁止**用它做普通辅助文字（时间戳/ID 用 `fg-2`） |

### 1.3 边框

| 场景 | ✅ 用 |
|---|---|
| 默认描边 | `border-[var(--border)]` |
| 更弱的分隔线 | `border-[var(--border-subtle)]` |
| 强调 / hover | `border-[var(--border-strong)]` |

### 1.4 圆角（按对象类型，禁止跨档）

| 对象 | ✅ 用 | 值 |
|---|---|---|
| 按钮 / 输入框 / Tag / Chip | `rounded-[var(--radius-control)]` | 6px |
| 卡片 / 列表行 / 图片 | `rounded-[var(--radius-card)]` | 8px |
| 浮层 / Tooltip / Popover / Drawer | `rounded-[var(--radius-panel)]` | 10px |
| 弹窗 | `rounded-[var(--radius-dialog)]` | 12px |
| 移动 BottomSheet | `rounded-[var(--radius-sheet)]` | 16px |
| 圆钮 / 头像 / Pill | `rounded-full` | — |
| 任意值 | ❌ `rounded-[7px]`、`rounded-md/lg/xl`、卡片用 dialog | — |

### 1.5 阴影（表达层级，静止别给浮起）

| 场景 | ✅ 用 |
|---|---|
| 静态卡片 / 输入框 | `shadow-[var(--shadow-1)]` |
| 浮起 / hover / 菜单 / Tooltip / Drawer | `shadow-[var(--shadow-2)]` |
| 弹窗 / Toast | `shadow-[var(--shadow-3)]` |
| 品牌光晕（**仅**当前运行态/主 CTA，慎用） | `shadow-[var(--shadow-amber)]` |
| ❌ 禁止 | inline `shadow-[0_…]`、`shadow-2xl/xl/lg`、静止元素挂 shadow-2 |

### 1.6 语义色（5 槽，用对槽位）

| 场景 | ✅ 用 | 实底文字 | 弱底 | 描边 |
|---|---|---|---|---|
| 主 CTA / 当前选中 / 运行中 / 聚焦 | `accent`（琥珀） | `text-[var(--accent-on)]` | `bg-accent-soft` | `border-accent-border` |
| 删除 / 错误 / 超额 | `danger` | `text-[var(--danger-on)]` | `bg-danger-soft` | `border-danger-border` |
| 成功 / 已启用 / 正常 | `success` | `text-[var(--success-on)]` | `bg-success-soft` | `border-success-border` |
| 警告 / **成本** / 将过期 | `warning` | `text-[var(--warning-on)]` | `bg-warning-soft` | `border-warning-border` |
| 中性提示 / 可选信息 | `info` | `text-[var(--info-on)]` | `bg-info-soft` | `border-info-border` |

**铁律：**
- 实底按钮上的文字**只许**用 `--{色}-on`，**禁止** `text-black` / `text-white`。
- 弱底/描边**只许**用 `{色}-soft`/`{色}-border`，**禁止** `bg-[var(--amber-400)]/10`、`rgba(242,169,58,0.X)`、任意 `/N`。
- **琥珀只给"当前"**：主 CTA、当前选中、正在运行、聚焦态。图标、序号、大数字、普通 hover **一律中性**，不许染琥珀。
- 状态用对槽：成功别用琥珀（用 success）、成本警告别用红（用 warning）、中性提示别用琥珀（用 info）。

### 1.7 排版（只用 14 档 `type-*`）

| 场景 | ✅ 用 | 备注 |
|---|---|---|
| 路由页主标题 | `type-page-title` | 24px |
| 紧凑页主标题 | `type-page-title-sm` | 22px |
| 分区 / 组标题 | `type-section-title` | 20px |
| 卡片标题 | `type-card-title` | 16px |
| 正文 | `type-body` | 15px |
| 次级正文 / 设置项 detail | `type-body-sm` | 13px |
| 标签 / 辅助说明 | `type-caption` | 12px |
| 数字指标 | `type-metric` | 22px tabular |
| **中文**分组小标签 | `type-overline`（CJK 修正后）或 `type-caption` | 见 §1.8 |
| 纯拉丁/数字大写标签 | `type-mono-meta` | **仅限拉丁/数字** |
| 营销大标 | `type-display` / `type-display-lg` | 32/28px |

**禁止：** `text-[Npx]` 任意值、`text-sm/xs/base font-*` 组合、在 `type-*` 上再叠 `text-[其它px]`（如 `type-metric text-[28px]` ❌）。最小字号 **10px**。

### 1.8 中文排版铁律（CJK）

- **中文永远不套** `font-mono`、`uppercase`、`tracking-wider/widest`（>0.05em 字距）。这些只对纯拉丁/数字/代码有效。
- 中文"分组小标签 / eyebrow / 状态标签"：用 `type-caption`，或修正后的 `type-overline`（CJK 环境下已去 uppercase、收紧字距、改 sans）。**拿不准就用 `type-caption`。**
- 层级靠"字号 + 颜色阶（fg-0/1/2）"表达，不靠加粗堆叠。

### 1.9 间距（`--space-*`，相邻至少差一档）

| 场景 | ✅ 用 | 值 |
|---|---|---|
| 紧凑控件内部 | `var(--space-1)` / `--space-2` | 4 / 8px |
| 默认元素之间 | `var(--space-3)` / `--space-4` | 12 / 16px |
| 分区之间 | `var(--space-5)` / `--space-6` | 20 / 24px |
| 大区块之间 | `var(--space-8)` / `--space-10` | 32 / 40px |

禁止连续小差值（`gap-[11px]`、`p-[13px]`）。

### 1.10 z-index（只用 token）

| 场景 | ✅ 用 | 值 |
|---|---|---|
| 页面内容 | `z-[var(--z-base)]` | 0 |
| 顶栏 | `z-[var(--z-header)]` | 10 |
| 底部 TabBar | `z-[var(--z-tabbar)]` | 20 |
| Composer | `z-[var(--z-composer)]` | 40 |
| 任务托盘 / 菜单 | `z-[var(--z-tray)]` | 50 |
| 弹窗 | `z-[var(--z-dialog)]` | 90 |
| 灯箱 | `z-[var(--z-lightbox)]` | 95 |
| Toast | `z-[var(--z-toast)]` | 100 |
| ❌ 禁止 | `z-30/40/50/60/61`、`z-[95]`、`z-[1000]` 等裸值 | — |

### 1.11 遮罩与媒体控件

| 场景 | ✅ 用 | ❌ 禁止 |
|---|---|---|
| 模态遮罩 scrim | `bg-[var(--surface-scrim)]` | `bg-black/40/45/55/60/72/76` |
| 媒体（图片/灯箱）上的控件底 | `bg-[var(--media-control-bg)]` | 手写 `bg-black/N` |
| 媒体控件文字/图标 | `text-[var(--media-control-fg)]` | 手写 `text-white/N` |

### 1.12 内容宽度（页面骨架）

| 页面类型 | ✅ 用 |
|---|---|
| 对话 / Markdown 正文 | `page-frame` + `data-width="text"`（800px） |
| 登录/注册/创建表单 | `data-width="form"`（720px） |
| 设置页 | `data-width="settings"`（1080px） |
| 媒体结果 | `data-width="media"`（1160px） |
| 工作台 | `data-width="workbench"`（1440px） |

禁止 `max-w-3xl/4xl/6xl`、`max-w-[1320px]` 等任意宽度覆盖。

---

## §2 金色标准（照这些抄，禁止自己创作第 N+1 种）

> 改任何一类 UI 前，**先打开对应的金色标准文件，抄它的写法**。模仿 > 创作。

| 你要做的 UI | 照抄这个文件 | 抄它的什么 |
|---|---|---|
| 按钮 | `components/ui/primitives/Button.tsx` | variant×size、loading 换 Spinner、`aria-busy`、移动命中区 |
| 图标按钮 | `components/ui/primitives/IconButton.tsx` | 强制 `aria-label`、可包 Tooltip |
| 输入框 / 文本域 | `components/ui/primitives/Input.tsx` / `Textarea.tsx` + `control-shell` | label/hint/error 关联、`aria-invalid` |
| 空状态 | `components/ui/primitives/EmptyState.tsx` | 图标 + 标题 + 描述 + CTA 结构 |
| 错误状态 | `components/ui/primitives/ErrorState.tsx` | `role="alert"`、重试按钮 |
| 加载 | `components/ui/primitives/Skeleton.tsx` / `Spinner.tsx` | 不自绘 CSS 圆环 |
| Toast | `components/ui/primitives/Toast.tsx` | tone 语义、action 按钮 |
| 确认弹窗 | `components/ui/primitives/ConfirmDialog.tsx`（W1 后改用它底下的 `Dialog` 基座） | 遮罩、进出场、按钮区 |
| **任意弹窗基座** | 工单 W1 产出的 `components/ui/primitives/Dialog.tsx` | 所有弹窗唯一基座 |
| 设置行 / 表格 / 表单 | `app/admin/_panels/settings/views-*.tsx` | **全后台最贴近 DESIGN 的样板** |
| 状态徽章 / 信息条 | `components/ui/projects/storyboard/StoryboardShared.tsx` 的 `StatusPill`/`InfoLine`、`components/ui/projects/components/OnlineBanner.tsx` | 语义色 + `*-soft` + `*-border` + `*-fg` 的标准用法 |
| 卡片 / 面板 / 弹窗表面 | `globals.css` 的 `surface-card` / `surface-panel` / `surface-dialog` | 不手写 `bg+border+rounded+shadow` |

---

## §3 反模式速查（看到即改，附正确写法）

> 这是最高频的"丑→好"对照。施工时先全文检索左列模式，逐个替换为右列。

### 3.1 排版

| ❌ 看到就改 | ✅ 改成 |
|---|---|
| `text-[15px] font-medium` / `text-base font-semibold` | `type-card-title` |
| `text-sm` | `type-body-sm` |
| `text-xs` | `type-caption` |
| `text-[10px]` | `type-overline` |
| `text-[11px]` / `text-[12.5px]` / `text-[8px]` 等阶梯外值 | 就近的 `type-*` 档 |
| `type-metric text-[28px]`（token 上叠 px） | 只留 `type-metric` |
| 中文 + `font-mono uppercase tracking-widest` | `type-caption`（或 CJK 版 `type-overline`） |
| `text-lg font-semibold`（当大数字指标） | `type-metric` |

### 3.2 颜色

| ❌ 看到就改 | ✅ 改成 |
|---|---|
| `bg-[var(--amber-400)]/10 text-[var(--amber-400)]` | `bg-accent-soft text-accent` |
| `border-[var(--amber-400)]/40` | `border-accent-border` |
| `rgba(242,169,58,0.X)` 字面量 | `accent` / `accent-soft` / `accent-border` |
| accent 实底上 `text-black` | `text-[var(--accent-on)]` |
| 任何实色钮上 `text-white` | `text-[var(--{色}-on)]` |
| `bg-[var(--danger)]/30 bg-[var(--danger)]/10` | `border-danger-border bg-danger-soft` |
| `text-white` / `bg-black` / `bg-white`（非媒体、非 scrim） | `text-[var(--fg-*)]` / `bg-[var(--bg-*)]` |
| 成功提示用琥珀 / 成本警告用红 / 中性提示用琥珀 | success / warning / info 槽 |
| 7 色彩虹（如 `WEIGHT_COLORS`） | 5 语义槽 或 单色相明度渐变 |
| 图标/序号/大数字/hover 染琥珀 | 改中性 `fg-*`（琥珀只给"当前"） |

### 3.3 控件

| ❌ 看到就改 | ✅ 改成 |
|---|---|
| `window.confirm(...)` | `<ConfirmDialog>` |
| 原生 `<input type="checkbox">` 当开关 | `<Switch>`（W2 原语） |
| 原生 `<select>` | `<Select>`（W2 原语） |
| 原生 `<input type="range">` | `<Slider>`（W2 原语） |
| 裸 `<button className="…">` 做 CTA / 取消 / 筛选 | `<Button variant="…">` / `<IconButton>` |
| 手搓 pill 徽章 | `<Badge>` / `<StatusBadge>`（W2 原语） |
| 手搓指标卡 | `<MetricCard>`（W2 原语） |
| hover 才显 `opacity-0 group-hover:opacity-100`（移动端有实例） | 常显，或收进溢出菜单 |

### 3.4 表面 / 层级 / 结构

| ❌ 看到就改 | ✅ 改成 |
|---|---|
| 手拼 `rounded-… border bg-…/95 shadow-… backdrop-blur` 的弹窗 | `Dialog` 基座（W1）+ `dialog-header/body/footer` |
| 手拼卡片 `rounded-[var(--radius-card)] border bg-[var(--bg-1)]/60 …` | `surface-card` |
| `bg-black/55`（模态遮罩） | `bg-[var(--surface-scrim)]` |
| 媒体控件 `bg-black/45 text-white/80` | `bg-[var(--media-control-bg)] text-[var(--media-control-fg)]` |
| 菜单/Popover 用 `shadow-[var(--shadow-3)]` | `shadow-[var(--shadow-2)]` |
| 弹窗用 `shadow-[var(--shadow-2)]` | `shadow-[var(--shadow-3)]` |
| 卡片用 `rounded-[var(--radius-dialog)]`（12px） | `rounded-[var(--radius-card)]`（8px） |
| `z-[1000]` / `z-50` / `z-[95]` 裸值 | 查 §1.10 的 `--z-*` |
| 设置页 `max-w-3xl/4xl/6xl` | `page-frame` + `data-width="settings"` |

### 3.5 文案

| ❌ 看到就改 | ✅ 改成 |
|---|---|
| `正在…` / `尚未` | `…中` / `未`（例：正在加载→加载中） |
| `请…` 前缀 | 直接句式（请检查网络→网络异常） |
| 按钮 `确定` | `确认` |
| 按钮超过 6 字 | 删到 ≤6 字（创建项目并开始分析→创建分析） |
| `1080 x 1920` / `1920x2560` | `1080 × 1920`（×，前后空格） |
| 中文界面英文词（Enabled/Reset/Download/AI Reading） | 中文（已启用/重置/下载/读取中） |

---

## §4 通用验收（每张工单改完必跑）

在 `apps/web/` 下执行，**全绿才算完成**：

```bash
# 1. 类型与 lint（必须过）
npm run type-check
npm run lint

# 2. 排版：本工单范围内不许有任意字号 / 中文 mono 大写
rg -n 'text-\[\d+(\.\d+)?px\]' <改动目录>            # 期望 0
rg -n 'font-mono.*uppercase' <改动目录>          # 中文场景期望 0

# 3. 颜色：不许有琥珀裸值 / 黑白裸值（非媒体）
rg -n 'amber-400\]|rgba\(242,169,58' <改动目录>      # 期望 0
rg -n '\b(text-white|bg-black|bg-white)\b' <改动目录> # 非媒体期望 0

# 4. 层级：不许有裸 z / 裸阴影数值
rg -n 'z-\[\d+\]|z-\d{2,}\b' <改动目录>              # 期望 0（z-0/1 除外）
rg -n 'shadow-(2xl|xl|lg|\[0_)' <改动目录>           # 期望 0

# 5. 文案：违禁词
rg -n '正在|尚未|确定' <改动目录>                    # 期望 0（copy 定义文件除外）
```

**视觉自检（逐条确认）：**
- [ ] 本工单范围没有任何裸值（颜色/圆角/阴影/字号/间距/z）绕过 §1 token。
- [ ] 每类元素都和 §2 金色标准长得一样。
- [ ] 琥珀只出现在：主 CTA / 当前选中 / 正在运行 / 聚焦态。
- [ ] 移动端所有操作可见（无 hover-only）。
- [ ] 中文无 mono / uppercase / 宽字距。
- [ ] 弹窗都走 `Dialog` 基座。
- [ ] 行为与改前一致（功能没动）。

---

# 工单集（§5 起，分批喂，每次一张）

> **喂法**：把「§0–§4」+「下面某一张工单」一起贴给执行 AI。工单之间有依赖（W1/W2 是地基，必须最先做）。
> **顺序**：`W0 → W1 → W2 →（之后 W3–W10 可任意顺序）→ W11 → W12`。

---

## 工单 W0 · 修复失效 token 硬 bug（P0，先做，零风险）

**目标**：修掉引用了"不存在变量"的样式，让这些元素恢复设计意图。
**范围**：散点，见下。
**施工**（逐条 before→after）：

| 文件 | Before | After |
|---|---|---|
| `components/ui/chat/desktop/ConversationImageGalleryActions.tsx:122` | `bg-[var(--amber-soft)]` | `bg-accent-soft` |
| `components/ui/chat/desktop/DesktopConversationTurns.tsx:208` | `text-[var(--ok,#30A46C)] bg-[var(--ok,#30A46C)]/8` | `text-success bg-success-soft` |
| `components/ui/composer/mobile/MobileComposerExpanded.tsx` | `focus-visible:ring-[var(--focus-ring)]` | `focus-visible:shadow-[var(--ring)]` |
| `components/ui/canvas/CanvasSelectionToolbar.tsx:306`、`CanvasWorkspace.tsx:608` | `z-[var(--z-popover)]` | `z-[var(--z-dialog)]` |
| `components/ui/chat/mobile/DevelopingCard.tsx` | `drop-shadow-[var(--shadow-2)]` | 删除该 class（或改 `shadow-[var(--shadow-2)]`） |
| `components/ui/chat/ConversationMemoryButtonView.tsx` | `min-h-11 min-w-11` 与 `h-7` 同时写 | 桌面紧凑态去掉 `min-h-11/min-w-11`（命中区交给移动端媒体查询） |
| 引用 `adaptive-material` 的 shell 组件 | 该 class 无基础定义（no-op） | 删除该 class，玻璃效果显式写 `bg-[var(--bg-0)]/96 backdrop-blur-xl` |

**验收**：
```bash
cd apps/web
rg -n 'amber-soft|--ok|--focus-ring|--z-popover|adaptive-material' src   # 期望 0
npm run type-check
```
**完成定义**：性别选中钮有填充、已复制态用 success、焦点环可见、画布浮层能压住内容、灯箱玻璃生效。

---

## 工单 W1 · 建 `Dialog` 基座并迁移全部手写弹窗（地基，最高优先）

**目标**：全站弹窗只有一个基座，统一遮罩/圆角/阴影/间距/焦点管理。
**范围**：新建 `components/ui/primitives/Dialog.tsx`；迁移 `ConfirmDialog.tsx`、`inpaint/InpaintModalView.tsx`、`SystemPromptManager.tsx`、`settings/memory/modal/MemoryCapabilityModal.tsx`、`canvas/CanvasProjectIndex.tsx` 的 `RenameDialog`。
**先做**：读金色标准 `ConfirmDialog.tsx` 和 `globals.css` 的 `surface-dialog`、`dialog-header/body/footer`、`mobile-dialog-*`。

**施工**：
1. **新建 `Dialog` 基座**，必须内置：
   - 遮罩 `bg-[var(--surface-scrim)]`（禁止 `bg-black/N`）。
   - 面板 `surface-dialog` + `dialog-layout`，桌面 `rounded-[var(--radius-dialog)]`、移动 BottomSheet `rounded-[var(--radius-sheet)]` + `mobile-dialog-shell/panel/scroll/footer`。
   - `useModalLayer`（focus trap + 层级隔离）+ Esc 关闭 + 背景点击关闭 + body 滚动锁。
   - 进出场动效 `framer-motion`，时长 `var(--dur-dialog)`、缓动 `var(--ease-develop)`（禁止 `duration:0`）。
   - 子组件 `Dialog.Header/Body/Footer` 分别套 `dialog-header/dialog-body/dialog-footer`。
2. **逐个迁移** 5 个弹窗到基座，删掉各自手写的 `fixed inset-0 + bg-black/N + z-[…] + 手写 header/footer`。
3. 典型 before→after：
   - `InpaintModalView.tsx` 的 `bg-black/76` → 删，由基座出 `--surface-scrim`。
   - `MemoryCapabilityModal.tsx` 的手写 scrim+面板（无焦点陷阱）→ 换 `<Dialog>`；关闭按钮文案 `确定`→`关闭`。
   - `SystemPromptManager.tsx:554` 桌面 `rounded-[var(--radius-sheet)]` → 由基座出 `--radius-dialog`。

**验收**：
```bash
cd apps/web
rg -n 'bg-black/(55|60|65|72|76)' src/components/ui/**/InpaintModal* src/components/ui/SystemPromptManager.tsx src/components/ui/primitives/ConfirmDialog.tsx  # 期望 0
rg -n 'dialog-(header|body|footer)' src/components/ui   # 期望 >0（开始被引用）
npm run type-check && npm run lint
```
**完成定义**：5 个弹窗全部走 `Dialog`；遮罩/圆角/阴影/间距全站一致；每个弹窗可 Esc、可焦点陷阱、移动端安全区正确。

---

## 工单 W2 · 建基础原语（Switch/Select/Badge/StatusBadge/MetricCard/Avatar/MediaControlButton/Slider）

**目标**：把全站 N 套同义控件收敛为各 1 套。
**范围**：新建到 `components/ui/primitives/`（含 `index.ts` 导出）。
**先做**：读 `Button.tsx`/`Input.tsx`（学原语写法）+ §1 决策表。

**施工**（每个原语的硬性规格）：
1. **`Switch`**：轨道 `rounded-full`、关态 `bg-[var(--bg-3)]`、开态 `bg-accent`；滑块 `rounded-full bg-[var(--fg-0)]`；`role="switch"` + `aria-checked`；移动 `min-h-11 min-w-11` 命中区。**替换**：admin 5 套开关、记忆 `SettingToggle`、canvas 检查器两套、providers 按钮式、原生 checkbox。
2. **`Select`**：盒式 `control-shell`，chevron 用主题感知 SVG（`stroke="currentColor"`，禁止写死 `#999`/`#666`），option 底 `bg-[var(--bg-1)]`。**替换**：admin/providers/composer/storyboard/settings 的原生 `<select>`。
3. **`Badge` / `StatusBadge`**：`rounded-full border px-2 py-0.5 type-caption`，语义色走 `bg-{色}-soft text-{色} border-{色}-border`；状态文案中文。**替换**：各处手搓 10/11px 徽章、英文状态徽章。
4. **`MetricCard`**：图标 + `type-caption` label + `type-metric` value + `type-caption` sub；`surface-card`。**替换**：admin 7 套指标卡。
5. **`Avatar`**：一款（建议 `rounded-full`），尺寸分 `sm/md/lg` 档，字号随档；删所有 inline `style={{fontSize:…}}`。**替换**：`AccountSheet`/`DesktopMe`/`MobileMe` 三种头像。
6. **`MediaControlButton`**：`bg-[var(--media-control-bg)] text-[var(--media-control-fg)] rounded-full`。**替换**：lightbox/share/poster/模特库的 `bg-black/N text-white/N` 控件。
7. **`Slider`**：封装 `input[type=range]`，自定义轨道（`bg-[var(--bg-3)]`）与拇指（`bg-accent`），非仅 `accent-color`。

**验收**：
```bash
cd apps/web
rg -n 'export (function|const) (Switch|Select|Badge|StatusBadge|MetricCard|Avatar|MediaControlButton|Slider)' src/components/ui/primitives  # 期望各 1
npm run type-check && npm run lint
```
**完成定义**：7 个原语存在并从 `primitives/index.ts` 导出；视觉与 `Button`/`Input` 同族。

---

## 工单 W3 · 全局外壳与导航统一

**目标**：主 CTA、选中态、抽屉、z-index、遮罩、侧栏宽度、字体，全端一致。
**范围**：`shell/*`、`Sidebar.tsx`、`sidebar/*`、`CommandPalette.tsx`、`GlobalTaskTray.tsx`、`tray/*`。
**前置**：W0、W2（头像用 `Avatar`）。
**施工**：
1. **"新建会话"CTA 统一**为 `Button variant="primary"`（`bg-accent text-accent-on`），删反白/琥珀+黑字/图标按钮其余 3 套。文件：`Sidebar.tsx:185`、`MobileConversationDrawerView.tsx:74`、`MobileConversationDrawerStates.tsx:163`、`DesktopStudio.tsx:720`。
2. **"当前选中"统一**：导航项用 accent 下条或左条，列表项用 `bg-accent-soft`，删其余 4 种。文件：`DesktopTopNav.tsx:163`、`MobileTabBar.tsx:113`、`ConversationItem.tsx:112`、`SettingsShell.tsx`。
3. **删 `MobileConversationDrawer` 重复实现**，会话抽屉收敛到 `Sidebar` 一套（统一宽度 `--sidebar-panel-w`、遮罩 `--surface-scrim`、`z-[var(--z-dialog)]`）。
4. **字体硬编码清零**：`text-[10~18px]`/`text-sm/xs` → §1.7 `type-*`（含 `text-[12.5px]`）。按 §3.1 替换。
5. **z-index 回收**：`z-30/40/50/60/61/95` → §1.10 token。
6. **遮罩统一**：`bg-black/40/45/55` → `bg-[var(--surface-scrim)]`。
7. **头像**用 `Avatar`（W2），删 inline style。
8. **搜索框圆角**统一 `rounded-[var(--radius-control)]`。

**验收**：
```bash
cd apps/web
rg -n 'text-\[\d+(\.\d+)?px\]' src/components/ui/shell src/components/ui/Sidebar.tsx src/components/ui/sidebar src/components/ui/CommandPalette.tsx  # 期望 0
rg -n 'z-\[\d+\]|z-[3-9]\d\b' src/components/ui/shell src/components/ui/sidebar  # 期望 0
rg -n 'bg-black/(40|45|55)' src/components/ui/shell src/components/ui/sidebar   # 期望 0
npm run type-check && npm run lint
```

---

## 工单 W4 · Chat 主界面（产品门面）

**目标**：对话画布与 Composer 跨端一致、语义色归位、消灭不可读小字。
**范围**：`components/ui/chat/*`、`components/ui/composer/*`。
**前置**：W0（已修 2 个失效变量）、W1（mask 弹窗）、W2。
**施工**：
1. **最小字号 10px**：`AttachmentRoleBadge.tsx:39` 的 `text-[8px]` → `type-overline`；`MobileComposerExecutionControls.tsx` 的 `text-[9px]` → ≥10px（放不下就降密度，见第 6 条）。
2. **accent 裸 alpha 清零**：`bg-[var(--amber-400)]/N`、`rgba(242,169,58,…)` → `accent-soft`/`accent-border`（§3.2）。重点：`MobileComposerPill`、`CompletionStatusLine`、`ConversationImageGallery`、`ConversationImageGalleryActions.tsx:52`（同组件 danger/accent 双标统一）。
3. **跨端对话元素对齐**：抽 `ConversationTurn`/`FinalImage` 共享原子——统一 UserTurn 对齐与装饰（删移动端 `left:-15px;top:25%;height:50%` 魔法值琥珀条）、统一成图 `object-fit` 与描边。文件：`DesktopConversationTurns.tsx:237-282`、`MobileConversationCanvas.tsx:442-516`。
4. **焦点环统一**：22 处 `focus-visible:ring-[var(--amber-400)]/60` → `focus-visible:shadow-[var(--ring)]`。
5. **浮层归位**：右键菜单/记忆下拉 `shadow-3`→`shadow-2`、`z-[1000]`→`z-[var(--z-tray)]`；图像卡 `rounded-[var(--radius-md)]`→`--radius-card`。
6. **移动生图参数条降密度**：5 列 9px → 摘要层（`type-caption` 摘要）+ 设置层（BottomSheet），对齐 DESIGN §8.3 三层披露。
7. **状态语义**：`CompletionStatusLine` active(accent)/warn(warning) 区分；成本警告 `text-[var(--danger)]`→`text-[var(--warning)]`。
8. **裸 button 收敛**：gallery/turns 的复制/重试/「···」→ `IconButton`/`Button`。

**验收**：
```bash
cd apps/web
rg -n 'text-\[(8|9|\d+\.\d+)px\]' src/components/ui/chat src/components/ui/composer  # 期望 0
rg -n 'amber-400\]|rgba\(242,169,58' src/components/ui/chat src/components/ui/composer  # 期望 0
rg -n 'z-\[1000\]|ring-\[var\(--amber' src/components/ui/chat src/components/ui/composer  # 期望 0
npm run type-check && npm run lint
```

---

## 工单 W5 · Admin 管理后台

**目标**：彩虹条清零、圆角归位、confirm/checkbox 原语化、控件各 1 套、状态徽章统一。
**范围**：`app/admin/**`、`components/admin/*`。
**前置**：W1（ConfirmDialog）、W2（Switch/Select/Badge/MetricCard）。
**施工**：
1. `WEIGHT_COLORS` 彩虹（`providers/model.ts:9-18`）→ 5 语义槽或单色相明度渐变。
2. **圆角归位**：卡片 `--radius-card`、容器 `--radius-panel`、弹窗 `--radius-dialog`；清掉卡片用 12px（`ByokPanel.shared.tsx:86,133`、`RequestEventsPanel.tsx:148` 等）。
3. `window.confirm`（`RedemptionPanel.tsx` 等）→ `ConfirmDialog`；原生 checkbox（`video-providers/ProviderEditorView.tsx:268` 总开关、`ByokPanel` 等）→ `Switch`。
4. 控件收敛：5 套开关/5 套输入框/7 套指标卡 → W2 的 `Switch`/`Input`(control-shell)/`MetricCard`。
5. **状态徽章统一**：英文 `valid/used/revoked`、`wallet/byok` → 中文 + `StatusBadge`；纯灰文本状态列（`PricingSections.tsx:236,366`）补徽章。
6. **密集表格**：超宽可编辑表（`PricingSections.tsx:417,615` min-w-1320）加行高/斑马/hover，移动端 `data-stack-on-mobile`。
7. hover 才显的操作（`providers/card.tsx:156`）→ 常显或溢出菜单。
8. 遮罩 `bg-black/55/60/65` → `--surface-scrim`；`divide-white/5` → `divide-[var(--border-subtle)]`。

**验收**：
```bash
cd apps/web
rg -n 'WEIGHT_COLORS|#6366f1|#ec4899' src/app/admin                              # 期望 0
rg -n 'window.confirm' src/app/admin                                            # 期望 0
rg -n 'type="checkbox"' src/app/admin                                           # 期望 0（除合理豁免）
rg -n 'rounded-\[var\(--radius-dialog\)\]' src/app/admin/_panels                # 卡片场景应大幅减少
npm run type-check && npm run lint
```

---

## 工单 W6 · Settings 设置中心 + 账户钱包

**目标**：内容宽统一 1080px、providers 飞地对齐、弹窗原语化、开关/头像统一、accent 归位。
**范围**：`app/settings/**`、`app/me/**`、`components/ui/me/*`。
**前置**：W1（MemoryCapabilityModal）、W2（Switch/Avatar/Select）。
**施工**：
1. **内容宽统一**：各设置页 `max-w-3xl/4xl/6xl` → `page-frame` + `data-width="settings"`（1080px）。
2. **providers 页对齐**：随 W5 一并收敛（type-*/圆角/accent/WEIGHT_COLORS）。
3. `MemoryCapabilityModal` → `Dialog` 基座（W1）；关闭按钮 `确定`→`关闭`。
4. **开关统一**：记忆 `SettingToggle` 补 `role="switch"`/`aria-checked`（或换 W2 `Switch`）；行内操作（改/删/探活）常显不 hover 才显。
5. **头像**用 `Avatar`；`bg-[var(--amber-400)]`/`text-black` → `accent`/`--accent-on`。
6. **表单控件**统一 `control-shell` + `type-*`；api-key 表单补可见 `<label>`。
7. 桌面"返回我的"冗余导航收敛；壳侧栏标题与页内标题去重。

**验收**：
```bash
cd apps/web
rg -n 'max-w-(3xl|4xl|6xl)' src/app/settings src/app/me                        # 期望 0
rg -n 'amber-400\]' src/app/settings src/app/me src/components/ui/me           # 期望 0
rg -n 'style=\{\{fontSize' src/components/ui/me                                # 期望 0
npm run type-check && npm run lint
```

---

## 工单 W7 · Projects 项目中心

**目标**：中文 label 去 mono、工作流表单统一、英文文案清零、CTA 归位、约束面板结构化。
**范围**：`components/ui/projects/**`、`app/projects/**`。
**前置**：W1（保存候选/约束抽屉）、W2。
**施工**：
1. **中文 label/Chip/状态去 mono+uppercase+宽字距** → `type-caption`（§1.8）。重灾：`stages/ShowcaseSetupFields.tsx`、`library/ModelLibraryBrowserView.tsx`、`ModelLibraryGenerator.tsx`、`ProductAnalysisStageView.tsx`。
2. **工作流表单统一**为盒式 `control-shell`（hairline 下划线输入全迁移，storyboard 同步）。
3. **英文文案清零**：`AI Reading/Enabled/Disabled/Reset/Download/free` → 中文（§3.5）。
4. **主 CTA 统一**：`bg-accent text-accent-on` + `--radius-control`；删无圆角/`text-black`/胶囊混杂（`ProjectFunctionHub.tsx:317`、`ProjectsIndex.tsx:206,190`）。
5. **约束面板结构化**：`jsonValue(...)` 倒原始对象 → 键值/列表/标签渲染（`PosterConstraintPanel.tsx:60-72`、`ConstraintPanel.tsx:42-58`）。
6. 合并 `/projects`(Hub) 与 `/projects/new` 重复入口；单卡动作 >3 个的（`PosterRenderCard.tsx`）收进溢出菜单。
7. 序号标签去 `mix-blend-difference` → `MediaControlButton` 底。

**验收**：
```bash
cd apps/web
rg -n 'font-mono.*uppercase' src/components/ui/projects                    # 期望 0
rg -n 'jsonValue\(' src/components/ui/projects                                 # 期望 0
rg -n 'AI Reading|Enabled|Disabled|>Reset<|>Download<' src/components/ui/projects  # 期望 0
npm run type-check && npm run lint
```

---

## 工单 W8 · Canvas 画布编辑器

**目标**：开关统一、弹窗原语化、节点层级降噪、空态/配色归位。
**范围**：`components/ui/canvas/**`、`app/projects/canvas/**`。
**前置**：W0（z-popover）、W1（RenameDialog/confirm）、W2（Switch/Slider）。
**施工**：
1. 检查器开关统一 `Switch`（`CanvasInspectorFields.tsx:186` 原生框 vs `CanvasNodeConfigFields.tsx:170` 滑块，二留一）。
2. `RenameDialog` → `Dialog` 基座；`window.confirm` 删除确认 → `ConfirmDialog`（`CanvasProjectIndex.tsx:293,179`）。
3. **节点阴影归位**：静止节点 `shadow-2`→`shadow-1`，仅浮起/运行给 `shadow-2`/`shadow-amber`（`nodes/CanvasNodesPresentation.tsx:78`）。
4. 空画布态 → `EmptyState` + `type-*` + `Button`（删 `canvas.module.css:87-136` 自绘）。
5. Frame 与普通节点统一圆角；端口配色按数据类型用语义色（非琥珀绑死图片/遮罩）。
6. `RangeField` → `Slider`；`CanvasTitleInput` 加 hover/focus 边框提示可编辑。
7. 弹窗进场动效 `duration:0` → `var(--dur-dialog)`。

**验收**：
```bash
cd apps/web
rg -n 'window.confirm|--z-popover' src/components/ui/canvas src/app/projects/canvas  # 期望 0
rg -n 'shadow-\[var\(--shadow-2\)\]' src/components/ui/canvas/nodes/CanvasNodesPresentation.tsx  # 静止态应改 shadow-1
npm run type-check && npm run lint
```

---

## 工单 W9 · 认证 + 公开/分享/错误页

**目标**：poster-styles 脱离皮肤归队、语义色修正、控件统一、品牌一致。
**范围**：`app/login`、`signup`、`reset-password`、`invite`、`share`、`poster-styles/*`、`not-found`、`error`、`global-error`。
**前置**：W0（adaptive-material）、W2（Switch/MediaControlButton）。
**施工**：
1. **poster-styles 全模块去 mono+uppercase**（53 处）→ `type-caption`；删 `N°01`、零补全数字、`text-[10.5px]` 等阶梯外字号；对齐主设计语言。
2. **语义色修正**：分享成功 toast 琥珀→success、中性提示琥珀→info、裸黑白 `bg-black/0.68 text-white/0.86`→token（`ShareContentClient.tsx:363,261`）。
3. **控件统一**：poster-styles 下划线输入 → `control-shell`（与认证页 auth-control 同族）。
4. `global-error` 对齐品牌 token（暖灰 `--fg-0`、`#F2A93A`、Geist），不用 zinc/`#f5a623`/system-ui。
5. kicker 不重复主标题（"风格库/风格库"）；邮箱 placeholder 统一 `name@example.com`；标点统一全角。
6. 分享网格 tile `shadow-3`→`shadow-1`、去 hover 琥珀光晕；404 大数字 `type-display` 且去琥珀。

**验收**：
```bash
cd apps/web
rg -n 'font-mono[^"']*uppercase|N°|text-\[10\.5px\]' src/components/ui/poster-styles  # 期望 0
rg -n '#f5a623|zinc|system-ui' src/app/global-error.tsx                          # 期望 0
rg -n '正在' src/app/login src/app/signup src/app/invite                        # 期望 0
npm run type-check && npm run lint
```

---

## 工单 W10 · 灯箱 / 弹窗体系打磨

**目标**：媒体控件原语化、操作区降权、弹窗语法归位。
**范围**：`components/ui/lightbox/*`、`inpaint/*`、`tray/*`、`primitives/Toast|Tooltip|ErrorState|Skeleton`。
**前置**：W1（弹窗基座）、W2（MediaControlButton/Badge/Slider）。
**施工**：
1. **媒体控件收敛**：lightbox/share 的 `bg-black/N text-white/N`（30+ 个一次性透明度）→ `MediaControlButton`（`--media-control-bg/fg`）。
2. **灯箱操作区降权**：6 个同权重按钮 → 主操作 + 溢出菜单；移动端创作钮去琥珀实底（中性，仅当前动作 accent）。文件：`DesktopLightboxView.tsx:112-192`、`MobileLightboxView.tsx:587,749-793`。
3. 灯箱加载失败态 → `ErrorState`（补重试按钮）。
4. 违禁样式：`shadow-2xl/lg`→token、`z-[60]`(Tooltip)→`--z-*`、`rgba(242,169,58,…)`→accent 槽、"正在…"→"…中"。
5. `Kbd` 统一（Inpaint 本地 Kbd 换原语）；`Spinner` 统一（删自绘 CSS 圆环）。
6. 死代码：`GlobalGsapMotion` 空壳、`Card` 的 `data-lumen-reveal`——要么接线要么删净（**先问决策者**）。

**验收**：
```bash
cd apps/web
rg -n 'bg-black/\d+|text-white/\d+' src/components/ui/lightbox                 # 期望大幅减少（应走 media token）
rg -n 'shadow-(2xl|lg)\b' src/components/ui/lightbox                           # 期望 0
rg -n '正在' src/components/ui/lightbox src/components/ui/tray                  # 期望 0
npm run type-check && npm run lint
```

---

## 工单 W11 · 中文排版专项（横切收尾）

**目标**：全站中文不再被当拉丁文排。
**范围**：全 `src/`（在各模块工单后做总清扫）。
**施工**：
1. token 层（若 W 系列未做）：给 `type-overline`/`type-mono-meta`/`type-page-kicker` 加 `:lang(zh)` 修正（去 uppercase、收紧字距至 ≤0.02em、改 sans）。
2. 全站检索中文场景的 `font-mono uppercase tracking-*`，逐个改 `type-caption`。
3. 全站检索 `text-[Npx]` 阶梯外字号，就近归 `type-*`；`type-*` 上叠 px 的全部删掉。
**验收**：
```bash
cd apps/web
rg -n 'font-mono.*uppercase' src                # 中文场景期望 0
rg -n 'text-\[\d+(\.\d+)?px\]' src                  # 期望 0（文档/config 除外）
rg -n 'type-\w+ text-\[' src                        # 期望 0
npm run type-check && npm run lint
```

---

## 工单 W12 · 微文案专项（横切收尾）

**目标**：违禁词清零、文案统一。
**范围**：全 `src/`（收尾）。
**施工**：按 §3.5 全文替换：正在→中、尚未→未、请…→直接句、确定→确认、超 6 字按钮删减、英文词中文化、`x`→`×`。优先把按钮动词/状态词接到 `src/lib/copy.ts` 的 `copy.action/state/error.*`。
**验收**：
```bash
cd apps/web
rg -n '正在|尚未|>确定<' src                         # 期望 0（copy.ts 定义除外）
npm run type-check && npm run lint
```

---

## 附：如果执行者开始"自由发挥"

出现以下任一信号，说明它在**创作**而非**施工**，立即停止并纠正：
- 写出了 §1 决策表之外的颜色/字号/圆角/阴影/z 值。
- 新造了一种 §2 金色标准之外的按钮/卡片/徽章/弹窗。
- 给不该有琥珀的地方加了琥珀。
- 说"我觉得这样更好看"。

纠正话术：**"回到 §0 纪律第 1、3 条。这一步只允许照 §3 反模式表做替换，不许新增样式。把刚才自由发挥的部分回退，用 §1/§2 的值重写。"**

---

## 修订记录

| 日期 | 说明 |
|---|---|
| 2026-08-04 | 初版：把"好看"操作化为宪法 + 决策表 + 金色标准 + 反模式 + 13 张自包含工单，供低审美执行 AI 分批施工。配套诊断见 ui-ux-redesign-plan-2026-08-04.md。 |
