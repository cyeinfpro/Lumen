"use client";

// Stage frame：去除嵌套卡片化，改为 hairline 分隔 + 工作台式排印层级。
// 视觉规范：mono uppercase eyebrow + compact title + sans subtitle。
// 子内容贴底铺，不再叠加 bg-white/[0.035] + border + shadow。

import { cn } from "@/lib/utils";

interface StageFrameProps {
  title: string;
  subtitle: string;
  badge?: React.ReactNode;
  actions?: React.ReactNode;
  eyebrow?: string;
  children: React.ReactNode;
  className?: string;
}

export function StageFrame({
  title,
  subtitle,
  badge,
  actions,
  eyebrow = "STAGE",
  children,
  className,
}: StageFrameProps) {
  return (
    <section className={cn("relative", className)}>
      <header className="pb-5 pt-1 md:pt-2">
        <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0 flex-1">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              {eyebrow}
            </p>
            <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h2 className="text-[22px] font-semibold leading-[1.18] tracking-tight text-[var(--fg-0)] md:text-[26px]">
                {title}
              </h2>
              {badge}
            </div>
            <p className="mt-2 max-w-xl text-[13px] leading-6 text-[var(--fg-2)]">
              {subtitle}
            </p>
          </div>
          {actions ? <div className="w-full shrink-0 self-start sm:w-auto md:self-end">{actions}</div> : null}
        </div>
      </header>
      <div className="relative">{children}</div>
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
        "flex h-40 flex-col items-center justify-center gap-4 border-y border-[var(--border)] text-center",
        className,
      )}
    >
      <span aria-hidden className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[var(--amber-400)] opacity-50" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-[var(--amber-400)]" />
      </span>
      <p className="text-[15px] font-semibold tracking-tight text-[var(--fg-0)]">{label}</p>
      <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
        Developing
      </p>
    </div>
  );
}

// 信息面板：hairline + mono eyebrow + body 文本。无嵌套卡片化。
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
    <section className={cn("border-t border-[var(--border)] py-3", className)}>
      <header className="flex items-center justify-between gap-2">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          {title}
        </p>
        {trailing}
      </header>
      <div className="mt-2 text-[13px] leading-6 text-[var(--fg-1)]">{children}</div>
    </section>
  );
}
