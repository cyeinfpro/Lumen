# 计费/钱包/兑换系统 UI 错位与叠加问题修复文档

> 范围：commit `c604858`、`6b66f86`、`056205f`、`4771eaf` 引入的所有前端改动
> 重点：移动端（safe-area、键盘、窄屏挤压、固定层叠加）+ 桌面端布局回归
> 标准：必须满足 `docs/frontend-theme-dialog-standards.md` 与 `apps/web/src/app/globals.css` 的 token / safe-area / z-index 体系
> 完成判定：每项 fix 都给出 **文件:行号**、**症状**、**根因**、**修复方案**、**验收**

---

## 0. 全局问题（优先级最高，先于单文件修复）

### 0.1【P0】SystemUpgradeBanner 与对话框 / 顶栏的 z-index 冲突
- **位置**：`apps/web/src/components/SystemUpgradeBanner.tsx:28`
- **现状**：`fixed inset-x-0 top-0 z-[90]`
- **冲突清单**（来自 `apps/web/src/app/globals.css:203-212` 的 token 体系）：
  | 元素 | 当前 z | 应当层级 |
  | --- | --- | --- |
  | `--z-header` | 10 | TopBar |
  | `--z-tabbar` | 20 | MobileTabBar |
  | `--z-composer` | 40 | PromptComposer 基线 |
  | `--z-tray` | 50 | 通知/任务托盘 |
  | `--z-dialog` | 90 | 所有对话框 |
  | `--z-lightbox` | 95 | Lightbox |
  | `--z-toast` | 100 | Toast / OfflineBanner |
  | SystemUpgradeBanner | **写死 90** | ❌ 与 `--z-dialog` 同层 |
  | RedemptionPanel `NewCodesModal` | **写死 80** | ❌ 比 banner 还低 |
  | PromptComposer toast | **写死 z-[60]** | ❌ 直接硬编码 |
  | PromptComposer 主体 | **写死 z-50** | ❌ 与 `--z-tray` 同层 |
- **症状**：升级 banner 弹出时，
  1. `NewCodesModal`（admin 创建兑换码后弹的明文码窗口）被 banner 顶部覆盖，且 banner 滑入时把弹窗右上角的关闭按钮挡住。
  2. PromptComposer 与 banner 处于不同 stacking context，但移动端键盘弹出时 banner 仍然挂在 viewport 顶部，叠加 OfflineBanner（z-100）时 banner 会被压到下方却又遮 TopBar。
- **修复方案**：
  - 在 `globals.css` 增加 `--z-banner: 85`（< dialog < toast）。
  - `SystemUpgradeBanner.tsx` 改为 `style={{ zIndex: "var(--z-banner, 85)" }}`，移除 `z-[90]`。
  - `RedemptionPanel.tsx:480` `NewCodesModal` 改为 `z-[var(--z-dialog,90)]`。
  - PromptComposer 两处 `z-50` / `z-[60]` 改为 `z-[var(--z-composer,40)]` / `z-[var(--z-tray,50)]`。
- **验收**：升级中触发 admin 发码、保持移动端 PromptComposer 打开，banner 不应遮挡任何对话框；OfflineBanner（离线）应位于 banner 之上。

### 0.2【P0】SystemUpgradeBanner 未暴露高度变量，所有 sticky/fixed 元素被遮
- **位置**：`apps/web/src/components/SystemUpgradeBanner.tsx`（整文件）
- **症状**：banner 占据顶部 ≈40px，但 `MobileTopBar`（`sticky top-0`，`MobileTopBar.tsx:52`）和 `DesktopTopNav`（`sticky top-0 z-30`，`DesktopTopNav.tsx:81`）依然按 `top: 0` 渲染，被 banner 完全覆盖；Lightbox 关闭按钮、PromptComposer 上方拖拽提示也会被压。
- **根因**：banner 是 `fixed`，不参与流式高度计算；下游组件没有 `padding-top` 或 `top` 偏移。
- **修复方案**（推荐双 token 方案）：
  1. 在 `SystemUpgradeBanner` 内用 `ResizeObserver` 或固定值把高度写到 `document.documentElement.style.setProperty('--system-banner-height', '40px')`，且在 `useEffect` cleanup 时清空（设回 `0px`）。
  2. `globals.css :root` 初始 `--system-banner-height: 0px`。
  3. `MobileTopBar.tsx:60-62` 的 `paddingTop` 改为 `calc(env(safe-area-inset-top, 0px) + var(--system-banner-height, 0px))`；DesktopTopNav 同样在 `style` 里加 `top: var(--system-banner-height, 0)`（仍 `sticky`）。
  4. PromptComposer `bottom: calc(...)` 不变；但其 toast (`PromptComposer.tsx:386`) 的 `bottom` 不受影响。
- **验收**：触发 `getSystemMaintenance.running=true`，确认 TopBar / Tab / 任何 `sticky top-0` 元素都向下偏移；banner 关闭后所有元素恢复原位无跳变。

