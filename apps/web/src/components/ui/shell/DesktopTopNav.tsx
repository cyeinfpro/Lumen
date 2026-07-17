"use client";

// 桌面端顶部主导航：复用在 DesktopStudio / DesktopStream / DesktopMe。
// 四 Tab 横向导航 + 左侧 Logo + 可配置右侧 slot。
// 路由契约集中在 navigation.ts，桌面顶部与移动底栏共用同一套 IA。

import { motion } from "framer-motion";
import { Menu, Search } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  useMemo,
  type ReactNode,
  type Ref,
} from "react";

import { LumenMark } from "@/components/ui/brand/LumenMark";
import { IconButton } from "@/components/ui/primitives";
import { TaskIsland } from "@/components/ui/tray/TaskIsland";
import { SPRING } from "@/lib/motion";
import { useUiStore } from "@/store/useUiStore";
import { DesktopAccountMenu } from "./DesktopAccountMenu";
import {
  getActiveNavKey,
  getAppNavItems,
  getFirstVisibleNavRoute,
  type AppNavItem,
  type AppNavKey,
} from "./navigation";

export type DesktopNavTab = AppNavKey;

export interface DesktopTopNavProps {
  active: DesktopNavTab;
  right?: ReactNode;
  onToggleSidebar?: () => void;
  sidebarTriggerRef?: Ref<HTMLButtonElement>;
  sidebarExpanded?: boolean;
}

export function DesktopTopNav({
  active,
  right,
  onToggleSidebar,
  sidebarTriggerRef,
  sidebarExpanded = false,
}: DesktopTopNavProps) {
  const pathname = usePathname();
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

  return (
    <header
      className={[
        "adaptive-material sticky top-0 grid h-[var(--appbar-h)] w-full items-center gap-2 px-3 md:px-5",
        "grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]",
        "border-b border-[var(--border-subtle)] bg-[var(--bg-0)]/96 backdrop-blur-lg",
      ].join(" ")}
      style={{
        top: "var(--top-banner-stack-height, 0px)",
        zIndex: "var(--z-header, 10)",
      }}
    >
      {/* Left: sidebar toggle + Logo */}
      <div className="flex min-w-0 max-w-full items-center gap-2 justify-self-start">
        {onToggleSidebar && (
          <IconButton
            ref={sidebarTriggerRef}
            size="md"
            aria-label={sidebarExpanded ? "收起会话侧栏" : "打开会话侧栏"}
            aria-expanded={sidebarExpanded}
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
          <span className="hidden type-card-title min-[1120px]:inline">
            Lumen
          </span>
        </Link>
      </div>

      <nav
        aria-label="主导航"
        data-testid="desktop-primary-nav"
        className="justify-self-center"
      >
        <ul className="flex items-center gap-1">
          {navItems.map((tab) => (
            <li key={tab.key} className="relative">
              <DesktopNavLink tab={tab} active={tab.key === currentActive} />
            </li>
          ))}
        </ul>
      </nav>

      {/* Right: slot */}
      <div className="flex min-w-0 max-w-full items-center justify-end gap-1.5 justify-self-end text-[var(--fg-2)]">
        {right ? (
          <div className="flex min-w-0 items-center gap-2">{right}</div>
        ) : null}
        <IconButton
          size="md"
          aria-label="打开命令面板"
          tooltip="命令面板 (⌘/Ctrl+K)"
          onClick={() =>
            window.dispatchEvent(
              new CustomEvent("lumen:command-palette-open"),
            )
          }
          className="rounded-[var(--radius-control)]"
        >
          <Search className="h-4 w-4" aria-hidden />
        </IconButton>
        <TaskIsland compact />
        <DesktopAccountMenu />
      </div>
    </header>
  );
}

function DesktopNavLink({
  tab,
  active,
  onNavigate,
}: {
  tab: AppNavItem;
  active: boolean;
  onNavigate?: () => void;
}) {
  return (
    <Link
      href={tab.route}
      onClick={onNavigate}
      aria-current={active ? "page" : undefined}
      className={[
        "type-nav relative inline-flex h-10 cursor-pointer items-center whitespace-nowrap px-2.5 transition-colors min-[960px]:px-3",
        active
          ? "text-[var(--fg-0)]"
          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
      ].join(" ")}
    >
      {active ? (
        <motion.span
          layoutId="desktop-nav-active"
          aria-hidden
          className="absolute inset-x-3 bottom-0 h-0.5 rounded-full bg-[var(--accent)]"
          transition={SPRING.snap}
        />
      ) : null}
      <span className="relative z-10">{tab.label}</span>
    </Link>
  );
}
