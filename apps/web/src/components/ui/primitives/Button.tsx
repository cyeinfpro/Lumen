"use client";

// 通用按钮原语。variant × size 正交；loading 时禁用并替换左图标为 Spinner。
// 升级版：集成 Framer Motion 提供更丝滑的物理动效（按压反弹、物理悬浮）。

import { cn } from "@/lib/utils";
import { Spinner } from "./Spinner";
import { motion, HTMLMotionProps } from "framer-motion";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "outline";
type Size = "sm" | "md" | "lg";

export interface ButtonProps extends Omit<HTMLMotionProps<"button">, "transition" | "children"> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  leftIcon?: React.ReactNode;
  rightIcon?: React.ReactNode;
  fullWidth?: boolean;
  children?: React.ReactNode;
}

const BASE =
  "inline-flex items-center justify-center gap-1.5 font-medium rounded-md " +
  "transition-[background-color,color,border-color,box-shadow] duration-150 " +
  "focus-visible:outline-none " +
  "disabled:opacity-50 disabled:pointer-events-none disabled:cursor-not-allowed " +
  "select-none whitespace-nowrap";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-[var(--accent)] text-black hover:bg-[#F6B755] " +
    "shadow-[0_1px_0_rgba(255,255,255,0.12)_inset,0_6px_18px_-8px_rgba(242,169,58,0.55)]",
  secondary:
    "bg-white/8 text-[var(--fg-0)] hover:bg-white/12 " +
    "border border-white/10 hover:border-white/15 backdrop-blur-sm",
  ghost:
    "bg-transparent text-[var(--fg-0)] hover:bg-white/6 " +
    "border border-transparent",
  danger:
    "bg-[var(--danger)] text-white hover:brightness-110 " +
    "shadow-[0_1px_0_rgba(255,255,255,0.12)_inset,0_6px_18px_-8px_rgba(229,72,77,0.55)]",
  outline:
    "bg-transparent text-[var(--fg-0)] border border-[var(--border)] " +
    "hover:border-[var(--border-strong)] hover:bg-white/4",
};

// 尺寸策略：桌面保持紧凑视觉；移动端 (max-sm) 通过 min-h-11 / 更宽的横向 padding
// 兜底 44×44 可点区域。globals.css 虽已对 (pointer:coarse) 注入 min-height/min-width，
// 但显式 min-h 可避免在视觉层出现"矮按钮 + 外挂 padding"的错位。
const SIZES: Record<Size, string> = {
  sm: "h-8 px-3 text-xs rounded-md max-sm:min-h-10 max-sm:px-3.5",
  md: "h-9 px-4 text-sm rounded-md max-sm:min-h-11 max-sm:text-[15px]",
  lg: "h-11 px-6 text-[15px] rounded-lg max-sm:min-h-12 max-sm:px-5",
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
    <motion.button
      ref={ref}
      type={type ?? "button"}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      className={cn(
        BASE,
        VARIANTS[variant],
        SIZES[size],
        fullWidth && "w-full",
        className,
      )}
      whileHover={isDisabled ? undefined : { scale: 1.01 }}
      whileTap={isDisabled ? undefined : { scale: 0.96 }}
      transition={{ type: "spring", stiffness: 400, damping: 25 }}
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
    </motion.button>
  );
}

export default Button;
