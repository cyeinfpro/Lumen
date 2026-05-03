"use client";

// 项目页使用全局主导航，确保"项目"与"创作 / 图库 / 我的"处于同一层级。
// 桌面端复用 DesktopTopNav；移动端复用全站 MobileTopBar + MobileTabBar，
// 避免项目模块在手机上另起一套导航语言。

import { ArrowLeft } from "lucide-react";
import Link from "next/link";

import { cn } from "@/lib/utils";
import { DesktopTopNav } from "@/components/ui/shell";
import { MobileTabBar } from "@/components/ui/shell/MobileTabBar";
import { MobileTopBar } from "@/components/ui/shell/MobileTopBar";

interface ProjectTopBarProps {
  right?: React.ReactNode;
}

export function ProjectTopBar({ right }: ProjectTopBarProps) {
  return (
    <div className="hidden md:block">
      <DesktopTopNav active="projects" right={right} />
    </div>
  );
}

interface ProjectMobileTopBarProps {
  title: string;
  subtitle?: React.ReactNode;
  backHref?: string;
  backLabel?: string;
  right?: React.ReactNode;
  className?: string;
}

export function ProjectMobileTopBar({
  title,
  subtitle,
  backHref,
  backLabel = "返回项目",
  right,
  className,
}: ProjectMobileTopBarProps) {
  return (
    <MobileTopBar
      className={cn("md:hidden", className)}
      left={
        <div className="flex min-w-0 items-center gap-2">
          {backHref ? (
            <Link
              href={backHref}
              aria-label={backLabel}
              className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-[var(--fg-1)] transition-colors active:bg-[var(--bg-2)]"
            >
              <ArrowLeft className="h-[18px] w-[18px]" />
            </Link>
          ) : null}
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
        </div>
      }
      right={right}
    />
  );
}

export function ProjectMobileTabBar() {
  return (
    <div className="md:hidden">
      <MobileTabBar />
    </div>
  );
}