### 0.3【P1】MobileWalletPill 与右侧 slot 在窄屏的拥挤
- **位置**：`apps/web/src/components/ui/shell/MobileTopBar.tsx:64-69`
- **现状**：所有移动端页面都会在 right slot 前自动注入 `<MobileWalletPill />`。`MobileStudioTopBar.tsx:118-170` 的 right 已经塞了 6 个图标（Fast、ContextWindow、ConversationMemory、可能的 GenerationRing、新建、设置）。加上 `¥XXX.XX` Pill 后，375px 屏幕 left（标题 + 抽屉按钮）剩余空间不足 100px。
- **症状**：
  - 余额超过 ¥999.99 时 Pill 宽度增长，挤压右侧图标导致最右侧的"设置"图标被裁掉或溢出 `overflow-hidden` 容器外。
  - 余额闪烁（流水更新时）会让右侧整体宽度跳动，触感很差。
- **修复方案**：
  1. `MobileWalletPill` 增加 `max-w-[88px] truncate`。
  2. 余额 ≥ 1000 时显示精简形式：`Number(rmb).toFixed(0)` + `k` 后缀（如 `¥1.2k`），用 `Intl.NumberFormat('zh-CN', { notation: 'compact' })`。
  3. 在 `MobileStudioTopBar`（仅 Studio 才挂这么多右侧元素）做条件：当 `running.any` 时隐藏 ContextWindowMeter，避免一行五图标 + Pill。
  4. `MobileTopBar` 容器 `gap-2.5` 在 <360px 视口降到 `gap-1.5`：`className` 加 `[@media(max-width:360px)]:gap-1.5`。
- **验收**：iPhone SE (375px) 模拟器、ChromeDevTools "Pixel 5" (393px)、`¥9999.99` 余额下右侧图标全部可见且互不遮挡。

### 0.4【P1】桌面端 WalletBalancePill 占用中间 Tabs 的 grid 槽位
- **位置**：`apps/web/src/components/ui/shell/DesktopTopNav.tsx:78-149`
- **现状**：header 是 `grid grid-cols-[auto_minmax(0,1fr)_auto]`，左槽是 Logo+菜单，中间是 Tabs，右槽是 `<WalletBalancePill />` + `right`。Pill 在 `<640px` 窗口下被 `hidden sm:inline-flex` 隐藏，但 sm-md 之间（640-900px）右槽仍可能压缩到底部，把 4 个 Tab 推向左侧导致下划线动画（layoutId `desktop-nav-underline`）抖动。
- **症状**：拖拽缩窗或 Sidebar 折叠/展开瞬间，活动 Tab 下划线会出现 100-200ms 的横向漂移。
- **根因**：右槽 `min-w-0`，但 `WalletBalancePill` 内 `whitespace-nowrap`（隐式）会让其拒绝压缩；右槽宽度增长 → grid 重新分配 → Tabs 收缩。
- **修复方案**：
  - `WalletBalancePill` 加 `shrink-0` 和 `max-w-[140px] truncate`，余额展示用上面 0.3 的紧凑格式。
  - 中间 nav 容器（`DesktopTopNav.tsx:109`）加 `flex-1 justify-center min-w-0`，确保 Tabs 永远居中。
  - 测试：拖拽桌面窗口 640px ↔ 1280px，下划线不抖动。

### 0.5【P1】移动端表格强制 `min-w-[680/720/840/980px]` 引发双滚动
- **位置**：
  - `apps/web/src/app/admin/_panels/BillingPanel.tsx:874-913`（`min-w-[680px]` 尺寸定价表）
  - `apps/web/src/app/admin/_panels/BillingPanel.tsx:959-1028`（`min-w-[840px]` 模型定价表）
  - `apps/web/src/app/admin/_panels/RedemptionPanel.tsx:312-415`（`min-w-[980px]` 兑换码表）
  - `apps/web/src/app/admin/_panels/RedemptionPanel.tsx:436-462`（`min-w-[720px]` 兑换记录）
- **症状**：iPad 9.7" 竖屏（768px）下，表格容器 `overflow-x-auto` 与 admin 页面本身的 `overflow-y-auto` 嵌套，导致：
  - 表头滚动不跟随内容，操作按钮（撤销/记录/撤销批次 4 个按钮）在最右侧需要持续横滑。
  - 横滑时手机浏览器把页面也横向回弹，触感断裂。
- **修复方案（移动端卡片化）**：
  1. 把表格主体抽成 `<TableRow>` 子组件，桌面端走 `<table>`，<md 走 `<div>` 卡片列表（每行垂直堆叠 label + value）。
  2. 用 `globals.css` 已有的 `responsive-data-row` 模式 / 新增一个 `data-stack-on-mobile` 工具类：
     ```css
     @media (max-width: 768px) {
       .data-stack-on-mobile table { display: block; }
       .data-stack-on-mobile thead { display: none; }
       .data-stack-on-mobile tr   { display: grid; grid-template-columns: 1fr; gap: .25rem;
                                    border: 1px solid var(--border-subtle); border-radius: var(--radius-card);
                                    padding: .75rem; margin-bottom: .5rem; }
       .data-stack-on-mobile td   { display: grid; grid-template-columns: 1fr auto; gap: .75rem; }
       .data-stack-on-mobile td::before { content: attr(data-label); color: var(--fg-2); font-size: 12px; }
     }
     ```
     每个 `<td>` 加 `data-label="档位"` 等属性。
  3. 操作按钮在移动端折叠成"⋯ 更多"菜单（用现有 `ActionSheet`）。
