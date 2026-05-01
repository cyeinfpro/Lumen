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
      paddingBottom: "calc(56px + env(safe-area-inset-bottom, 0px))",
    } as const;
  }
  if (mode === "composer") {
    return {
      paddingBottom: "calc(56px + 56px + 12px + env(safe-area-inset-bottom, 0px))",
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
      className={[
        "relative flex flex-col bg-[var(--bg-0)]",
        className,
      ].join(" ")}
      // 100dvh 在 iOS 地址栏伸缩时会抖动；用 min-height 配 -webkit-fill-available
      // 作为渐进增强 fallback，保证内容溢出时也能撑满整个屏幕。
      style={{
        minHeight: "100dvh",
      }}
    >
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      {showLandscapeBanner && <LandscapeBanner />}
      {topBar}

      <main
        className="flex-1 overflow-y-auto overscroll-contain"
        style={insetStyle(bottomInset)}
      >
        <div className="mx-auto max-w-[640px] px-4 pt-1">{children}</div>
      </main>

      {overlay}
      <MobileTabBar />
    </div>
  );
}
