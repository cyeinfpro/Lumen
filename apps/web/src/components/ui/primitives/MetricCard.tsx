"use client";

import { cn } from "@/lib/utils";

export interface MetricCardProps extends React.HTMLAttributes<HTMLDivElement> {
  icon?: React.ReactNode;
  label: React.ReactNode;
  value: React.ReactNode;
  sub?: React.ReactNode;
}

export function MetricCard({
  icon,
  label,
  value,
  sub,
  className,
  ref,
  ...props
}: MetricCardProps & { ref?: React.Ref<HTMLDivElement> }) {
  return (
    <div
      {...props}
      ref={ref}
      className={cn(
        "surface-card flex min-w-0 flex-col gap-[var(--space-2)] p-[var(--space-4)]",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        {icon ? (
          <span
            aria-hidden="true"
            className="inline-flex shrink-0 text-[var(--fg-2)]"
          >
            {icon}
          </span>
        ) : null}
        <span className="min-w-0 type-caption">{label}</span>
      </div>
      <div className="type-metric">{value}</div>
      {sub ? (
        <div className="type-caption text-[var(--fg-2)]">{sub}</div>
      ) : null}
    </div>
  );
}
