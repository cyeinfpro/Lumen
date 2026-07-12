"use client";

// Scene NN 分隔条：mono meta label + 两侧 1px 细线。
// 双击切换折叠态（回调上浮）。

import { cn } from "@/lib/utils";

interface SceneDividerProps {
  index: number;
  collapsed?: boolean;
  onToggle?: () => void;
}

export function SceneDivider({ index, collapsed, onToggle }: SceneDividerProps) {
  const label = `Scene ${String(index).padStart(2, "0")}`;
  return (
    <button
      type="button"
      onClick={onToggle}
      className="my-2 flex min-h-11 w-full items-center gap-2.5 select-none cursor-pointer active:opacity-60 transition-opacity motion-reduce:transition-none"
      aria-label={collapsed ? `${label} (已折叠，点击展开)` : `${label} (点击折叠)`}
      aria-expanded={!collapsed}
    >
      <span
        aria-hidden="true"
        className="flex-1 h-px bg-gradient-to-r from-transparent via-[var(--border-subtle)] to-transparent"
      />
      <span
        aria-hidden="true"
        className={cn(
          "px-0.5 font-mono text-[10px] uppercase tracking-[0.14em] leading-none",
          collapsed ? "text-[var(--fg-3)]" : "text-[var(--fg-3)]",
        )}
      >
        {label}
        {collapsed ? " ▸" : ""}
      </span>
      <span
        aria-hidden="true"
        className="flex-1 h-px bg-gradient-to-l from-transparent via-[var(--border-subtle)] to-transparent"
      />
    </button>
  );
}
