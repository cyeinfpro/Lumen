---
status: current
owner: web
created: 2026-08-03
scope: apps/web UI 质量检阅与优化建议
related:
  - apps/web/DESIGN.md
  - apps/web/src/app/globals.css
  - apps/web/src/components/ui/primitives/
  - apps/web/scripts/check-ui-governance.mjs
---

# Lumen Web UI 优化检阅报告

> 检阅日期：2026-08-03
> 范围：`apps/web/src`（Next.js 16 App Router + React 19 + Tailwind v4）
> 性质：只读检阅，不含代码改动。结论基于源码结构、原语采用率、治理脚本状态与代表性模块抽样。

---

## 0. 一句话结论

设计系统是 **80 分底座 + 50 分采用率**。最大杠杆不是重做视觉，而是：

1. **补齐并强制采用原语**（表单控件 + 统一空/错/Toast）
2. **补齐路由 loading 与 `copy.ts`**
3. **拆分 video / admin / composer 等上帝模块，减少平行实现**

当前治理脚本（UI governance / layout contract / complexity / hit-area）均为通过状态；问题主要在**组合一致性**，而非 token 失控。

---

## 1. 产品与技术背景

| 项 | 现状 |
|---|---|
| 产品 | Lumen Studio — AI 图像 / 对话 / 视频工作台 |
| 栈 | Next.js ~16.2、React 19、Tailwind CSS v4、Zustand、TanStack Query、Framer Motion、Lucide、@xyflow/react、Konva |
| 规模 | `src/` 下约 690 个非测试 TS/TSX，约 16 万+ 行 |
| 设计 SoT | `apps/web/DESIGN.md` + `apps/web/src/app/globals.css` |
| 主题 | 默认 Darkroom 暗色；`.theme-light` / 系统偏好；cookie 驱动 |

### 1.1 信息架构

| Key | 路由 | 表面 |
|---|---|---|
| studio | `/` | 桌面/移动 Studio（对话 + 生图） |
| video | `/video` | 视频工作台 |
| projects | `/projects` | 工作流中枢（服装 / 海报 / 画布 / 分镜 / 模特库） |
| assets | `/assets`（别名 `/stream`、`/library`） | 素材流 / 模特库 |
| me | `/me`、`/me/wallet` | 账户与钱包 |
| settings | `/settings/*` | API Key / 记忆 / 隐私 / 提示词 / 供应商 / Telegram / 用量 |
| admin | `/admin` | 单页多 Tab 运营台 |

导航契约：`src/lib/navigation.ts`。桌面 `DesktopTopNav` + 可选 `Sidebar`；移动 `MobileTopBar` + `MobileTabBar`。

### 1.2 应用壳

```
app/layout.tsx
  └─ fonts + theme class
  └─ GlobalGsapMotion
       └─ LumenAppShell  （/share/* 跳过）
            ├─ QueryProvider + RuntimeDefaultsBootstrap
            ├─ SSEProvider
            ├─ PageTransitions → {children}
            ├─ dynamic: Lightbox / InpaintModal / GlobalTaskTray / CommandPalette
            ├─ SystemUpgradeBanner / OfflineBanner / ToastViewport
            └─ IdleRouteWarmup / ServiceWorkerRegister
```

关键文件：

- `src/app/layout.tsx`
- `src/components/LumenAppShell.tsx`
- `src/app/globals.css`
- `src/components/ui/shell/*`

---

## 2. 现状亮点（建议保持）

| 层 | 表现 |
|---|---|
| 设计 SoT | 5 语义色 + 中性阶、圆角/阴影语义档、14 档 `type-*`、surface / page 布局 utility 完整 |
| 治理 | `check-ui-governance`、`check-layout-contract`、`check-complexity`、hit-area 基线均通过 |
| 原语质量 | `Button`（variant × size、loading、`aria-busy`、移动 min 命中区）、`EmptyState`、`ErrorState`、`Toast`、移动端 `BottomSheet` / `Pressable` 可用 |
| 壳层 | Studio 桌面/移动分叉清晰；safe-area、键盘 inset、reduced-motion 有考虑 |
| 主题 | Darkroom 优先；焦点环与 accent 一致；ESLint 禁止原生调色板色与无语义 `rounded-*` |
| 无障碍基础 | `lang="zh-CN"`、tab bar `aria-current`、ErrorState `role="alert"`、canvas 工具条标签较全 |