- **验收**：iPad mini / Pixel 5 上 Billing → 定价、Redemption 两个表都不出现横向滚动；表头信息以 label 形式出现在每行内。

### 0.6【P2】移动端弹窗未使用 `mobile-dialog-*` 安全区类
- **位置**：`apps/web/src/app/admin/_panels/RedemptionPanel.tsx:471-547`（`NewCodesModal`）
- **现状**：`fixed inset-0 z-[80] flex items-center justify-center bg-black/60 px-4 py-6` → 内部面板 `max-h-[90dvh]`。
- **症状**：
  - iPhone 15 Pro 横屏时 `90dvh` 计算包含 Dynamic Island，footer 提示被 home indicator 遮 4-6px。
  - iOS Safari 工具栏伸缩时 `dvh` 跳变，弹窗会忽然变高引起内容回流。
- **修复方案**：按 `MEMORY.md` Lumen UI 标准强制使用：
  ```tsx
  <div className="fixed inset-0 z-[var(--z-dialog)] bg-black/60 backdrop-blur-sm mobile-dialog-shell flex items-end justify-center sm:items-center">
    <div className="mobile-dialog-panel sm:max-w-3xl ...">
      <div className="mobile-dialog-scroll">…codes…</div>
      <div className="mobile-dialog-footer">关闭后…</div>
    </div>
  </div>
  ```
  这些类已经在 `globals.css` 处理了 safe-area 和 dvh 兼容性。
- **验收**：iPhone 15 Pro 横竖屏切换、Android Chrome 工具栏滑出滑入，弹窗高度稳定且不与刘海/底部 home indicator 重叠。

### 0.7【P2】钱包余额数字宽度抖动
- **位置**：`MobileTopBar.tsx:104-117`、`DesktopTopNav.tsx:181-196`、`wallet/page.tsx:182`、`PromptComposer.tsx:616-619`
- **现状**：余额格式 `Number(rmb).toFixed(2)`，文字 `tabular-nums` 已经加了，但容器宽度不固定。
- **症状**：余额 `¥99.50` → `¥100.00`，Pill 宽度跳 6-8px，连带 TopBar 右侧重排，视觉跳动。
- **修复方案**：余额 Pill 容器加 `inline-block min-w-[72px] sm:min-w-[88px] text-right`；金额本身用 `var(--font-mono)`（已有 `IBMPlexMono` 变量）。

### 0.8【P2】暗色硬编码违规（违反 `docs/frontend-theme-dialog-standards.md`）
- **位置**：
  - `MobileTopBar.tsx:113`：`bg-white/5`
  - `DesktopTopNav.tsx:83,191`：`border-white/[0.04]`、`bg-white/5`
  - `BillingPanel.tsx:105,1087`：`bg-white/[0.04]`、`bg-white/10`
  - `RedemptionPanel.tsx:491,541`：`hover:bg-white/6`、`bg-[var(--bg-1)]/72`
  - `wallet/page.tsx:141,176,287`：`hover:bg-white/4`、`bg-white/5`、`bg-white/10`
  - `SystemUpgradeBanner.tsx:28`：`bg-amber-500/18 text-amber-100`
- **修复方案**：统一改用语义 token：
  - `bg-white/5/10` → `bg-[var(--bg-2)]` 或 `bg-[color-mix(in_srgb,var(--fg-0)_4%,transparent)]`。
  - `border-white/0.04` → `border-[var(--border-subtle)]`。
  - SystemUpgradeBanner 的 amber 配色保留语义（升级提示），但前景色用 `text-[var(--warning-fg)]`，背景用 `bg-warning-soft border-warning-border`，与 OfflineBanner 风格一致。

### 0.9【P2】重复滚动容器导致 sticky 失效
- **位置**：
  - `BillingPanel.tsx:316` `max-h-[360px] divide-y overflow-auto`（审计事件列表）
  - 父 `SettingsShell main` `overflow-y-auto`（`SettingsShell.tsx:46`）
- **症状**：在移动端 admin 页面里，审计列表自己滚动，但同时整页也可滚动；用户上滑想看健康检查卡片时事件容器先消化掉滚动手势，体验"卡顿"。
- **修复方案**：移动端去掉子容器的 `max-h` / `overflow-auto`，让列表沿着外层滚动；只在 `md:` 起恢复 `md:max-h-[360px] md:overflow-auto`。

