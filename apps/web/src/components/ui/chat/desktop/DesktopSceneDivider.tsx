"use client";

// Scene NN 分隔条（桌面版）：左侧 meta label + 单条延伸线。

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/primitives";

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
    <Button
      variant="ghost"
      size="sm"
      className="mx-auto my-4 h-auto w-full max-w-[var(--content-composer)] justify-start gap-3 px-0 select-none hover:bg-transparent"
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
          "type-caption shrink-0 leading-none",
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
    </Button>
  );
}
