---
status: current
owner: web
last_reviewed: 2026-07-10
supersedes_frontend_guidance: docs/DESIGN.md
---

# Lumen 设计语言（SoT）

本文件是 `apps/web` 的设计语言**唯一来源**。所有颜色、排版、圆角、阴影、文案规范都在此约定。底层值在 `apps/web/src/app/globals.css`，本文档只描述「何时用、怎么用」。

如果发现 UI 与本文不符，先改 UI。如果设计需要新增，先改本文件 + globals.css，再写组件。

---

## 1. 配色：5 语义槽 + 中性灰

全站只允许 5 个语义色 + 中性 4 阶。**禁止任何 Tailwind 原生强调色**（`text-red-*` `bg-emerald-*` `border-sky-*` 等）。

### 1.1 5 语义槽

| 槽 | 用途 | utility |
|---|---|---|
| **accent**（琥珀） | 品牌主色、CTA、选中、聚焦光晕 | `bg-accent / text-accent / border-accent-border / ring-accent` |
| **danger**（红） | 破坏性操作（删除/撤销）、错误、超额 | `bg-danger / text-danger / border-danger-border` |
| **success**（绿） | 成功、已启用、连接正常 | `bg-success / text-success / border-success-border` |
| **warning**（黄） | 提醒但非错误（成本警告、即将过期） | `bg-warning / text-warning / border-warning-border` |
| **info**（蓝） | 中性提示、可选信息（对应 sky+blue） | `bg-info / text-info / border-info-border` |

每槽 3 形态：
- `bg-{name}` / `text-{name}` / `border-{name}` — 实色（按钮、徽章实底、强调文字）
- `bg-{name}-soft` — 弱底（hover、notice 背景）
- `border-{name}-border` — 描边（带 0.30 alpha 的弱边）

### 1.2 何时用 / 何时不用

| 场景 | ✅ 用 | ❌ 不用 |
|---|---|---|
| 删除按钮 | `bg-danger text-white` | `bg-red-500` |
| 错误提示框 | `bg-danger-soft border-danger-border text-danger` | `bg-red-500/10 border-red-500/30 text-red-300` |
| 成功 toast | `bg-success-soft text-success` | `bg-emerald-500/10 text-emerald-300` |
| 信息提示 | `bg-info-soft text-info` | `bg-sky-500/10 text-sky-300` 或 `bg-blue-500/10 text-blue-300` |
| 即将到期警告 | `bg-warning-soft text-warning` | `bg-amber-500/15 text-amber-200` |
| 装饰性光晕（保留） | `var(--amber-glow)` / `var(--shadow-amber)` | — |

### 1.3 亮色对比度补偿

`bg-danger` 等 utility 是字面量，亮色模式可读性可能不足。密集正文场景用 `text-[var(--danger-fg)]`：暗模式回落到 `--danger`，亮模式由 `.theme-light` 覆盖到 Radix step 11 的加深色（AA ≥ 4.5:1）。

### 1.4 中性灰

按用途分 4 阶（已存在）：

- **背景**：`bg-[var(--bg-0)]`（最底）→ `bg-1` → `bg-2` → `bg-3`（最浅）
- **前景**：`text-[var(--fg-0)]`（最强对比）→ `fg-1` → `fg-2` → `fg-3`（最弱，失能态）
- **描边**：`border-[var(--border-subtle)]` → `border` → `border-strong`

**禁止** `text-neutral-*` `bg-neutral-*` `border-white/N`（已有 `.theme-light` 兼容覆盖，但新代码不要写）。

---

## 2. 圆角：5 语义档（已定义在 globals.css）

| token | 值 | 用途 |
|---|---|---|
| `--radius-control` | 6px | 按钮、输入框、Tag、Chip |
| `--radius-card` | 8px | 卡片、列表行 |
| `--radius-panel` | 10px | 浮层、Tooltip、PopOver、Drawer |
| `--radius-dialog` | 12px | 弹窗 |
| `--radius-sheet` | 16px | 移动 BottomSheet |
| `--radius-pill` / `rounded-full` | 999px | 圆形按钮、Avatar、Pill |

写法：`rounded-[var(--radius-card)]`。

**禁止** `rounded-md/lg/xl/2xl/3xl/sm/xs`（与 token 同值但语义不明）。例外：`rounded-full` / `rounded-none` 可写。

---

## 3. 阴影：4 档 + 1 特例

| token | 用途 |
|---|---|
| `--shadow-1` | 静态卡片、Input |
| `--shadow-2` | 浮起 / hover 提一档、Drawer、Tooltip |
| `--shadow-3` | 弹窗、Toast |
| `--shadow-amber` | 品牌强调光晕 |
| `--shadow-shutter` | 显影动画特例 |

写法：`shadow-[var(--shadow-1)]`。