### 0.10【P3】文本溢出（金额、订单号、邮箱、redemption code）未统一截断
- **位置**：
  - `RedemptionPanel.tsx:339-345`：`max-w-[160/180px] truncate` ✓
  - `RedemptionPanel.tsx:451-452`：邮箱 + ID 两行无统一截断
  - `wallet/page.tsx:336-350`：流水卡片 `grid-cols-[1fr_auto]`，左侧 `min-w-0` 已加但没 truncate
  - `BillingPanel.tsx:367`：`{item.tx.ref_type}:{item.tx.ref_id}` 直出，长 ref_id 撑破
- **修复方案**：所有 ID/邮箱/code 字段统一加 `truncate` 或 `[overflow-wrap:anywhere]`；金额加 `tabular-nums`。

---

## 1. 单文件修复清单

### 1.1 `apps/web/src/components/SystemUpgradeBanner.tsx`
- **L24-34**：见 0.1 / 0.2，重写为：
  ```tsx
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      document.documentElement.style.setProperty(
        "--system-banner-height",
        `${el.offsetHeight}px`,
      );
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => {
      ro.disconnect();
      document.documentElement.style.setProperty("--system-banner-height", "0px");
    };
  }, [data?.running]);
  ```
- **L28**：`z-[90]` → `z-[var(--z-banner,85)]`；类名补 `pt-[env(safe-area-inset-top,0px)]`。
- **L32**：长 phase / 长 target_tag 时 `<span>` 会换行撑高 banner，需加 `truncate max-w-[min(92vw,640px)] mx-auto`，外层 flex 加 `flex-nowrap`。

### 1.2 `apps/web/src/components/ui/shell/MobileTopBar.tsx`
- **L52**：`paddingTop` 改 `calc(env(safe-area-inset-top,0px) + var(--system-banner-height,0px))`。
- **L60**：`zIndex: var(--z-header,10)` 已经 OK，但 banner 出现时需要确保 banner > header。
- **L64**：`max-w-[640px] mx-auto px-3 gap-2.5` → 加 `[@media(max-width:360px)]:gap-1.5`。
- **L66-69**：见 0.3，把 `MobileWalletPill` 用 `<div className="hidden xs:flex">` 条件渲染或者 Studio 页面 right slot 自带隐藏逻辑，避免 Studio 顶栏六图标 + Pill 全显示。
- **L99**：`!wallet?.balance` 判断会让"余额为 0"的用户看不到 Pill，对低余额用户体验不一致。改为 `wallet?.balance == null`（仅 loading 时不显示）。
- **L100-104**：低余额阈值判断 `wallet.balance.micro < wallet.low_balance_threshold.micro` 当余额为负时仍然成立（合规），但负余额应展示 `-¥X.XX`，目前 `toFixed(2)` 已经支持，但样式需要加 `text-[var(--danger-fg)]`。
- **L109-114**：Pill 类名加 `max-w-[88px] truncate shrink-0`。
- **L116**：`¥{balanceText}` 改为 `formatMoneyCompact(wallet.balance.rmb)`（见 0.3 紧凑格式工具）。

### 1.3 `apps/web/src/components/ui/shell/DesktopTopNav.tsx`
- **L83**：`border-white/[0.04]` → `border-[var(--border-subtle)]`，`bg-[var(--bg-0)]/70` 已 OK。
- **L109**：nav 容器加 `flex-1`，避免见 0.4 的 layoutId 抖动。
- **L145-148**：右槽加 `shrink-0`，子项之间 `gap-2`。
- **L182-195**：`WalletBalancePill`：
  - 加 `max-w-[140px] shrink-0 truncate`。
  - 金额用紧凑格式。
  - `border-white/[0.04]`、`bg-white/5` 替换语义 token。

### 1.4 `apps/web/src/components/ui/PromptComposer.tsx`
- **L385**：`z-[60]` → `z-[var(--z-tray,50)]`。
- **L386**：`bottom: calc(var(--composer-bottom,9rem)+env(safe-area-inset-bottom))` 没考虑键盘 offset。改为 `calc(var(--composer-bottom,9rem)+env(safe-area-inset-bottom)+var(--keyboard-offset,0px))`，与 composer 主体一致。
- **L412**：`z-50` → `z-[var(--z-composer,40)]`；同时考虑 banner 时不应被 banner 遮，因为 composer 在底部不会冲突。
- **L413**：`w-[min(calc(100vw-var(--sidebar-w)-1rem),48rem)]` 在 iPhone SE 与 banner 同时显示下 OK；但 `lg:w-[min(...)]` 不会响应 banner 高度，建议加 `max-h-[calc(100dvh-var(--system-banner-height,0px)-2rem)]` 防止键盘 + banner 同时挤压。
- **L614-619** `estimatedCharge` Badge：
  - 长金额（`¥9999.99` 以上）撑破 `text-[11px]` badge：加 `max-w-[148px] truncate`。
  - 与 `SendButton` 之间没有强分隔，工具条 wrap 时 badge 会跑到下一行末尾导致与发送按钮上下错位。改为给 badge 加 `order-2 ml-auto` 或者把 badge 放进 `<div className="ml-auto flex items-center gap-2">` 与 SendButton 同组。
  - 显示 `"价格未配置"` 用 `text-[var(--warning-fg)]`，否则用户以为是普通信息。
