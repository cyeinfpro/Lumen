"use client";

// 桌面端顶部主导航：复用在 DesktopStudio / DesktopStream / DesktopMe。
// 三 Tab 横向导航 + 左侧 Logo + 可配置右侧 slot。
// 与 MobileTabBar 的路由契约保持一致：/ → 创作；/projects → 项目；/stream → 图库；/me、/settings → 我的。

import { motion } from "framer-motion";
import { Menu } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useMemo, type ReactNode } from "react";

import { SPRING } from "@/lib/motion";

export type DesktopNavTab = "studio" | "projects" | "stream" | "me";

interface TabDef {
  key: DesktopNavTab;
  label: string;
  route: string;
}

const TABS: TabDef[] = [
  { key: "studio", label: "创作", route: "/" },
  { key: "projects", label: "项目", route: "/projects" },
  { key: "stream", label: "图库", route: "/stream" },
  { key: "me", label: "我的", route: "/me" },
];

export interface DesktopTopNavProps {
  active: DesktopNavTab;
  right?: ReactNode;
  onToggleSidebar?: () => void;
}

export function DesktopTopNav({ active, right, onToggleSidebar }: DesktopTopNavProps) {
  const pathname = usePathname();
  const router = useRouter();

  const currentActive: DesktopNavTab = useMemo(() => {
    if (pathname === "/" || pathname === "") return "studio";
    if (pathname.startsWith("/projects")) return "projects";
    if (pathname.startsWith("/stream")) return "stream";
    if (pathname.startsWith("/me") || pathname.startsWith("/settings")) return "me";
    return active;
  }, [pathname, active]);

  const onTap = useCallback(
    (tab: TabDef) => {
      if (tab.key === currentActive) return;
      router.push(tab.route);
    },
    [currentActive, router],
  );

  return (
    <header
      className={[
        "sticky top-0 z-30 w-full h-11 flex items-center justify-between px-3 md:px-5",
        "backdrop-blur-xl bg-[var(--bg-0)]/70 border-b border-white/[0.04]",
      ].join(" ")}
    >
      {/* Left: sidebar toggle + Logo */}
      <div className="flex items-center gap-3 md:gap-4 min-w-0">
        {onToggleSidebar && (
          <button
            type="button"
            onClick={onToggleSidebar}
            aria-label="切换侧栏"
            title="切换侧栏 (⌘K)"
            className={[
              "inline-flex items-center justify-center w-8 h-8 rounded-full",
              "text-[var(--fg-2)] hover:text-[var(--fg-0)] hover:bg-white/8",
              "cursor-pointer active:scale-[0.94] transition-all duration-150",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
            ].join(" ")}
          >
            <Menu className="w-4.5 h-4.5" />
          </button>
        )}
        <Link href="/" className="flex items-center gap-2 shrink-0" aria-label="Lumen 首页">
          <div className="w-5 h-5 rounded-full bg-gradient-to-tr from-[var(--amber-400)] to-orange-200 shadow-[var(--shadow-amber)]" />
          <span className="text-[13px] font-medium tracking-tight text-[var(--fg-0)]">Lumen</span>
        </Link>
      </div>

      {/* Middle: Tabs */}
      <nav aria-label="主导航" className="absolute left-1/2 -translate-x-1/2">
        <ul className="flex items-center gap-1">
          {TABS.map((tab) => {
            const isActive = tab.key === currentActive;
            return (
              <li key={tab.key} className="relative">
                <button
                  type="button"
                  onClick={() => onTap(tab)}
                  aria-current={isActive ? "page" : undefined}
                  className={[
                    "relative px-3 py-1.5 rounded-md text-[13px] font-medium transition-colors cursor-pointer",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                    isActive
                      ? "text-[var(--fg-0)]"
                      : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
                  ].join(" ")}
                >
                  {tab.label}
                  {isActive && (
                    <motion.span
                      layoutId="desktop-nav-underline"
                      aria-hidden
                      className="absolute left-2 right-2 -bottom-[9px] h-0.5 rounded-full bg-[var(--amber-400)] shadow-[var(--shadow-amber)]"
                      transition={SPRING.snap}
                    />
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* Right: slot */}
      <div className="flex items-center gap-1.5 text-sm text-[var(--fg-2)] md:gap-2 min-w-0">
        {right}
      </div>
    </header>
  );
}
