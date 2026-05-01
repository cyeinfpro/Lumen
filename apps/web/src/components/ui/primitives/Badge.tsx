"use client";

// 小徽标。variant 走语义色 × soft 背景；size 控制 padding/text。

import { cn } from "@/lib/utils";

type Variant = "neutral" | "amber" | "success" | "danger" | "info";
type Size = "sm" | "md";

interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: Variant;
  size?: Size;
  dot?: boolean;
}

const VARIANTS: Record<Variant, { bg: string; text: string; border: string; dot: string }> = {
  neutral: {
    bg: "bg-white/[0.06]",
    text: "text-[var(--fg-0)]",
    border: "border-white/10",
    dot: "bg-[var(--fg-1)]",
  },
  amber: {
    bg: "bg-[var(--accent-soft)]",
    text: "text-[var(--accent)]",
    border: "border-[var(--accent-border)]",
    dot: "bg-[var(--accent)]",
  },
  success: {
    bg: "bg-[var(--success-soft)]",
    text: "text-[var(--success)]",
    border: "border-[var(--success)]/30",
    dot: "bg-[var(--success)]",
  },
  danger: {
    bg: "bg-[var(--danger-soft)]",
    text: "text-[var(--danger)]",
    border: "border-[var(--danger)]/30",
    dot: "bg-[var(--danger)]",
  },
  info: {
    bg: "bg-[var(--info-soft)]",
    text: "text-[var(--info)]",
    border: "border-[var(--info)]/30",
    dot: "bg-[var(--info)]",
  },
};

const SIZES: Record<Size, string> = {
  sm: "h-5 px-1.5 text-[10px] gap-1",
  md: "h-6 px-2 text-[11px] gap-1.5",
};

export function Badge({
  variant = "neutral",
  size = "sm",
  dot = false,
  className,
  children,
  ref,
  ...props
}: BadgeProps & { ref?: React.Ref<HTMLSpanElement> }) {
  const v = VARIANTS[variant];
  return (
    <span
      ref={ref}
      className={cn(
        "inline-flex items-center rounded-full border font-medium tracking-tight whitespace-nowrap",
        SIZES[size],
        v.bg,
        v.text,
        v.border,
        className,
      )}
      {...props}
    >
      {dot ? (
        <span
          className={cn("inline-block w-1.5 h-1.5 rounded-full", v.dot)}
          aria-hidden="true"
        />
      ) : null}
      {children}
    </span>
  );
}

export default Badge;
