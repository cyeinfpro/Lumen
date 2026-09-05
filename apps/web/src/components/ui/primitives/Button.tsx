"use client";

// 通用按钮原语。variant × size 正交；loading 时禁用并替换左图标为 Spinner。
// 高频按压反馈使用短 CSS transition，保持即时、可中断且不创建动画实例。

import { cn } from "@/lib/utils";
import { Spinner } from "./Spinner";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "outline" | "glass" | "link";
type Size = "sm" | "md" | "lg";

export interface ButtonProps extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  leftIcon?: React.ReactNode;
  rightIcon?: React.ReactNode;
  fullWidth?: boolean;
  children?: React.ReactNode;
}

const BASE =
  "type-control inline-flex max-w-full items-center justify-center gap-1.5 rounded-[var(--radius-control)] " +
  "transition-[transform,background-color,color,border-color,box-shadow,filter,opacity] duration-150 " +
  "focus-visible:outline-none " +
  "disabled:opacity-50 disabled:pointer-events-none disabled:cursor-not-allowed " +
  "select-none text-center leading-tight touch-manipulation";

const VARIANTS: Record<Variant, string> = {
  primary:
    "[background:var(--button-primary-bg)] text-[var(--accent-on)] font-medium " +
    "hover:[background:var(--button-primary-bg-hover)]",
  secondary:
    "[background:var(--button-secondary-bg)] text-[var(--fg-0)] " +
    "hover:[background:var(--button-secondary-bg-hover)] border border-[var(--border)] " +
    "hover:border-[var(--border-strong)]",
  ghost:
    "bg-transparent text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] " +
    "border border-transparent",
  danger:
    "bg-[var(--danger)] text-[var(--danger-on)] hover:brightness-110 " +
    "shadow-[var(--shadow-2)]",
  outline:
    "bg-transparent text-[var(--fg-0)] border border-[var(--border)] " +
    "hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
  // glass: 浮层透明按钮（用于图片卡片浮动操作、composer 附件操作等）。
  glass:
    "[background:var(--button-glass-bg)] backdrop-blur-md text-[var(--fg-0)] " +
    "hover:[background:var(--button-glass-bg-hover)] border border-[var(--border-subtle)] " +
    "hover:border-[var(--border)]",
  // link: 看起来像链接的按钮（替代裸 <a> 风格按钮）。
  // 走 LINK_SIZES 而非 SIZES，避免 twMerge 让 SIZES 的 h/p 覆盖 link 的 h-auto/p-0。
  link:
    "bg-transparent text-[var(--link-fg)] underline underline-offset-2 " +
    "hover:decoration-2 border-0 p-0 h-auto",
};

// 尺寸策略：桌面保持紧凑视觉；移动端 (max-sm) 通过 min-h-11 / 更宽的横向 padding
// 兜底 44×44 可点区域。globals.css 虽已对 (pointer:coarse) 注入 min-height/min-width，
// 但显式 min-h 可避免在视觉层出现"矮按钮 + 外挂 padding"的错位。
const SIZES: Record<Size, string> = {
  sm: "h-9 px-3 max-sm:min-h-11 max-sm:px-3.5",
  md: "h-10 px-4 max-sm:min-h-11",
  lg: "h-11 px-6 rounded-[var(--radius-card)] max-sm:min-h-12 max-sm:px-5",
};

// link variant 专用尺寸：仅控制字号与 inline 行高，绝不引入 h-/px- 让 cn() 覆盖 p-0/h-auto。
const LINK_SIZES: Record<Size, string> = {
  sm: "",
  md: "",
  lg: "",
};

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  leftIcon,
  rightIcon,
  fullWidth,
  disabled,
  className,
  children,
  type,
  ref,
  ...props
}: ButtonProps & { ref?: React.Ref<HTMLButtonElement> }) {
  const isDisabled = disabled || loading;
  const spinnerSize = size === "lg" ? 20 : size === "sm" ? 12 : 16;
  return (
    // @hit-area-ok: size variants and coarse-pointer CSS guarantee 44px touch targets.
    <button
      ref={ref}
      type={type ?? "button"}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      data-lumen-button-variant={variant}
      data-lumen-interactive={isDisabled ? undefined : "soft"}
      className={cn(
        BASE,
        VARIANTS[variant],
        variant === "link" ? LINK_SIZES[size] : SIZES[size],
        fullWidth && "w-full",
        className,
      )}
      {...props}
    >
      {loading ? (
        <Spinner size={spinnerSize} />
      ) : leftIcon ? (
        <span className="inline-flex items-center shrink-0">{leftIcon}</span>
      ) : null}
      {children}
      {rightIcon ? (
        <span className="inline-flex items-center shrink-0">{rightIcon}</span>
      ) : null}
    </button>
  );
}
