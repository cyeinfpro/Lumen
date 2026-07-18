"use client";

import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Pressable } from "./Pressable";

export interface ChipProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  active?: boolean;
  icon?: ReactNode;
  children?: ReactNode;
}

/**
 * 44px 高的 Chip，保证触控命中区和可见边界一致。
 * 反馈走 Pressable（scale + opacity + haptic）。
 */
export function Chip({
  active = false,
  icon,
  children,
  className = "",
  onClick,
  ...rest
}: ChipProps) {
  return (
    <Pressable
      size="inline"
      minHit={false}
      pressScale="soft"
      haptic="light"
      aria-pressed={active}
      onClick={onClick}
      className={[
        "relative min-h-11 px-3 rounded-full gap-1.5",
        "text-caption leading-none whitespace-nowrap font-medium",
        active
          ? "bg-[rgba(242,169,58,0.15)] text-[var(--amber-300)] border border-[rgba(242,169,58,0.40)]"
          : "bg-[var(--bg-2)] text-[var(--fg-1)] border border-[var(--border-subtle)]",
        className,
      ].join(" ")}
      {...rest}
    >
      {icon && <span className="relative z-[1] inline-flex items-center">{icon}</span>}
      {children && <span className="relative z-[1]">{children}</span>}
    </Pressable>
  );
}
