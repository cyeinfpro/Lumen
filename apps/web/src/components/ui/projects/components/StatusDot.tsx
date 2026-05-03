"use client";

// 步骤侧栏 / 移动条上用的 2px 圆点，依状态着色。
// running 走 lumen-pulse-soft 慢呼吸（globals.css 已定义），暗示"正在显影"。

import { cn } from "@/lib/utils";

const PULSE = "animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]";

const STATUS_CLASS: Record<string, string> = {
  approved: "bg-[var(--success)]",
  completed: "bg-[var(--success)]",
  selected: "bg-[var(--success)]",
  running: `bg-[var(--amber-400)] ${PULSE}`,
  generating: `bg-[var(--amber-400)] ${PULSE}`,
  needs_review: "bg-[var(--amber-300)]",
  failed: "bg-[var(--danger)]",
  rejected: "bg-[var(--danger)]/70",
};

export function StatusDot({
  status,
  size = 8,
  className,
}: {
  status?: string;
  size?: number;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      style={{ width: size, height: size }}
      className={cn(
        "inline-block shrink-0 rounded-full",
        status ? (STATUS_CLASS[status] ?? "bg-[var(--fg-3)]") : "bg-[var(--fg-3)]",
        className,
      )}
    />
  );
}
