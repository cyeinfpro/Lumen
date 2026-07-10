"use client";

// 桌面端顶部主导航：复用在 DesktopStudio / DesktopStream / DesktopMe。
// 四 Tab 横向导航 + 左侧 Logo + 可配置右侧 slot。
// 路由契约集中在 navigation.ts，桌面顶部与移动底栏共用同一套 IA。

import { motion } from "framer-motion";
import { Menu, Search } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useMemo, useRef, type KeyboardEvent, type ReactNode } from "react";

import { LumenMark } from "@/components/ui/brand/LumenMark";
import { IconButton, Kbd } from "@/components/ui/primitives";
import { TaskIsland } from "@/components/ui/tray/TaskIsland";
import { SPRING } from "@/lib/motion";
import { useUiStore } from "@/store/useUiStore";
import { DesktopAccountMenu } from "./DesktopAccountMenu";
import {
  getActiveNavKey,
  getAppNavItems,
  getFirstVisibleNavRoute,
  isSameRoute,
  type AppNavItem,
  type AppNavKey,
} from "./navigation";

export type DesktopNavTab = AppNavKey;

export interface DesktopTopNavProps {
  active: DesktopNavTab;
  right?: ReactNode;
  onToggleSidebar?: () => void;
}

export function DesktopTopNav({ active, right, onToggleSidebar }: DesktopTopNavProps) {
  const pathname = usePathname();
  const router = useRouter();
  const navVisibility = useUiStore((s) => s.navVisibility);
  const navItems = useMemo(
    () => getAppNavItems(navVisibility).filter((item) => item.key !== "me"),
    [navVisibility],
  );
  const homeHref = useMemo(
    () => getFirstVisibleNavRoute(navVisibility),
    [navVisibility],
  );

  const currentActive: DesktopNavTab = useMemo(() => {
    return getActiveNavKey(pathname, navVisibility) ?? active;
  }, [pathname, active, navVisibility]);

  const onTap = useCallback(
    (tab: AppNavItem) => {
      if (isSameRoute(pathname, tab.route)) return;
      router.push(tab.route);
    },
    [pathname, router],
  );

  const tabsRef = useRef<HTMLUListElement | null>(null);
  const onTabsKeyDown = useCallback((e: KeyboardEvent<HTMLUListElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const root = tabsRef.current;
    if (!root) return;
    const buttons = Array.from(root.querySelectorAll<HTMLButtonElement>("button"));
    if (buttons.length === 0) return;
    const idx = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const base = idx < 0 ? 0 : idx;
    const delta = e.key === "ArrowRight" ? 1 : -1;
    const next = (base + delta + buttons.length) % buttons.length;
    e.preventDefault();
    buttons[next]?.focus();
  }, []);

  return (
    <header
      className={[
        "sticky top-0 grid h-[52px] w-full items-center gap-3 px-4 md:px-6",
        "grid-cols-[auto_minmax(0,1fr)_auto]",
        "border-b border-[var(--border-subtle)] bg-[var(--bg-0)]/92 backdrop-blur-lg",
      ].join(" ")}
      style={{
        top: "var(--system-banner-height, 0px)",
        zIndex: "var(--z-header, 10)",
      }}
    >
      {/* Left: sidebar toggle + Logo */}
      <div className="flex min-w-0 items-center gap-2.5 md:gap-3">
        {onToggleSidebar && (
          <IconButton
            size="md"
            aria-label="切换侧栏"
            title="切换侧栏 (⌘/Ctrl+B)"
            tooltip="切换侧栏 (⌘/Ctrl+B)"
            onClick={onToggleSidebar}
            className="rounded-[var(--radius-control)]"
          >
            <Menu className="h-[18px] w-[18px]" />
          </IconButton>
        )}
        <Link
          href={homeHref}
          className="inline-flex h-10 shrink-0 items-center gap-2 rounded-[var(--radius-control)] px-1.5 text-[var(--fg-0)] transition-colors hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
          aria-label="Lumen 首页"
        >
          <LumenMark className="text-[var(--accent)]" />
          <span className="hidden text-[15px] font-semibold tracking-normal text-[var(--fg-0)] sm:inline">
            Lumen
          </span>
        </Link>
      </div>

      {/* Middle: Tabs — 占据中间 1fr，居中显示，可挤压 */}
      <nav aria-label="主导航" className="flex min-w-0 flex-1 justify-center overflow-hidden">
        <ul
          ref={tabsRef}
          onKeyDown={onTabsKeyDown}
          className="flex items-center gap-1"
        >
          {navItems.map((tab) => {
            const isActive = tab.key === currentActive;
            return (
              <li key={tab.key} className="relative">
                {/* 顶部导航 Tab：内嵌 layoutId 动画下划线 + tabsRef 键盘导航需要原生 button */}
                <button
                  type="button"
                  onClick={() => onTap(tab)}
                  aria-current={isActive ? "page" : undefined}
                  className={[
                    "relative inline-flex h-9 cursor-pointer items-center px-3 text-[13px] font-medium leading-none transition-colors whitespace-nowrap",
                    "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                    isActive
                      ? "text-[var(--fg-0)]"
                      : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
                  ].join(" ")}
                >
                  {isActive && (
                    <motion.span
                      layoutId="desktop-nav-active"
                      aria-hidden
                      className="absolute inset-x-3 bottom-0 h-0.5 rounded-full bg-[var(--accent)]"
                      transition={SPRING.snap}
                    />
                  )}
                  <span className="relative z-10">{tab.label}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* Right: slot */}
      <div className="flex min-w-0 shrink-0 items-center justify-end gap-2 text-sm text-[var(--fg-2)]">
        {right ? (
          <div className="flex min-w-0 items-center gap-2">{right}</div>
        ) : null}
        <button
          type="button"
          onClick={() =>
            window.dispatchEvent(
              new CustomEvent("lumen:command-palette-open"),
            )
          }
          aria-label="打开命令面板"
          className="hidden min-h-10 items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-2.5 text-[12px] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:shadow-[var(--ring)] lg:inline-flex"
        >
          <Search className="h-4 w-4" aria-hidden />
          <span className="hidden 2xl:inline">搜索</span>
          <Kbd className="hidden 2xl:inline-flex">⌘K</Kbd>
        </button>
        <IconButton
          size="md"
          aria-label="打开命令面板"
          tooltip="命令面板 (⌘/Ctrl+K)"
          onClick={() =>
            window.dispatchEvent(
              new CustomEvent("lumen:command-palette-open"),
            )
          }
          className="rounded-[var(--radius-control)] lg:hidden"
        >
          <Search className="h-4 w-4" aria-hidden />
        </IconButton>
        <TaskIsland compact />
        <DesktopAccountMenu />
      </div>
    </header>
  );
}
