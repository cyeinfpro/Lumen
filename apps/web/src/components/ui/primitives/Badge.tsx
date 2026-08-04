"use client";

import { cn } from "@/lib/utils";

export type BadgeTone = "accent" | "danger" | "success" | "warning" | "info";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  children?: React.ReactNode;
}

const TONES: Record<BadgeTone, string> = {
  accent: "bg-accent-soft text-accent border-accent-border",
  danger: "bg-danger-soft text-danger border-danger-border",
  success: "bg-success-soft text-success border-success-border",
  warning: "bg-warning-soft text-warning border-warning-border",
  info: "bg-info-soft text-info border-info-border",
};

export function Badge({
  tone = "info",
  className,
  children,
  ref,
  ...props
}: BadgeProps & { ref?: React.Ref<HTMLSpanElement> }) {
  return (
    <span
      {...props}
      ref={ref}
      className={cn(
        "inline-flex max-w-full items-center rounded-full border px-2 py-0.5 type-caption",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
