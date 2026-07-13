"use client";

import {
  Cable,
  Hand,
  Maximize2,
  MousePointer2,
  Plus,
  Undo2,
} from "lucide-react";

import type { CanvasToolMode } from "@/lib/canvas/types";
import { cn } from "@/lib/utils";
import { useCanvasStore } from "../CanvasStoreProvider";

const MODES: Array<{
  mode: CanvasToolMode;
  label: string;
  icon: typeof Hand;
}> = [
  { mode: "hand", label: "平移", icon: Hand },
  { mode: "select", label: "选择", icon: MousePointer2 },
  { mode: "connect", label: "连接", icon: Cable },
];

export function CanvasMobileToolbar({
  onAdd,
  onFitView,
}: {
  onAdd: () => void;
  onFitView: () => void;
}) {
  const toolMode = useCanvasStore((state) => state.toolMode);
  const setToolMode = useCanvasStore((state) => state.setToolMode);
  const undo = useCanvasStore((state) => state.undo);
  const canUndo = useCanvasStore((state) => state.history.length > 0);
  return (
    <div className="absolute inset-x-2 bottom-[max(8px,env(safe-area-inset-bottom))] z-20 flex min-h-14 items-center gap-1 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/96 p-1.5 shadow-[var(--shadow-3)] backdrop-blur-xl md:hidden">
      <div className="grid min-w-0 flex-1 grid-cols-3 rounded-[var(--radius-control)] bg-[var(--bg-0)] p-1">
        {MODES.map(({ mode, label, icon: Icon }) => (
          <button
            key={mode}
            type="button"
            aria-pressed={toolMode === mode}
            aria-label={label}
            onClick={() => setToolMode(mode)}
            className={cn(
              "inline-flex min-h-11 min-w-0 flex-col items-center justify-center gap-0.5 rounded-[var(--radius-control)] type-caption transition-colors",
              toolMode === mode
                ? "bg-[var(--bg-2)] text-[var(--accent)]"
                : "text-[var(--fg-2)]",
            )}
          >
            <Icon className="h-4 w-4" />
            <span>{label}</span>
          </button>
        ))}
      </div>
      <ToolbarButton label="添加节点" onClick={onAdd}>
        <Plus className="h-5 w-5" />
      </ToolbarButton>
      <ToolbarButton label="适应视图" onClick={onFitView}>
        <Maximize2 className="h-5 w-5" />
      </ToolbarButton>
      <ToolbarButton label="撤销" disabled={!canUndo} onClick={undo}>
        <Undo2 className="h-5 w-5" />
      </ToolbarButton>
    </div>
  );
}

function ToolbarButton({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-1)] transition-colors active:bg-[var(--bg-2)] disabled:opacity-40"
    >
      {children}
    </button>
  );
}
