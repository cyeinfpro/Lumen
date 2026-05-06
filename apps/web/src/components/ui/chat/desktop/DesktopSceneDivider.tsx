"use client";

// Scene NN 分隔条（桌面版）：mono meta label + 两侧 1px 细线。
// 跟 mobile 视觉一致，仅字号略大、上下间距更舒朗。

import { cn } from "@/lib/utils";

interface DesktopSceneDividerProps {
  index: number;
  collapsed?: boolean;
  onToggle?: () => void;
}

export function DesktopSceneDivider({
  index,
  collapsed,
  onToggle,
}: DesktopSceneDividerProps) {
  const label = `Scene ${String(index).padStart(2, "0")}`;
  return (
    <div
      className="flex items-center gap-3 my-2 select-none"
      onDoubleClick={onToggle}
      role="separator"
      aria-label={label}
      title="双击折叠/展开"
    >
      <span
        aria-hidden="true"
        className="flex-1 h-px bg-[var(--border-subtle)]"
      />
      <span
        aria-hidden="true"
        className={cn(
          "font-mono text-[10px] uppercase tracking-[0.14em] leading-none",
          "text-[var(--fg-3)]",
        )}
      >
        {label}
        {collapsed ? " · 折叠" : ""}
      </span>
      <span
        aria-hidden="true"
        className="flex-1 h-px bg-[var(--border-subtle)]"
      />
    </div>
  );
}
