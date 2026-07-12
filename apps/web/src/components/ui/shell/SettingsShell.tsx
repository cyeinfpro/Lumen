"use client";

import { type ReactNode, useEffect, useRef } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { MobileTopBar } from "@/components/ui/shell/MobileTopBar";
import { MobileTabBar } from "@/components/ui/shell/MobileTabBar";
import { DesktopTopNav } from "@/components/ui/shell/DesktopTopNav";

interface SettingsShellProps {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  maxWidth?: string;
}

const SETTINGS_NAV = [
  { href: "/settings/api-key", label: "API Key" },
  { href: "/settings/memory", label: "记忆" },
  { href: "/settings/privacy", label: "隐私" },
  { href: "/settings/prompts", label: "提示词" },
  { href: "/settings/providers", label: "供应商" },
  { href: "/settings/telegram", label: "Telegram" },
  { href: "/settings/usage", label: "用量" },
] as const;

export function SettingsShell({
  title,
  subtitle,
  children,
  maxWidth = "max-w-6xl",
}: SettingsShellProps) {
  const pathname = usePathname();
  const settingsNavRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!window.matchMedia("(max-width: 767px)").matches) return;
    const frame = window.requestAnimationFrame(() => {
      const active = settingsNavRef.current?.querySelector<HTMLElement>(
        '[aria-current="page"]',
      );
      active?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "center",
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [pathname]);

  return (
    <div className="flex h-[100dvh] min-h-0 w-full flex-col overflow-hidden bg-[var(--bg-0)] text-[var(--fg-0)] md:h-auto md:min-h-[100dvh] md:overflow-visible">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <div className="md:hidden">
        <MobileTopBar
          glassOnScroll={false}
          left={
            <div className="min-w-0">
              <div className="type-page-title truncate">
                {title}
              </div>
              {subtitle ? (
                <div className="mt-0.5 truncate font-mono text-[10px] tracking-wider text-[var(--fg-2)]">
                  {subtitle}
                </div>
              ) : null}
            </div>
          }
        />
      </div>
      <div className="hidden md:block">
        <DesktopTopNav active="me" />
      </div>

      <nav
        aria-label="设置分类"
        className="safe-x shrink-0 border-b border-[var(--border-subtle)] bg-[var(--bg-0)]/96 md:hidden"
      >
        <div
          ref={settingsNavRef}
          className="flex snap-x snap-mandatory gap-1 overflow-x-auto px-3 py-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {SETTINGS_NAV.map((item) => {
            const active = pathname === item.href;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={
                  "inline-flex min-h-11 shrink-0 snap-start items-center justify-center rounded-[var(--radius-control)] px-3 type-caption font-medium transition-colors " +
                  (active
                    ? "bg-accent-soft text-accent"
                    : "text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]")
                }
              >
                {item.label}
              </Link>
            );
          })}
        </div>
      </nav>

      <main
        data-app-scroll
        className="max-md:mb-[var(--mobile-tabbar-height)] min-h-0 flex-1 scroll-pb-[calc(var(--mobile-tabbar-height)+var(--mobile-tabbar-h))] overflow-x-hidden overflow-y-auto overscroll-contain px-4 pb-[calc(32px+env(safe-area-inset-bottom,0px))] pt-4 touch-pan-y [overflow-anchor:none] md:overflow-visible md:pb-10 md:pt-8"
      >
        <div className={`mx-auto w-full min-w-0 ${maxWidth} safe-x mobile-compact [overflow-wrap:anywhere]`}>{children}</div>
      </main>

      <div className="md:hidden">
        <MobileTabBar />
      </div>
    </div>
  );
}
