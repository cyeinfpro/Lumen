"use client";

// 阶段大卡片外框。所有 Stage 必须用它包，保证标题/副标题/边距统一。
// AnimatePresence 由父级控制，子内容由各 Stage 自行决定结构。

import { cn } from "@/lib/utils";

interface StageFrameProps {
  title: string;
  subtitle: string;
  badge?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

export function StageFrame({
  title,
  subtitle,
  badge,
  actions,
  children,
  className,
}: StageFrameProps) {
  return (
    <section
      className={cn(
        "rounded-md border border-[var(--border)] bg-white/[0.035] p-4 md:p-5",
        "shadow-[var(--shadow-1)]",
        className,
      )}
    >
      <header className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-medium tracking-normal text-[var(--fg-0)]">
              {title}
            </h2>
            {badge}
          </div>
          <p className="mt-1 text-sm leading-6 text-[var(--fg-2)]">{subtitle}</p>
        </div>
        {actions ? <div className="shrink-0">{actions}</div> : null}
      </header>
      {children}
    </section>
  );
}

export function RunningState({
  label,
  className,
}: {
  label: string;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex h-32 items-center justify-center gap-2.5 rounded-md border border-[var(--border)] bg-white/[0.03] text-sm text-[var(--fg-1)]",
        className,
      )}
    >
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[var(--amber-400)] opacity-60" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-[var(--amber-400)]" />
      </span>
      {label}
    </div>
  );
}

export function InfoPanel({
  title,
  children,
  className,
  trailing,
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
  trailing?: React.ReactNode;
}) {
  return (
    <section
      className={cn(
        "rounded-md border border-[var(--border)] bg-white/[0.035] p-3",
        className,
      )}
    >
      <header className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-medium text-[var(--fg-0)]">{title}</h3>
        {trailing}
      </header>
      <div className="mt-2 text-sm leading-6 text-[var(--fg-1)]">{children}</div>
    </section>
  );
}