**不要做的事：**

- 不要重做 Darkroom 视觉或换配色
- 不要强行合并桌面/移动整页组件（应抽共享原子，而非消灭分叉）
- 不要为文案规范上完整 i18n 框架（先吃透 `copy.ts`）
- 不要为「行数好看」机械切文件（按状态 / 展示 / 交互边界拆）

---

## 3. 问题总览（按优先级）

### P0 — 投入产出最高

#### 3.1 表单与浮层原语缺口

`primitives/` 当前导出：

- 操作：`Button`、`IconButton`
- 表单：仅 `Input`、`Textarea`
- 反馈：`Spinner`、`Skeleton`、`EmptyState`、`ErrorState`、`Toast`、`Tooltip`、`ConfirmDialog`
- 布局：`Card`
- 移动：`BottomSheet`、`ActionSheet`、`Pressable`、`Chip`、`SegmentedControl` 等

**明显缺失：** `Select`、`Checkbox`、`Switch`、`Radio`、`Tabs`、`Badge`、`FormField`、通用 `Modal` / `DropdownMenu` / `Popover`。

**证据：**

- 裸 `<select>` 散落在 admin providers / video-providers / model library / composer / storyboard 等
- `SwitchField` 在 `app/admin/_panels/BillingPanelParts.tsx` 私有实现；Telegram、Storage、AdminUpdate 等各自再写 `role="switch"`
- 全站约 **39** 个 `role="dialog"` 实现；`ConfirmDialog` 调用面明显偏少

**影响：** 焦点环、高度、禁用态、标签关联、移动 44px 命中区在 feature 间漂移，尤其 admin + video + canvas inspector。

**建议：**

1. 新增 `Switch`、`Select`、`FormField`、`Modal`（优先）
2. 从 admin billing / providers / video workbench 三处迁移
3. 后续再补 `Checkbox`、`Tabs`、`Badge`、`DropdownMenu`

#### 3.2 空态 / 错误态 / 骨架双轨甚至多轨

| 体系 | 位置 | 问题 |
|---|---|---|
| 设计系统 | `EmptyState` / `ErrorState` / `Skeleton` | 全站引用约 **11** 处，采用率偏低 |
| Admin 平行实现 | `AdminFeedback.EmptyBlock` / `ErrorBlock` / `ListSkeleton` | 文案层级、圆角、按钮样式与原语不一致 |
| 散装空态 | Sidebar、ConversationList、Redemption、Pricing、Providers… | 大量「暂无 xxx」裸段落 |

对比示例：

- `EmptyBlock` 用 `text-sm`；原语 `EmptyState` 用 `type-card-title`
- `ErrorBlock` 使用裸 `<button>` 而非 `Button`，且错误标题写死「加载失败」

**建议：**

1. `EmptyBlock` / `ErrorBlock` 改为对 `EmptyState` / `ErrorState` 的薄封装（可保留紧凑 layout variant）
2. 列表空态统一走 `EmptyState`，禁止新增裸「暂无」段落
3. 骨架统一 `Skeleton` 或基于它的 `ListSkeleton`

#### 3.3 Toast 双轨

| API | 文件 | 用途 |
|---|---|---|
| `toast` + `ToastViewport` | `primitives/Toast.tsx` | 桌面主路径 |
| `pushMobileToast` | `primitives/mobile/Toast.tsx` | 移动路径；桌面 asset stream 偶发调用 |

**风险：** 位置、时长、与 tab bar 避让不一致；消息「发了但看不见」。

**建议：** 单一 `toast()` API，内部按 viewport / 是否有 tab bar 决定锚点；`pushMobileToast` 标记 deprecated 后删除。

---

### P1 — 结构与体验债

#### 3.4 超大 UI 模块

高频「上帝组件」（约 650–800+ 行，抽样）：

