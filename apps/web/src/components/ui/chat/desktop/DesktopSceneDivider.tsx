"use client";

// Scene NN 分隔条（桌面版）：Instrument Serif italic 13px，两侧 1px 细线。
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
          "italic tracking-[0.02em] leading-none",
          "text-[12px] text-[var(--fg-3)]",
        )}
        style={{ fontFamily: "var(--font-display)" }}
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
