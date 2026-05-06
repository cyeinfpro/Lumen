"use client";

// 骨架占位。配合 .animate-shimmer（globals.css）做光扫。默认使用控件半径。

import { cn } from "@/lib/utils";

interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  /** 关掉 shimmer，只保留静态灰块（用于非常小的占位） */
  static?: boolean;
}

export function Skeleton({
  className,
  static: isStatic = false,
  ref,
  ...props
}: SkeletonProps & { ref?: React.Ref<HTMLDivElement> }) {
  return (
    <div
      ref={ref}
      aria-hidden="true"
      className={cn(
        "block rounded-[var(--radius-control)] bg-white/[0.06]",
        !isStatic && "animate-shimmer",
        className,
      )}
      {...props}
    />
  );
}

export default Skeleton;
