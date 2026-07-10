"use client";

// Scene NN 分隔条（桌面版）：左侧 meta label + 单条延伸线。

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
      className="mx-auto my-3 flex w-full max-w-[760px] items-center gap-3 select-none"
      onDoubleClick={onToggle}
      role="separator"
      aria-label={label}
      title="双击折叠/展开"
    >
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
        className="h-px flex-1 bg-[var(--border-subtle)]"
      />
    </div>
  );
}