- **L605**：`bg-white/10` 分隔竖线改 `bg-[var(--border)]`。
- **L549**：`placeholder:text-neutral-500` → `placeholder:text-[var(--fg-3)]`（暗色硬编码）。

### 1.5 `apps/web/src/components/ui/me/AccountCenter.tsx`
- **L107-121**：钱包/API Key 互斥渲染没问题，但 wallet 模式下用户希望快速看到余额，建议在 `label` 后追加 `<span className="ml-auto tabular-nums text-[var(--fg-2)]">¥{walletBalance}</span>` 子项（需要先查询 wallet）。这只是优化，不影响主修复。
- **L126,133**：`badge={stagingCount}` / `badge={promptCount}` 当 ≥100 时会撑破 AccountRow 右侧的圆角徽章。`AccountRow` 内部需对 `badge` 做 `Math.min(99, n)` + `"+"` 后缀。
- **L154-167**：admin 入口在底部，wallet 模式用户找钱包要滚很久。建议在第一个 group 把"钱包"放到第二位（"用量统计" 之后）。
- **L33**：`APP_VERSION = "v1.0.47"` 与实际 VERSION 文件不符。修复时同步从 `VERSION` 注入或 build-time 替换。

### 1.6 `apps/web/src/app/me/wallet/page.tsx`
- **L131-148** BYOK 分支：返回的 `SettingsShell` 内只有一段说明，没复用任何 wallet 相关 UI，但 BYOK 用户依然能在 MobileTopBar 看到 wallet pill（pricingQuery 拒绝渲染逻辑会兜底）—— 这里实际 OK。
- **L153-165** 桌面端 header 是 `hidden md:flex`，移动端没有"返回我的"按钮。`SettingsShell` 已经渲染了 MobileTopBar 显示标题，但缺返回箭头。建议给 `MobileTopBar` 的 left 传入返回按钮，或在 `SettingsShell` 增加 `backHref` 参数。
- **L167-171** 低余额条幅在桌面端是占满宽的卡片，移动端也是。需加 `mx-0` 在 wrapper（mobile 已经被父 `safe-x` 控制 padding），实测 OK，但建议加 `flex items-center gap-2` + `<AlertTriangle />` 图标，符合 Lumen 通知风格。
- **L173** `grid gap-4 md:grid-cols-[1fr_1.2fr]`：sm-md 之间（640-768px）单列堆叠，但 `md` 起两列时兑换码输入框 `h-11` 与左侧余额卡片高度不一致，右下边对不齐。修复：兑换 `<form>` 改 `grid-rows-[auto_1fr_auto]` 或 `<Card>` 包装统一最小高度 `min-h-[180px]`。
- **L209** 兑换码输入 `tracking-[0.08em]` 在移动端导致 19 字符占满后挤出占位符 → placeholder 截断。改 `tracking-[0.06em]` 并 `text-base sm:text-lg` 动态。
- **L268-301** 限额窗口 `grid md:grid-cols-3`，<md 单列时进度条卡片高度不齐（重置时间长短不一）；加 `min-h-[112px]`。
- **L317-333** 流水筛选 chips：6 个 chip + `flex-wrap`，移动端会换行，整体高度变化导致下方流水列表跳动。改 `overflow-x-auto scrollbar-thin` + `flex-nowrap`，左右滑动 chips。
- **L336** 流水行 `grid grid-cols-[1fr_auto]` 没有 `truncate`：长 `tx.kind`（如 "topup_redeem"）已 `formatKind`，但若新增 kind 未在 map 中（如 `charge_completion_priority`）会原样输出超长字符串。给左 `<p>` 加 `truncate`。
- **L373-426** "兑换历史"卡片同上，`code_id` 长字符串需要 `truncate` 或 `break-all`。

