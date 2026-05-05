"use client";

import { type ReactNode } from "react";

import { MobileTopBar } from "@/components/ui/shell/MobileTopBar";
import { MobileTabBar } from "@/components/ui/shell/MobileTabBar";
import { DesktopTopNav } from "@/components/ui/shell/DesktopTopNav";

interface SettingsShellProps {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  maxWidth?: string;
}

export function SettingsShell({
  title,
  subtitle,
  children,
  maxWidth = "max-w-6xl",
}: SettingsShellProps) {
  return (
    <div className="flex min-h-[100dvh] w-full flex-1 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <div className="md:hidden">
        <MobileTopBar
          glassOnScroll={false}
          left={
            <div className="min-w-0">
              <div className="truncate font-display text-[24px] italic leading-[1.05] text-[var(--fg-0)]">
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

      <main className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-[calc(72px+env(safe-area-inset-bottom,0px))] pt-4 md:pb-10 md:pt-8">
        <div className={`mx-auto ${maxWidth} safe-x mobile-compact`}>{children}</div>
      </main>

      <div className="md:hidden">
        <MobileTabBar />
      </div>
    </div>
  );
}
