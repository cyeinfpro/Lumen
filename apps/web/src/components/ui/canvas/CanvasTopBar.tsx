"use client";

import {
  ArrowLeft,
  Command,
  Keyboard,
  Maximize2,
  Minimize2,
  PanelRight,
  Redo2,
  Scan,
  Undo2,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { MobileRuntimeResilienceStatus } from "@/components/RuntimeResilienceStatus";
import { validateCanvasNodeExecution } from "@/lib/canvas/graph";
import { isCanvasExecutableNodeType } from "@/lib/canvas/registry";
import { IconButton } from "@/components/ui/primitives";
import { useCanvasStore } from "./CanvasStoreProvider";

export function CanvasTopBar({
  title,
  onRename,
  onFitView,
  onOpenInspector,
  onOpenCommandMenu,
  onOpenShortcuts,
  onToggleFullscreen,
  fullscreen,
}: {
  title: string;
  onRename: (title: string) => void;
  onFitView: () => void;
  onOpenInspector: () => void;
  onOpenCommandMenu: () => void;
  onOpenShortcuts: () => void;
  onToggleFullscreen: () => void;
  fullscreen: boolean;
}) {
  const historyLength = useCanvasStore((state) => state.history.length);
  const futureLength = useCanvasStore((state) => state.future.length);
  const selectedNodeId = useCanvasStore((state) => state.selectedNodeId);
  const graph = useCanvasStore((state) => state.graph);
  const undo = useCanvasStore((state) => state.undo);
  const redo = useCanvasStore((state) => state.redo);
  const selectedNode = graph.nodes.find((node) => node.id === selectedNodeId);
  const runnable = Boolean(
    selectedNode &&
      isCanvasExecutableNodeType(selectedNode.type) &&
      validateCanvasNodeExecution(graph, selectedNode.id).valid,
  );

  return (
    <>
      <header
        data-canvas-selected-runnable={runnable ? "true" : "false"}
        className="hidden h-[var(--appbar-h)] shrink-0 items-center gap-2 border-b border-[var(--border)] bg-[var(--surface-chrome)] px-3 min-[768px]:flex"
      >
        <Link
          href="/projects/canvas"
          aria-label="返回画布列表"
          title="返回画布列表"
          className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <CanvasTitleInput
          key={title}
          title={title}
          onRename={onRename}
        />
        <div className="ml-auto flex items-center gap-1">
          <IconButton
            aria-label="撤销"
            tooltip="撤销"
            disabled={historyLength === 0}
            onClick={undo}
          >
            <Undo2 className="h-4 w-4" />
          </IconButton>
          <IconButton
            aria-label="重做"
            tooltip="重做"
            disabled={futureLength === 0}
            onClick={redo}
          >
            <Redo2 className="h-4 w-4" />
          </IconButton>
          <IconButton
            aria-label="适应视图"
            tooltip="适应视图"
            onClick={onFitView}
            className="hidden min-[1200px]:inline-flex"
          >
            <Scan className="h-4 w-4" />
          </IconButton>
          <IconButton
            aria-label="打开命令菜单"
            tooltip="命令菜单"
            onClick={onOpenCommandMenu}
          >
            <Command className="h-4 w-4" />
          </IconButton>
          <IconButton
            aria-label="查看快捷键"
            tooltip="快捷键"
            onClick={onOpenShortcuts}
          >
            <Keyboard className="h-4 w-4" />
          </IconButton>
          <IconButton
            aria-label={fullscreen ? "退出全屏" : "全屏画布"}
            tooltip={fullscreen ? "退出全屏" : "全屏画布"}
            aria-pressed={fullscreen}
            onClick={onToggleFullscreen}
          >
            {fullscreen ? (
              <Minimize2 className="h-4 w-4" />
            ) : (
              <Maximize2 className="h-4 w-4" />
            )}
          </IconButton>
          <IconButton
            aria-label="打开检查器"
            tooltip="检查器"
            onClick={onOpenInspector}
            className="min-[1200px]:hidden"
          >
            <PanelRight className="h-4 w-4" />
          </IconButton>
        </div>
      </header>

      <header
        className="flex shrink-0 items-center gap-1 border-b border-[var(--border)] bg-[var(--surface-chrome)] px-[max(8px,env(safe-area-inset-left,0px))] min-[768px]:hidden"
        style={{
          minHeight:
            "calc(var(--mobile-topbar-h) + max(env(safe-area-inset-top, 0px), calc(var(--system-banner-height, 0px) + var(--offline-banner-height, 0px))))",
          paddingTop:
            "max(env(safe-area-inset-top, 0px), calc(var(--system-banner-height, 0px) + var(--offline-banner-height, 0px)))",
          paddingRight: "max(8px, env(safe-area-inset-right, 0px))",
        }}
      >
        <Link
          href="/projects/canvas"
          aria-label="返回画布列表"
          className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-[var(--fg-1)] active:bg-[var(--bg-2)]"
        >
          <ArrowLeft className="h-[18px] w-[18px]" />
        </Link>
        <div className="min-w-0 flex-1">
          <p className="truncate type-body-sm font-medium text-[var(--fg-0)]">{title}</p>
        </div>
        <MobileRuntimeResilienceStatus />
        <IconButton
          aria-label={fullscreen ? "退出全屏" : "全屏画布"}
          aria-pressed={fullscreen}
          onClick={onToggleFullscreen}
        >
          {fullscreen ? (
            <Minimize2 className="h-4 w-4" />
          ) : (
            <Maximize2 className="h-4 w-4" />
          )}
        </IconButton>
        <IconButton aria-label="打开检查器" onClick={onOpenInspector}>
          <PanelRight className="h-4 w-4" />
        </IconButton>
      </header>
    </>
  );
}

function CanvasTitleInput({
  title,
  onRename,
}: {
  title: string;
  onRename: (title: string) => void;
}) {
  const [draftTitle, setDraftTitle] = useState(title);
  return (
    <input
      value={draftTitle}
      maxLength={255}
      aria-label="画布标题"
      onChange={(event) => setDraftTitle(event.currentTarget.value)}
      onBlur={() => {
        const next = draftTitle.trim();
        if (next && next !== title) onRename(next);
        else setDraftTitle(title);
      }}
      className="min-w-0 max-w-[360px] flex-1 rounded-[var(--radius-control)] border border-transparent bg-transparent px-2 py-1 type-card-title text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] transition-[border-color,background-color,box-shadow] duration-[var(--dur-quick)] hover:border-[var(--border)] hover:bg-[var(--bg-2)] focus:border-[var(--border-strong)] focus:bg-[var(--bg-1)] focus:shadow-[var(--ring)]"
    />
  );
}