### 1.7 `apps/web/src/app/admin/_panels/BillingPanel.tsx`
- **L104-125** Tabs 横向滚动 + `inline-flex`：移动端 4 个 tab 是 OK 的，但聚焦 tab 在右侧时不会自动 scroll into view。建议在 `setTab` 后用 `scrollIntoView({ inline: "center" })`。
- **L241-260** 健康检查 `grid md:grid-cols-3`：<md 单列时 `truncate` 的 `value` 仍会撑破（"缺少 1k, 2k" 等）。给 `<span>` 加 `whitespace-normal break-words`，去掉 `truncate`。
- **L289-310** MetricCard 网格 `md:grid-cols-4`：<md 单列，金额会被卡片宽度撑过 64px，应该 sm:grid-cols-2 中等屏先两列。改：`grid gap-3 sm:grid-cols-2 md:grid-cols-4`。
- **L316** `max-h-[360px] overflow-auto` → 见 0.9，加 `md:` 前缀。
- **L318** 行 `md:grid-cols-[180px_220px_1fr]`：长 event_type（如 `wallet_audit_mismatch_user_xxx`）撑破 220px 固定列。改为 `auto auto 1fr` 或 `flex flex-wrap`。
- **L363-385** 孤儿 hold 行同 0.10：`item.tx.ref_id` 需 truncate。
- **L720-754** "全局设置" `grid md:grid-cols-5`：5 列在 768-1024px 之间挤压严重，输入框高度被压成 36px 看不清。改 `grid gap-3 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-5`。
- **L755-788** "兑换码 secret" 卡片：`<Button class="w-full md:w-auto">` 在 sm 起会撑满整行下方留白；应 `flex flex-wrap` 配合 button `sm:w-auto`。当前 `w-full md:w-auto` 写在 wrapper 而非 button 上，实际 button 仍是 `sm:w-auto`，但 wrapper `w-full md:w-auto` 让整行变窄，需要重写。
- **L820-823** "模型 / channel" 两输入 `md:grid-cols-[1fr_180px]`：<md 单列正常，但移动端没有 label 暗示哪个是 model 哪个是 channel。建议 `<label>` 包裹并显示标题。
- **L874-913** 见 0.5 表格卡片化。
- **L915-937** 新增档位行 `md:grid-cols-[1fr_1fr_auto]`：<md 单列时按钮宽度撑满会显得突兀，应 `sm:flex-row`。
- **L959-1028** 同 0.5。
- **L1030-1053** 价目导入 textarea：`md:grid-cols-[1fr_120px_auto]` 在 <md 单列时 USD→RMB 输入框紧贴 textarea 下方，没有 label 提示输入率；加 `<label>` + caption。
- **L1085-1097** SwitchField：`relative h-5 w-9` 的 thumb 容器 `bg-white/10` 改 `bg-[var(--bg-2)]`；thumb `bg-[var(--bg-0)]` 在浅色主题下会与背景同色看不见，改 `bg-[var(--fg-0)]/90` 或 `var(--bg-elev)`。

### 1.8 `apps/web/src/app/admin/_panels/RedemptionPanel.tsx`
- **L196** "批量发码" `grid md:grid-cols-2`：sm 起两列时长 label（"每码最大兑换次数"）会让左右两列输入框基线不齐。统一 label 用 `min-h-[16px]` 或 `<span>` 等高。
- **L234-239** 备注输入与上面网格不在同一 grid：备注独立 `h-10 w-full`，移动端 OK，桌面端建议 `md:max-w-[480px]`。
- **L240-257** 创建按钮区：`flex flex-wrap items-center justify-between`，<sm 折行时按钮居左、价值文案居右，视觉颠倒。改 `flex-col-reverse sm:flex-row` 让按钮始终在 CTA 位置。
- **L273-291** Status filter chips：与 0.10 / wallet 流水 chips 同样改 `overflow-x-auto flex-nowrap`。
- **L312-414** 兑换码表，见 0.5 卡片化。补充：
  - **L329** 面额单位 `¥` 没 `tabular-nums`。
  - **L331** `{redeemed_count}/{max_redemptions}` 两数字间没空格，长数字粘连。
  - **L344** `max-w-[180px] truncate` 备注：移动端表格本来就横滚，180px 是绝对值，移动端可能超过单元格宽度。卡片化后改用 `line-clamp-2`。
  - **L347** 操作按钮列 `text-right`：5 个按钮（前缀/重新查看/撤销/记录/撤销批次）会换行错位。移动端必须折叠为 ActionSheet。
- **L428-464** "兑换记录" 子卡片 `min-w-[720px]`：同 0.5。
- **L471-547** `NewCodesModal`：
  - **L480** 见 0.6 改用 `mobile-dialog-shell`。
  - **L491** `hover:bg-white/6` 改 token。
  - **L526** `min-h-0 flex-1 overflow-auto p-5`：内部 codes 列表本身可滚，但外层 panel 也 `max-h-[90dvh]`，iOS Safari 工具栏伸缩时会双滚。改为只让内部滚。
  - **L528-538** code 行 `grid grid-cols-[1fr_auto] break-all`：`break-all` 让 LMN-XXXX-XXXX-XXXX-XXXX 在每个 `-` 之间断行（有 4 段时会拆成 4 行）。改用 `[overflow-wrap:anywhere]` 或 `font-mono whitespace-nowrap overflow-x-auto`（让单行水平滚动而非垂直撑高）。
  - **L541** 底部说明 `text-xs text-[var(--fg-2)]` + 浅色背景，没有 footer 类样式。改用 `mobile-dialog-footer` 类。
- **L549+** `UserWalletsSubpanel`（用户钱包子面板，需读全文件再列细节，常见同类问题：筛选 chip 折行、表格强宽、调账按钮分组、`text-xs` 在移动端过小）。

### 1.9 `apps/web/src/components/ui/chat/GenerationView.tsx`
- 整体计费相关只是 `isFreeGeneration` 工具函数；UI 改动很小。
- **L105-110**：`<p>` truncate + `<StageTicker>` 同行，在 <360px 屏 StageTicker 文案（"理解中 12s"）会与 prompt 重叠。改为 `flex-col sm:flex-row`，移动端纵向堆叠。
- **L131-134**：`md:text-xs` 在桌面端字号过小，删除；保留 `text-sm` 即可。
- **L201-211** `free` badge：`absolute top-2 z-10` 在 ordinal 存在时 `left-10`，但 cancel 按钮也在 `top-2 left-2`，三者同行时仍可能重叠（cancel + ordinal + free）。修改：`free` badge 改 `bottom-2 left-2`，保持图标区域三角化布局。

