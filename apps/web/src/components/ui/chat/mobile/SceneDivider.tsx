"use client";

// Scene NN 分隔条：Instrument Serif italic 11px，两侧 1px 细线。
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
      className="flex items-center gap-2.5 my-2 w-full select-none min-h-[36px] cursor-pointer active:opacity-60 transition-opacity"
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
          "italic text-[11px] tracking-[0.04em] leading-none px-0.5",
          collapsed ? "text-[var(--fg-3)]" : "text-[var(--fg-3)]",
        )}
        style={{ fontFamily: "var(--font-display)" }}
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
