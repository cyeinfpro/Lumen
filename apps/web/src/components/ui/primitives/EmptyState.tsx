"use client";

// 空状态：居中图标 + 标题 + 描述 + 可选 action。
// 默认图标为 Inbox；传 icon prop 可覆盖。

import { Inbox } from "lucide-react";
import { cn } from "@/lib/utils";

interface EmptyStateProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  icon?: React.ReactNode;
  title: React.ReactNode;
  description?: React.ReactNode;
  action?: React.ReactNode;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
  ref,
  ...props
}: EmptyStateProps & { ref?: React.Ref<HTMLDivElement> }) {
  return (
    <div
      ref={ref}
      className={cn(
        "flex flex-col items-center justify-center px-6 py-10 text-center",
        "text-[var(--fg-0)]",
        className,
      )}
      {...props}
    >
      <div
        className={cn(
          "mb-3 flex h-12 w-12 items-center justify-center rounded-[var(--radius-card)]",
          "bg-white/[0.04] border border-white/[0.06] text-[var(--fg-1)]",
        )}
      >
        {icon ?? <Inbox className="w-5 h-5" aria-hidden="true" />}
      </div>
      <h3 className="type-card-title mb-1 text-balance">
        {title}
      </h3>
      {description ? (
        <p className="type-body-sm max-w-sm text-pretty text-[var(--fg-1)]">
          {description}
        </p>
      ) : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

export default EmptyState;