### 1.10 `apps/web/src/components/ui/chat/desktop/DesktopConversationCanvas.tsx`
- 改动仅是 `isFreeGeneration` 判定；UI 无新增。但需检查：
- **L244** `HistoryLoadControl` `z-[1]`：SystemUpgradeBanner 出现时此 sentinel 完全被 banner 覆盖，loadMore 仍工作但 error 提示用户看不到。改 `z-10` 并 `sticky top-[calc(var(--system-banner-height,0)+8px)]`。

### 1.11 `apps/web/src/components/ui/chat/mobile/MobileConversationCanvas.tsx`
- 同 1.10，sentinel 需要避让 banner。
- 长消息 `[overflow-wrap:anywhere]` 是否已应用需检查每个 MessageBubble 组件（不在本次 commit 范围，跳过）。

### 1.12 `apps/web/src/components/admin/UpdateAvailableCard.tsx`
- 整文件多处暗色硬编码：`text-neutral-100/400/500`、`bg-neutral-900` 等。统一换 `--fg-0/1/2`、`--bg-1/2`。
- Badge 区 `flex` 没 `flex-wrap`：版本号 / channel / size 三标签在窄屏会溢出，需加 `flex-wrap gap-1`。

### 1.13 `apps/web/src/components/ui/projects/library/ModelLibraryBrowser.tsx` & `ModelLibraryJobsPanel.tsx`
- 这两个文件被计费 commit 修改主要为加入"剩余余额校验"按钮 / 提示。重点检查：
  - 按钮新增后 grid 是否拥挤；当 wallet 不足时是否有 disabled 状态的视觉一致性。
  - `grid-cols-2 gap-2 self-start min-[420px]:flex` 在 <420px 强制 2 列：长按钮文案会换行。改 `grid-cols-1 min-[360px]:grid-cols-2 min-[420px]:flex`。

### 1.14 `apps/web/src/app/settings/api-key/page.tsx`
- commit 增加了一段说明（BYOK vs Wallet 模式选择）。需要验证：
  - 在 wallet 账号下访问 `/settings/api-key` 是否会被钱包 Pill / 引导横幅遮挡。
  - 说明文案是否在移动端换行错位（建议 `max-w-prose`）。

### 1.15 `apps/web/src/app/layout.tsx`
- **L192** `<SystemUpgradeBanner />` 渲染顺序在 `<Lightbox />` / `<InpaintModal />` / `<GlobalTaskTray />` 之后；只要 z-index 正确，DOM 顺序不影响。但 `<OfflineBanner />`（L193）紧随其后，两个 banner 同时出现时会上下重叠（都是 `fixed top-0`），离线时升级 banner 完全被 OfflineBanner 盖住。
  - **修复**：OfflineBanner 出现时让 SystemUpgradeBanner 让出顶部；用 `:has()` 或者把两者合并到一个 `BannerStack` 组件，按优先级（offline > upgrade）渲染。

---

## 2. 修复实施顺序（建议分 4 个 PR）

### PR-1：全局 z-index / safe-area / banner-height 基础设施
- `globals.css`：新增 `--z-banner`、`--system-banner-height`、紧凑金额工具（CSS 部分）。
- `SystemUpgradeBanner.tsx`：暴露高度变量、改 z-index。
- `OfflineBanner.tsx`：加 banner 协调逻辑。
- `MobileTopBar.tsx` / `DesktopTopNav.tsx`：消费 `--system-banner-height`。
- 验收脚本：手动开启 `/admin/system/maintenance` 模拟升级，截图所有页面 TopBar 不被遮。

### PR-2：钱包余额 Pill / 紧凑格式 / Tabs 抖动
- 新增 `lib/format/money.ts`：`formatMoneyCompact`、`formatMoneyExact`。
- `MobileWalletPill` / `WalletBalancePill` / `wallet/page.tsx` / `PromptComposer estimatedCharge`：统一用紧凑格式 + 固定最小宽度。
- `DesktopTopNav` Tabs 抖动修复。
- 验收：Chrome DevTools 切换余额 mock 数据，TopBar 无重排；窗口拖拽下划线稳定。

### PR-3：admin 表格移动端卡片化
- `globals.css` 加 `data-stack-on-mobile` 工具类。
- `BillingPanel.tsx`（尺寸/模型定价表）、`RedemptionPanel.tsx`（兑换码/兑换记录表）改造。
- 移动端操作按钮折叠为 ActionSheet。
- 验收：iPad mini 竖屏、Pixel 5 竖屏 admin 所有表格无横向滚动，所有操作可达。

