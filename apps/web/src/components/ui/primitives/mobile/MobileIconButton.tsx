"use client";

import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { Pressable } from "./Pressable";

export interface MobileIconButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon: ReactNode;
  label: string;
  variant?: "plain" | "filled" | "danger";
  size?: "default" | "large";
  minHit?: boolean;
  onPress?: () => void;
}

/**
 * 44×44 图标按钮（large=56），icon 20×20 居中。
 * 基于 Pressable —— 不需要自行 wire haptic/反馈。
 * 默认 minHit=true；某些场景（如 Toast 右上角关闭）传 minHit={false} 以保留紧凑视觉。
 */
export const MobileIconButton = forwardRef<HTMLButtonElement, MobileIconButtonProps>(
  function MobileIconButton(
    {
      icon,
      label,
      variant = "plain",
      size = "default",
      minHit = true,
      className = "",
      onPress,
      ...rest
    },
    ref,
  ) {
    const variantClass =
      variant === "filled"
        ? "bg-[var(--bg-2)] text-[var(--fg-0)] border border-[var(--border)]"
        : variant === "danger"
          ? "bg-[var(--danger-soft)] text-[var(--danger)] border border-[var(--danger)]/40"
          : "bg-transparent text-[var(--fg-1)]";

    return (
      <Pressable
        ref={ref as React.Ref<HTMLElement>}
        size={size}
        minHit={minHit}
        pressScale="tight"
        haptic="light"
        aria-label={label}
        onPress={onPress as () => void}
        className={["rounded-full", variantClass, className].filter(Boolean).join(" ")}
        {...rest}
      >
        <span className="w-5 h-5 inline-flex items-center justify-center" aria-hidden>
          {icon}
        </span>
      </Pressable>
    );
  },
);
