"use client";

// MobileShell · spec §3.1 ~ §3.6
// 通用移动端外壳组合：LandscapeBanner + TopBar slot + main + 底部 TabBar。
// 具体 Tab（Studio / Stream / Me）在业务组件内直接组合这些原子件，
// 也可以用 <MobileShell topBar={...}>...</MobileShell> 快速搭骨架。
//
// 注意：
//   - 顶部放置 data-topbar-sentinel，供 MobileTopBar 判断玻璃化
//   - main 自带 safe-composer / safe-tabbar padding 二选一
//   - TabBar 始终在最底；Lightbox 打开时会自动 fade-out（由 TabBar 自判）

import type { ReactNode } from "react";
import { LandscapeBanner } from "./LandscapeBanner";
import { MobileTabBar } from "./MobileTabBar";

export interface MobileShellProps {
  topBar: ReactNode;
  children: ReactNode;
  /** 底部留白策略：tabbar（只给 TabBar 让位）/ composer（给 Pill + TabBar）/ none */
  bottomInset?: "tabbar" | "composer" | "none";
  /** 是否显示横屏 banner（默认 true） */
  showLandscapeBanner?: boolean;
  className?: string;
  /** 额外浮层（例如 Composer Pill），落在 TabBar 之下、main 之上 */
  overlay?: ReactNode;
}

function insetStyle(mode: "tabbar" | "composer" | "none") {
  if (mode === "tabbar") {
    return {
      paddingBottom: "var(--mobile-tabbar-height)",
    } as const;
  }
  if (mode === "composer") {
    return {
      paddingBottom:
        "var(--bottom-overlay-stack, calc(var(--mobile-tabbar-height) + var(--mobile-composer-height, var(--mobile-topbar-h)) + var(--space-3)))",
    } as const;
  }
  return undefined;
}

export function MobileShell({
  topBar,
  children,
  bottomInset = "tabbar",
  showLandscapeBanner = true,
  className = "",
  overlay,
}: MobileShellProps) {
  return (
    <div
      data-app-viewport
      className={[
        "relative flex min-h-0 flex-col bg-[var(--bg-0)]",
        className,
      ].join(" ")}
    >
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      {showLandscapeBanner && <LandscapeBanner />}
      {topBar}

      <main
        data-app-scroll
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
        style={insetStyle(bottomInset)}
      >
        <div className="mx-auto max-w-[640px] px-4 pt-1">{children}</div>
      </main>

      {overlay}
      <MobileTabBar />
    </div>
  );
}