### PR-4：弹窗与杂项
- `NewCodesModal` 改用 `mobile-dialog-*` 体系。
- 暗色硬编码统一替换。
- Tabs / chip 横滑改造（wallet 流水筛选、Redemption 状态筛选）。
- AccountCenter 钱包入口顺序调整 + 余额预览。
- 验收：`apps/web` 下 `npm run type-check && npm run lint && npm run build` 通过；`git diff --check` 无空格问题。

---

## 3. 自动化验收清单

### 3.1 视觉回归（手动）
| 场景 | 设备 | 期望 |
| --- | --- | --- |
| 升级 banner + Studio 输入 | iPhone SE / Pixel 5 / iPad mini | TopBar、Composer、TabBar 不被遮 |
| 升级 banner + admin 发码弹窗 | iPad mini | 弹窗在 banner 之上，关闭按钮可见 |
| 钱包余额从 99.50 → 100.00 | DevTools mock | Pill 无宽度跳动 |
| `¥99999.99` 极大余额 | iPhone SE | Pill 紧凑显示 `¥99.9k`，TopBar 不溢出 |
| wallet/page.tsx 在 iPhone SE | iPhone SE | 卡片不堆叠错位、chips 单行滑动 |
| admin 兑换码表 200 行 | iPad mini 竖屏 | 卡片列表无横向滚动 |
| Composer estimatedCharge 极大金额 | iPhone SE | badge 不溢出工具条，不与 Send 上下错位 |
| OfflineBanner + SystemUpgradeBanner 同时触发 | DevTools | 仅 OfflineBanner 显示在最上层 |
| 极长 prompt + StageTicker | iPhone SE | prompt 截断与 ticker 上下堆叠不重叠 |

### 3.2 静态扫描
```bash
# 1. 暗色硬编码扫描（应 0 命中或全部归类为 媒体/lightbox/scrim）
rg "bg-(white|neutral-9\d\d|black)|text-white|border-white" \
   apps/web/src/app/admin/_panels apps/web/src/app/me \
   apps/web/src/components/SystemUpgradeBanner.tsx \
   apps/web/src/components/ui/shell/MobileTopBar.tsx \
   apps/web/src/components/ui/shell/DesktopTopNav.tsx \
   apps/web/src/components/ui/PromptComposer.tsx

# 2. 硬编码 z-index 扫描（除特殊场景外都应改 var）
rg "z-\[\d+\]|z-(40|50|60|80|90|100)" apps/web/src

# 3. 缺失 mobile-dialog-* 的 fixed 弹窗
rg "fixed inset-0" apps/web/src | grep -v "mobile-dialog-shell"

# 4. lint / type-check / build
cd apps/web && npm run type-check && npm run lint && npm run build
```

### 3.3 用户路径回归
1. Wallet 模式新用户：打开 `/` → 看到余额 Pill → 进入 `/me/wallet` → 兑换码输入 → 成功提示。
2. Admin：进入 `/admin` → 切换到「计费 → 兑换码」→ 创建 10 张 → 弹窗复制/下载 → 撤销批次。
3. 升级中：管理员触发 `/api/admin/system/maintenance`，普通用户首页应同时看到升级 banner + 余额 Pill + composer，三者互不遮挡。

---

## 4. 风险与注意事项

1. **`--system-banner-height` 由客户端 useEffect 写入**：SSR 首屏到 hydration 之间 banner 高度为 0，可能首帧出现 TopBar 闪烁。若关键，可在 `<head>` 内联一段判定脚本（读取 cookie `lumen-upgrade` 或 SSR 时 prefetch maintenance 状态）。
2. **紧凑金额格式可能误导**：从 `¥1234.56` 简写为 `¥1.2k` 会失去精度。Pill 加 `title` 显示完整金额，并在 `/me/wallet` 页保留 `.toFixed(2)`。
3. **表格卡片化的 `data-label` 属性**：需要在每个 `<td>` 上手写 `data-label`，未来新增列容易漏。建议封装 `<DataCell label="…">` 子组件。
4. **OfflineBanner 与 SystemUpgradeBanner 合并**：要避免重复 `useQuery` 订阅，统一在父 `BannerStack` 内决策优先级。
5. **AccountCenter L33 `APP_VERSION` 硬编码**：与本次修复无直接关系，但顺手处理；从 `process.env.NEXT_PUBLIC_APP_VERSION` 或 build-time `define` 注入。

---

## 5. 后续改进（不在本次 fix 范围）

- 把"低余额"提示从 PromptComposer / TopBar 之外，独立为一个全局轻量 toast（首次进入 + 失败请求都触发）。
- 钱包数据加入 `staleTime` + `refetchOnFocus` 之外的 SSE 推送，避免余额过期。
- 设计 token：`--z-banner` / `--z-system-banner` / `--z-net-banner` 分层（升级 vs 离线 vs 维护通知）。
- 在 `docs/frontend-theme-dialog-standards.md` 补充本文 0.x 提到的 banner 高度与 z-index 规范。

---

文档生成时间：2026-05-18 · 涉及 commits：c604858 / 6b66f86 / 056205f / 4771eaf · 总计 16 个核心文件、10 个全局问题、80+ 单文件细项。