**禁止** inline `shadow-[0_...]` 和 `shadow-2xl/xl/lg`。

---

## 4. 排版：11 档 type-* class（已定义）

中文/英文混排，统一用 `type-*` class，不用 `text-{size} font-{weight}` 组合。

| class | 用途 |
|---|---|
| `type-display` | 营销页、空状态大标（32px / 700） |
| `type-display-lg` | 中等大号（28px / 700） |
| `type-page-title` | 路由页主标题（24px / 600） |
| `type-page-title-sm` | 紧凑页主标题（22px / 600） |
| `type-page-kicker` | 标题之上的小 mono uppercase（10px） |
| `type-page-subtitle` | 副标题（12px） |
| `type-section-title` | Panel/Card 组标题（20px / 600） |
| `type-card-title` | 卡片标题（16px / 600） |
| `type-body` | 正文默认（15px） |
| `type-body-sm` | 次级正文、设置项 detail（13px） |
| `type-caption` | 标签、辅助说明（12px） |
| `type-overline` | 分组标签（10px / uppercase / sans） |
| `type-mono-meta` | 元数据 mono uppercase（10px） |
| `type-metric` | 数字指标（22px tabular） |

### 替换示例

| Before（硬编码） | After |
|---|---|
| `text-2xl font-semibold` | `type-page-title` |
| `text-xl font-semibold` | `type-section-title` |
| `text-base font-semibold` | `type-card-title` |
| `text-sm` | `type-body-sm` |
| `text-xs` | `type-caption` |
| `text-xs uppercase tracking-wider font-semibold` | `type-overline` |

---

## 5. 微文案规范

### 5.1 按钮动词表

固定 12 个动词：**保存 / 取消 / 确认 / 删除 / 重试 / 继续 / 上一步 / 下一步 / 关闭 / 复制 / 导入 / 导出**。

- 禁用 "好的" / "我知道了" / "OK"
- 复合按钮可加宾语：`保存草稿` / `导出 PNG`，但宾语 ≤ 2 字
- 长度上限：6 个汉字以内

通用文案集中在 `apps/web/src/lib/copy.ts`，**优先复用 `copy.action.*`**。

### 5.2 状态词

固定 8 个，全 4 字以内：**加载中 / 已保存 / 已删除 / 已复制 / 失败 / 成功 / 暂无 / 无结果**。

进行式后缀统一用「中」（保存中、删除中、上传中），完成式用「已」（已保存、已删除）。**禁止「尚未」「正在」**。

### 5.3 错误句式

格式：`<对象> <问题>`。

| ✅ | ❌ |
|---|---|
| 网络异常 | 请检查您的网络连接 |
| 凭据失效 | 您的登录已过期，请重新登录 |
| 上传超时 | 上传超时啦，请重试 |
| 格式不正确 | 请填写一个有效的整数 |

**禁用前缀**：`请...` / `您的...` / `不能...`

### 5.4 数字单位

中英文之间 1 空格：`12 张` / `3.4 MB` / `250 ms` / `1080 × 1920`。

纯中文紧贴：`五张图`（很少用，优先阿拉伯数字）。

### 5.5 长度上限

| 元素 | 上限 |
|---|---|
| 按钮 | 6 字 |
| Tooltip | 20 字 |
| Banner 标题 | 12 字 |
| Banner 详情 | 30 字 |
| 设置项 detail | 15 字（多选项的解释下沉到 `choice.description`） |
| 表单错误提示 | 12 字 |

---

## 6. Surface primitives（已定义在 globals.css）

四档统一的"表面"语法，组件层面优先用，不要重复写 `bg-... border-... rounded-...`：

| class | 用途 |
|---|---|
| `surface-card` | 标准卡片（border + shadow-1 + 半透明 bg-1） |
| `surface-card-hover` | 配合上面用，hover 自动提升 border + shadow |
| `surface-panel` | 浮层、Tooltip、Drawer（border + shadow-2 + blur 16） |
| `surface-dialog` | 弹窗（border + shadow-3 + blur 20） |
| `control-shell` | 输入框/Segmented 的统一外壳 |

普通页面分区不要默认套 `surface-card`。优先使用：

| class | 用途 |
|---|---|
| `page-shell / page-scroll / page-frame` | 路由页面骨架、滚动区与稳定内容宽度 |
| `page-header / page-header-copy / page-header-actions` | 页面标题、说明和主操作 |
| `page-section / section-header` | 全宽内容分区，用 hairline 建立层级 |
| `toolbar-shell / toolbar-group` | 筛选、批量操作和视图工具 |
| `list-group / list-row` | 设置、账户和高密度操作列表 |
| `dialog-layout / dialog-header / dialog-body / dialog-footer` | 弹层标题、滚动内容和操作区 |

`Card` 默认不抬升；只有需要从页面中真正浮起的对象才显式使用 elevation。
避免 `Card` 套 `Card`，也不要把整段页面 section 做成浮动卡片。