| 文件 | 约行数 | 建议拆分方向 |
|---|---:|---|
| `composer/desktop/DesktopComposerPill.tsx` | ~797 | 输入区 / 附件 / 高级设置 / 发送态 |
| `composer/mobile/MobileComposerPill.tsx` | ~712 | 同上 |
| `canvas/CanvasViewport.tsx` | ~800 | 视口 / overlay / 选择工具条 |
| `shell/DesktopStudio.tsx` | ~733 | chrome vs conversation vs composer |
| `Sidebar.tsx` | ~737 | 列表 / 搜索 / 空态 / item |
| `app/video/page.tsx` 及 view/task/workbench 族 | 多份 650–800 | 状态 hooks 与纯展示彻底分离 |
| admin 多 panel | 700–900 | 已部分拆，继续按 subview |

大文件最容易出现桌面/移动按钮高度不一致、空态样式漂移、无障碍标签遗漏。

#### 3.5 Video 模块架构不一致

`app/video/*` 约 **1.8 万行** UI + 逻辑共置；chat / canvas / projects 则在 `components/ui/*`。

**影响：**

- governance 与 primitives 采用路径不一致
- `page.tsx` 状态过重
- 缺少 `app/video/loading.tsx`

**建议：** 渐进迁到 `components/ui/video/` 或 `features/video/ui/`；`app/video/page.tsx` 只做路由组装；同步补 loading。

#### 3.6 路由 loading 覆盖不全

**已有 `loading.tsx`：**

- `/`（根）
- admin、settings、projects、projects/canvas、canvas/[id]
- storyboard/[runId]、share/[token]、library

**高流量缺失：**

- `/video`
- `/assets`、`/stream`
- `/me`、`/me/wallet`
- `/login`、`/signup`
- `/poster-styles`
- `/projects/library`、`/projects/new`

部分 route 使用 `fallback={null}` → 首屏白闪。统一复用 `RouteLoadingSkeleton` / `ShellSkeleton`，成本低、体感提升大。

#### 3.7 `copy.ts` 采用率过低

规范见 `DESIGN.md` §5；`src/lib/copy.ts` 已定义标准动词 / 状态词 / 错误句式，但仅约 **31** 个文件引用。

**已出现漂移示例：**

- 「加载失败」vs `copy.state.failed`（「失败」）
- 「暂无流水 / 暂无兑换记录 / 还没有会话 / 当前筛选下暂无作品」同义不同句
- 进行式/完成式用词不统一

**建议：**

- 新代码强制 `copy.action.*` / `copy.state.*` / `copy.error.*`
- 旧代码按模块批量替换按钮动词与状态词（不必一次改完所有长句）

---

### P2 — 打磨项

#### 3.8 裸 `<button>` 过多

`Button` / `IconButton` / `Pressable` 之外，高密度裸 button 出现在：

- `poster-styles/PosterStyleBrowser.tsx`
- lightbox 桌面/移动 View
- storyboard、share gallery
- workflow detail、conversation item、asset stream

**可保留：** 高度定制的媒体控件、画布工具。
**应迁回原语：** 通用 CTA、筛选 chip、列表行操作、确认/取消。

**收益：** 统一 `aria-busy`、disabled、移动 `min-h-11`、焦点环。

#### 3.9 媒体 chrome 的 `text-white` / `bg-black` 债

约 150+ 命中，集中在 share gallery、poster-styles 浮层、lightbox / mask。暗色下好看，**亮色主题会穿帮**。

`globals.css` 已提供：

- `--media-control-bg`
- `--media-control-fg`

**建议：** 媒体浮层 chrome 统一走 media-control token；governance 可对 media path 保留例外，但 chrome 本身必须 token 化。

#### 3.10 SettingsShell 宽度未吃 layout token

```ts
// SettingsShell.tsx
maxWidth = "max-w-6xl"  // Tailwind ≈ 1152px

// globals.css
--content-settings: 1080px
```

内部还有 `max-w-[1440px]` 硬编码。应对齐 `page-frame` / `--content-settings` / `--content-workbench`。

