"use client";

// 正方形图标按钮。aria-label 在类型层强制；tooltip prop 可选包裹。
// 升级版：集成 Framer Motion 提供物理按压动效。

import { cn } from "@/lib/utils";
import { Spinner } from "./Spinner";
import { Tooltip } from "./Tooltip";
import { motion, HTMLMotionProps } from "framer-motion";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "outline";
type Size = "sm" | "md" | "lg";

export interface IconButtonProps extends Omit<HTMLMotionProps<"button">, "transition" | "children"> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  /** 无障碍必填：给屏幕阅读器用的描述 */
  "aria-label": string;
  /** 提供则自动用 Tooltip 包裹；默认方向 top */
  tooltip?: React.ReactNode;
  tooltipSide?: "top" | "bottom" | "left" | "right";
  children?: React.ReactNode;
}

const BASE =
  "inline-flex items-center justify-center rounded-md " +
  "transition-[background-color,color,border-color] duration-150 " +
  "focus-visible:outline-none " +
  "disabled:opacity-50 disabled:pointer-events-none disabled:cursor-not-allowed " +
  "shrink-0 select-none";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-[var(--accent)] text-black hover:bg-[#F6B755]",
  secondary:
    "bg-white/6 text-[var(--fg-0)] hover:bg-white/12 " +
    "border border-white/10 hover:border-white/15",
  ghost:
    "bg-transparent text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-white/6",
  danger:
    "bg-[var(--danger)] text-white hover:brightness-110",
  outline:
    "bg-transparent text-[var(--fg-0)] border border-[var(--border)] hover:border-[var(--border-strong)] hover:bg-white/4",
};

// 移动端：显式 min-h/min-w 11 (44px) 避免 globals.css 的 coarse pointer 兜底与
// 固定 h/w 冲突（会让图标视觉居中但外框被挤出）。桌面保留紧凑尺寸。
const SIZES: Record<Size, string> = {
  sm: "h-8 w-8 max-sm:min-h-11 max-sm:min-w-11",
  md: "h-9 w-9 max-sm:min-h-11 max-sm:min-w-11",
  lg: "h-10 w-10 max-sm:min-h-11 max-sm:min-w-11",
};

export function IconButton({
  variant = "ghost",
  size = "md",
  loading = false,
  tooltip,
  tooltipSide = "top",
  disabled,
  className,
  children,
  type,
  ref,
  ...props
}: IconButtonProps & { ref?: React.Ref<HTMLButtonElement> }) {
  const isDisabled = disabled || loading;
  const btn = (
    <motion.button
      ref={ref}
      type={type ?? "button"}
      disabled={isDisabled}
      aria-busy={loading || undefined}
      className={cn(BASE, VARIANTS[variant], SIZES[size], className)}
      whileHover={isDisabled ? undefined : { scale: 1.05 }}
      whileTap={isDisabled ? undefined : { scale: 0.92 }}
      transition={{ type: "spring", stiffness: 400, damping: 25 }}
      {...props}
    >
      {loading ? <Spinner size={size === "lg" ? 20 : 16} /> : children}
    </motion.button>
  );

  if (tooltip != null && tooltip !== "") {
    return (
      <Tooltip content={tooltip} side={tooltipSide}>
        {btn}
      </Tooltip>
    );
  }
  return btn;
}

export default IconButton;