---

## 7. 反模式速查

| 反模式 | 改成 |
|---|---|
| `text-red-300 bg-red-500/10 border-red-500/30` | `text-danger bg-danger-soft border-danger-border` |
| `bg-emerald-500/15 text-emerald-300` | `bg-success-soft text-success` |
| `text-sky-300` 或 `text-blue-400` | `text-info` |
| `text-neutral-300` `border-white/15` | `text-[var(--fg-1)]` `border-[var(--border)]` |
| `rounded-2xl` | `rounded-[var(--radius-dialog)]` 或 `rounded-[var(--radius-panel)]`，看上下文 |
| `shadow-[0_8px_24px_rgba(0,0,0,0.4)]` | `shadow-[var(--shadow-2)]` |
| `text-base font-semibold text-neutral-100` | `type-card-title` |
| `<button className="...">` 自己拼样式 | `<Button variant="..." size="...">` 或 `<IconButton>` |

---

## 8. Luminous Darkroom 2.0 布局规则

### 8.1 视觉权重

固定顺序：**内容 > 当前任务状态 > 导航 > 设置**。

- 全局 App Bar 只承载品牌、顶层导航、命令面板、任务入口和账户菜单。
- 会话级 Fast、上下文、记忆、系统提示词放在 Studio Context Bar。
- 新建会话属于侧栏主动作，不与账户和任务状态并列。
- 琥珀色只用于当前动作、当前焦点和当前运行状态；普通边框、普通 Hover、普通图标保持中性。

### 8.2 内容宽度

`globals.css` 提供六个稳定宽度：

| token | 用途 |
|---|---|
| `--content-text` | 对话、Markdown、代码正文，800px |
| `--content-form` | 登录、注册、创建流程核心表单，720px |
| `--content-settings` | 设置页主内容，1080px |
| `--content-composer` | Desktop Composer，880px |
| `--content-media` | 图片结果、对比和会话媒体，1160px |
| `--content-workbench` | 项目、视频和多栏工作台，1440px |

不要让文字正文直接继承工作台宽度。媒体可以突破文字列，但不能反向把正文拉宽。

### 8.3 Composer 分层

Composer 必须按三层渐进披露：

1. 核心层：附件、模式、输入、发送/停止。
2. 摘要层：当前执行参数与成本摘要。
3. 设置层：Desktop Popover / Mobile Bottom Sheet 中的模型、比例、数量、质量、推理与工具。

高级参数不得长期占据主输入工具栏，也不得因打开设置而改变输入框主几何。
生图模式的数量、比例、输出尺寸、生成质量与 Fast 属于高频执行参数，可在摘要层直接调整；模型、工具与其他低频项仍留在设置层。

### 8.4 Shell 与移动触控

- Desktop App Bar 高度 56px。
- Desktop Sidebar：宽屏固定、桌面中宽 64px 窄栏、窄屏抽屉。
- Mobile Top Bar 第一行只处理位置、切换与新建；会话参数放第二行。
- Mobile Tab Bar 高度 56px，标签不小于 11px。
- 视觉按钮可以小于 44px，但移动端实际命中区必须至少 44×44px。

---

## 9. ESLint 防回归

`apps/web/eslint.config.mjs` 配置以下规则（陆续上线）：

1. **强调色**：禁用 `\b(text|bg|border|ring)-(red|emerald|amber|sky|blue|green|yellow|orange|rose|pink|fuchsia|violet|indigo|cyan|teal|lime)-\d+\b`
2. **圆角**：禁用 `\brounded-(xl|2xl|3xl|md|lg|sm|xs)\b`
3. **阴影**：禁用 `\bshadow-\[`
4. **中性色阶**：禁用 `text-neutral-* / bg-neutral-*`，改用 `--fg-* / --bg-*`

`Button` / `IconButton` / `Pressable` 仍是默认选择。原生 `<button>` 可用于
组件内部的专用语义控件（例如拖拽把手、媒体覆盖操作、复合输入器子控件），但必须
保留正确的 `type`、可访问名称、Disabled 状态，并继承全局 Focus Outline；因此不再
把所有 Raw Button 写成无法执行的绝对禁令。

例外：图表、第三方库子组件可用 `// eslint-disable-next-line` 显式豁免。

---

## 10. 变更记录

- **2026-07-10 Luminous Darkroom 2.0**：收拢 App Bar 与 Studio Context Bar 层级，建立四档内容宽度、三层 Composer、三态侧栏、统一任务入口和克制的强调色规则。
- **2026-05-09 V1 设计语言统一**：建立 5 语义槽 utility（`@theme` 注册 15 个 `--color-*` 字面量），新增 4 个 `*-fg` 变量做亮色补偿，补 `.type-display / .type-display-lg / .type-overline` 三档。