#### 3.11 桌面/移动双树的共享原子偏薄

Studio / chat / composer / lightbox / assets / me 双实现是合理产品选择，但共享层主要是 token，**表现原子不够**：

- 会话空态两边各写一遍
- Composer 附件 chip / 发送按钮态可能漂移
- Lightbox 操作图标 accessible name 不一定对称

**建议抽取（无布局假设）：**

- `ConversationEmpty`
- `ComposerSendButton`
- `MediaActionBar`

桌面/移动只包布局。

#### 3.12 死代码与入口别名

- `app/(chat)/` 空路由组可删
- `/stream` vs `/assets` 几乎重复入口，建议收敛 redirect 与文档，减少双维护

#### 3.13 无障碍边角

**强项：** 根语言、主题 `color-scheme`、导航标签、ErrorState live role、canvas 工具条、hit-area 治理。

**缺口：**

- 无 `FormField` → `label` / `htmlFor` / `aria-describedby` 不一致
- 部分 icon-only 裸 button 缺稳定 accessible name
- 自定义 tablist / select 未必有 roving tabindex
- 异步列表更新多靠 toast，少 `aria-live` 区域（admin 表格尤甚）
- share / lightbox 低对比白色半透明文字在部分背景下可能不达 AA

---

## 4. 关键路径与文件地图

### 4.1 设计系统与治理

| 路径 | 说明 |
|---|---|
| `apps/web/DESIGN.md` | 设计语言 SoT |
| `apps/web/src/app/globals.css` | token、主题、type、surface、page utility |
| `apps/web/src/lib/copy.ts` | 共享微文案 |
| `apps/web/src/lib/utils.ts` | `cn()` |
| `apps/web/src/lib/motion.ts` | 动效辅助 |
| `apps/web/eslint.config.mjs` | 设计 token 防回归 |
| `apps/web/scripts/check-ui-governance.mjs` | 暗色硬编码 / media / live-region 债 |
| `apps/web/scripts/check-layout-contract.mjs` | 布局契约 |
| `apps/web/scripts/audit-hit-area.mjs` | 触控命中区审计 |

### 4.2 原语

| 路径 | 说明 |
|---|---|
| `src/components/ui/primitives/Button.tsx` | 标准 CTA |
| `src/components/ui/primitives/IconButton.tsx` | 图标控件 |
| `src/components/ui/primitives/Input.tsx` / `Textarea.tsx` | 文本输入 |
| `src/components/ui/primitives/EmptyState.tsx` / `ErrorState.tsx` | 反馈态 |
| `src/components/ui/primitives/Toast.tsx` | 桌面 toast |
| `src/components/ui/primitives/ConfirmDialog.tsx` | 共享确认框 |
| `src/components/ui/primitives/mobile/*` | 移动 sheet / pressable / chip 等 |

### 4.3 壳与主表面

| 路径 | 说明 |
|---|---|
| `src/components/LumenAppShell.tsx` | 全局 provider 与浮层 |
| `src/components/ui/shell/ResponsiveStudio.tsx` | Studio 响应式入口 |
| `src/components/ui/shell/DesktopStudio.tsx` / `MobileStudio.tsx` | 主产品 chrome |
| `src/components/ui/shell/DesktopTopNav.tsx` / `MobileTabBar.tsx` | 主导航 |
| `src/components/ui/shell/SettingsShell.tsx` | 设置页框架 |
| `src/lib/navigation.ts` | IA / 导航可见性 |

### 4.4 重点 feature

| 路径 | 说明 |
|---|---|
| `src/components/ui/chat/**` | 桌面/移动对话画布 |
| `src/components/ui/composer/**` | 桌面/移动 composer |
| `src/components/ui/canvas/**` | 节点图画布 |
| `src/components/ui/projects/**` | 工作流、模特库、分镜 |
| `src/app/video/**` | 视频工作台（架构异位） |
| `src/features/assets/**` | 素材虚拟瀑布流 |
| `src/app/admin/**` | 管理台与 panel |
| `src/components/ui/lightbox/**` / `inpaint/**` | 全局媒体浮层 |
| `src/app/me/wallet/WalletPageView.tsx` | 用户钱包 |

