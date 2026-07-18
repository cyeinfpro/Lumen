"use client";

// Scene NN 分隔条（桌面版）：左侧 meta label + 单条延伸线。

import { cn } from "@/lib/utils";

interface DesktopSceneDividerProps {
  index: number;
  collapsed?: boolean;
  controlsId?: string;
  onToggle?: () => void;
}

export function DesktopSceneDivider({
  index,
  collapsed,
  controlsId,
  onToggle,
}: DesktopSceneDividerProps) {
  const label = `Scene ${String(index).padStart(2, "0")}`;
  return (
    <button
      type="button"
      className="mx-auto my-3 flex w-full max-w-[var(--content-composer)] items-center gap-3 select-none"
      onClick={onToggle}
      aria-label={
        collapsed ? `${label}（已折叠，点击展开）` : `${label}（点击折叠）`
      }
      aria-expanded={!collapsed}
      aria-controls={controlsId}
    >
      <span
        aria-hidden="true"
        className={cn(
          "type-mono-meta shrink-0 leading-none",
          "text-[var(--fg-2)]",
        )}
      >
        {label}
        {collapsed ? " · 折叠" : ""}
      </span>
      <span
        aria-hidden="true"
        className="h-px flex-1 bg-[var(--border-subtle)]"
      />
    </button>
  );
}
