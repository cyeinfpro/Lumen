"use client";

import {
  Cable,
  Command,
  Hand,
  MousePointer2,
  Plus,
  Redo2,
  Scan,
  Undo2,
} from "lucide-react";

import { blurActiveCanvasEditor } from "@/lib/canvas/interaction";
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
  onOpenCommandMenu,
}: {
  onAdd: () => void;
  onFitView: () => void;
  onOpenCommandMenu: () => void;
}) {
  const toolMode = useCanvasStore((state) => state.toolMode);
  const setToolMode = useCanvasStore((state) => state.setToolMode);
  const undo = useCanvasStore((state) => state.undo);
  const redo = useCanvasStore((state) => state.redo);
  const canUndo = useCanvasStore((state) => state.history.length > 0);
  const canRedo = useCanvasStore((state) => state.future.length > 0);
  return (
    <nav
      aria-label="画布工具"
      className="relative z-[var(--z-tabbar)] w-full shrink-0 overflow-x-auto overscroll-x-contain border-t border-[var(--border)] bg-[var(--surface-chrome)]/96 pt-1.5 shadow-[var(--shadow-1)] backdrop-blur-xl [scrollbar-width:none] min-[1200px]:hidden [&::-webkit-scrollbar]:hidden"
      style={{
        paddingBottom: "max(6px, env(safe-area-inset-bottom, 0px))",
        paddingLeft: "max(8px, env(safe-area-inset-left, 0px))",
        paddingRight: "max(8px, env(safe-area-inset-right, 0px))",
      }}
    >
      <div
        role="toolbar"
        className="flex min-w-max touch-pan-x items-center gap-1"
      >
        <div
          role="group"
          aria-label="画布模式"
          className="grid w-[132px] shrink-0 grid-cols-3 rounded-[var(--radius-control)] bg-[var(--bg-0)] p-0.5"
        >
          {MODES.map(({ mode, label, icon: Icon }) => (
            <button
              key={mode}
              type="button"
              aria-pressed={toolMode === mode}
              aria-label={label}
              onClick={() => {
                blurActiveCanvasEditor();
                setToolMode(mode);
              }}
              data-lumen-interactive="true"
              className={cn(
                "inline-flex min-h-11 min-w-11 flex-col items-center justify-center gap-0.5 rounded-[var(--radius-control)] type-caption",
                "transition-[background-color,color,opacity] duration-[var(--dur-fast)] ease-[var(--ease-develop)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
                toolMode === mode
                  ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                  : "text-[var(--fg-2)] active:bg-[var(--bg-2)]",
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
        <ToolbarButton
          label="命令菜单"
          onClick={() => {
            blurActiveCanvasEditor();
            onOpenCommandMenu();
          }}
        >
          <Command className="h-5 w-5" />
        </ToolbarButton>
        <ToolbarButton label="适应视图" onClick={onFitView}>
          <Scan className="h-5 w-5" />
        </ToolbarButton>
        <ToolbarButton
          label="撤销"
          disabled={!canUndo}
          onClick={() => {
            blurActiveCanvasEditor();
            undo();
          }}
        >
          <Undo2 className="h-5 w-5" />
        </ToolbarButton>
        <ToolbarButton
          label="重做"
          disabled={!canRedo}
          onClick={() => {
            blurActiveCanvasEditor();
            redo();
          }}
        >
          <Redo2 className="h-5 w-5" />
        </ToolbarButton>
      </div>
    </nav>
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
      data-lumen-interactive={disabled ? undefined : "true"}
      className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-1)] transition-[background-color,color,opacity] duration-[var(--dur-fast)] ease-[var(--ease-develop)] active:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:shadow-[var(--ring)] disabled:pointer-events-none disabled:opacity-40"
    >
      {children}
    </button>
  );
}