---

## 5. 量化快照（检阅时）

| 指标 | 约数 | 备注 |
|---|---:|---|
| `EmptyState` 引用文件 | ~11 | 不含 primitives 自身 |
| `ErrorState` 引用文件 | ~11 | 同上 |
| `copy.ts` 引用文件 | ~31 | 相对全站 UI 偏低 |
| `role="dialog"` 实现文件 | ~39 | ConfirmDialog 覆盖不足 |
| 裸 `<button>` 高发文件 | 20+ | poster / lightbox / storyboard / share 等 |
| Admin `EmptyBlock/ErrorBlock` 使用面 | 多 panel | 与设计系统平行 |
| `app/video` 源码规模 | ~18k 行 | 含测试约更多 |
| UI governance / complexity / layout | 0 新增违规 | 底座治理健康 |

> 以上为检阅日抽样统计，落地改造时请以仓库实时 `rg` / 脚本为准。

---

## 6. 推荐落地顺序

### 第 1 周 — 一致性杠杆

1. 实现 `Switch` / `Select` / `FormField` / `Modal` 原语
2. `AdminFeedback` 改为封装 `EmptyState` / `ErrorState` / `Skeleton`
3. 合并 Toast API（`toast` 统一，弃用 `pushMobileToast`）
4. 迁移 admin billing + providers 中的 switch / select / 空错态

### 第 2 周 — 体感与文案

1. 高流量 `loading.tsx`：`/video`、`/assets`、`/me`、`/me/wallet`、auth
2. `SettingsShell` 对齐 `--content-settings` 等 layout token
3. `copy.action` / `copy.state` 批量替换按钮与状态词
4. 媒体 chrome 试点改 `--media-control-*`

### 第 3–4 周 — 结构瘦身

1. video 渐进迁出 `app/video` → `components/ui/video` 或 `features/video/ui`
2. Composer / Sidebar / 部分 Admin panel 按交互边界拆分
3. 抽取 `ConversationEmpty`、`ComposerSendButton`、`MediaActionBar`
4. 删除空 `app/(chat)/`；收敛 `/stream` → `/assets` 文档与 redirect

### 持续门禁

- 新 UI：**禁止**新增裸 select / switch / 空态段落 / 第二套 toast
- PR 检查：是否复用 `primitives` + `copy`
- 保留并扩展现有 governance 脚本（可增加「禁用 AdminFeedback 平行实现」类规则）

---

## 7. 可选立即开工包

若进入实现阶段，建议按单包交付，避免大爆炸 PR：

| 包 | 内容 | 验收 |
|---|---|---|
| **A. 表单原语** | `Switch` / `Select` / `FormField` + admin billing 迁移 | 视觉对齐 token；键盘与 focus-visible；移动 min 高度 |
| **B. 反馈统一** | Toast 合并 + AdminFeedback 封装 Empty/Error | admin 列表空/错与 shell 一致；移动 toast 不丢 |
| **C. Loading 补齐** | video / assets / me / auth 的 `loading.tsx` | 首屏无白闪；骨架与 shell 几何一致 |
| **D. Video 迁层** | 纯展示组件迁出 `app/video`，page 变薄 | architecture check 通过；行为无回归 |
| **E. Copy 收敛** | 按钮动词与状态词替换 | 无「好的/我知道了/正在…」；关键路径走 `copy.*` |

---

## 8. 总结

Lumen Web 前端具备**清晰的设计语言、健康的治理基线、可用的响应式壳层**。质量压力来自产品表面扩张后，**原语覆盖跟不上 feature 分叉速度**：

- admin / video / workflows 手搓控件与反馈态
- 桌面/移动双树共享原子不足
- 文案与 loading 契约采用不完整

按上文 P0 → P1 → P2 推进，可在**不改品牌视觉**的前提下显著提升一致性、可维护性与无障碍下限。

---

## 修订记录

| 日期 | 说明 |
|---|---|
| 2026-08-03 | 初版：全站 UI 只读检阅与优化路线图 |
