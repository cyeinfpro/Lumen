"use client";
// 卡片归一原语（V1 设计语言统一, 2026-05-09）。
// 替代散在各处的 "rounded-2xl border border-[var(--border)] bg-..." 拼接写法。
// variant × elevation × padding 正交。default/subtle 复用 globals.css 的 surface-* class。

import { cn } from "@/lib/utils";

type Variant = "default" | "glass" | "subtle";
type Elevation = 0 | 1 | 2 | 3;
type Padding = "none" | "sm" | "md" | "lg";

interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: Variant;
  elevation?: Elevation;
  padding?: Padding;
  /** true 时启用 surface-card-hover（hover 提升边框/背景/阴影） */
  hover?: boolean;
  /** 暂未实现 Slot 转发，预留 API。 */
  asChild?: false;
}

// default 走 globals.css 的 surface-card；默认保持平面，只有明确需要抬升时才加阴影，
// 避免页面区块、列表和工具面板全部呈现为同权重卡片。
// glass / subtle 走自定义视觉。
const VARIANTS: Record<Variant, string> = {
  default: "surface-card",
  glass:
    "bg-[var(--bg-0)]/70 backdrop-blur-md border border-[var(--border-strong)] " +
    "rounded-[var(--radius-panel)]",
  subtle:
    "bg-[var(--bg-1)]/60 border border-[var(--border-subtle)] " +
    "rounded-[var(--radius-card)]",
};

// elevation 是叠加层。default 自带 shadow-1：elevation=1 视觉等价、
// elevation=0 显式 shadow-none 抹掉、2/3 升级到更强阴影。
const ELEVATIONS: Record<Elevation, string> = {
  0: "shadow-none",
  1: "shadow-[var(--shadow-1)]",
  2: "shadow-[var(--shadow-2)]",
  3: "shadow-[var(--shadow-3)]",
};

const PADDINGS: Record<Padding, string> = {
  none: "",
  sm: "p-3",
  md: "p-4 max-[359px]:p-3",
  lg: "p-6 max-sm:p-4 max-[359px]:p-3",
};

export function Card({
  variant = "default",
  elevation = 0,
  padding = "md",
  hover = false,
  className,
  children,
  ref,
  ...props
}: CardProps & { ref?: React.Ref<HTMLDivElement> }) {
  return (
    <div
      ref={ref}
      data-lumen-card={hover ? "true" : undefined}
      data-lumen-reveal={hover ? "true" : undefined}
      className={cn(
        VARIANTS[variant],
        ELEVATIONS[elevation],
        PADDINGS[padding],
        hover && "surface-card-hover",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}
