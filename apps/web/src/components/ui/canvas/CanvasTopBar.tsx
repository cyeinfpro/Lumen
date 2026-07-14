"use client";

import {
  ArrowLeft,
  CloudAlert,
  CloudCheck,
  Command,
  Keyboard,
  Loader2,
  Maximize2,
  Minimize2,
  PanelRight,
  Play,
  Redo2,
  Scan,
  Undo2,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { validateCanvasNodeExecution } from "@/lib/canvas/graph";
import { isCanvasExecutableNodeType } from "@/lib/canvas/registry";
import type { CanvasSaveState } from "@/lib/canvas/types";
import { IconButton } from "@/components/ui/primitives";
import { useCanvasStore } from "./CanvasStoreProvider";

export function CanvasTopBar({
  title,
  saveState,
  saveMessage,
  onRename,
  onFitView,
  onRunSelected,
  onOpenInspector,
  onOpenCommandMenu,
  onOpenShortcuts,
  onToggleFullscreen,
  onRetrySave,
  fullscreen,
  running,
}: {
  title: string;
  saveState: CanvasSaveState;
  saveMessage?: string | null;
  onRename: (title: string) => void;
  onFitView: () => void;
  onRunSelected: () => void;
  onOpenInspector: () => void;
  onOpenCommandMenu: () => void;
  onOpenShortcuts: () => void;
  onToggleFullscreen: () => void;
  onRetrySave?: () => void;
  fullscreen: boolean;
  running: boolean;
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
      <header className="hidden h-[var(--appbar-h)] shrink-0 items-center gap-2 border-b border-[var(--border)] bg-[var(--surface-chrome)] px-3 min-[1200px]:flex">
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
        <SaveIndicator
          state={saveState}
          message={saveMessage}
          onRetry={onRetrySave}
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
          <IconButton aria-label="适应视图" tooltip="适应视图" onClick={onFitView}>
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
          <button
            type="button"
            disabled={!runnable || running}
            onClick={onRunSelected}
            className="ml-2 inline-flex h-9 items-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-3 type-body-sm font-medium text-[var(--accent-on)] transition-opacity hover:opacity-[var(--op-hover)] disabled:pointer-events-none disabled:opacity-40"
          >
            {running ? (
              <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" />
            ) : (
              <Play className="h-4 w-4" />
            )}
            运行节点
          </button>
        </div>
      </header>

      <header
        className="flex shrink-0 items-center gap-1 border-b border-[var(--border)] bg-[var(--surface-chrome)] px-[max(8px,env(safe-area-inset-left,0px))] min-[1200px]:hidden"
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
          <SaveIndicator
            state={saveState}
            message={saveMessage}
            onRetry={onRetrySave}
            compact
          />
        </div>
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
        <IconButton
          aria-label="运行节点"
          variant="primary"
          disabled={!runnable || running}
          loading={running}
          onClick={onRunSelected}
        >
          <Play className="h-4 w-4" />
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
      className="min-w-0 max-w-[360px] flex-1 border-0 bg-transparent px-2 type-card-title text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)]"
    />
  );
}

function SaveIndicator({
  state,
  message,
  onRetry,
  compact = false,
}: {
  state: CanvasSaveState;
  message?: string | null;
  onRetry?: () => void;
  compact?: boolean;
}) {
  const content = (() => {
    if (state === "saving") {
      return {
        icon: (
          <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />
        ),
        label: "保存中",
      };
    }
    if (state === "dirty") {
      return { icon: <Loader2 className="h-3.5 w-3.5" />, label: "待保存" };
    }
    if (state === "conflict" || state === "error") {
      return { icon: <CloudAlert className="h-3.5 w-3.5" />, label: state === "conflict" ? "版本冲突" : "保存失败" };
    }
    return { icon: <CloudCheck className="h-3.5 w-3.5" />, label: "已保存" };
  })();
  const className = `inline-flex items-center gap-1.5 type-caption ${
    state === "conflict" || state === "error"
      ? "text-[var(--danger-fg)]"
      : "text-[var(--fg-2)]"
  } ${compact ? "mt-0.5" : ""}`;
  const announcement = message ? `${content.label}：${message}` : content.label;
  if (state === "error" && onRetry) {
    return (
      <>
        <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {announcement}
        </span>
        <button
          type="button"
          title={message ?? "保存失败，点击重试"}
          onClick={onRetry}
          className={className}
        >
          {content.icon}
          重试保存
        </button>
      </>
    );
  }
  return (
    <span
      role="status"
      aria-live="polite"
      aria-atomic="true"
      title={message ?? content.label}
      className={className}
    >
      {content.icon}
      {content.label}
    </span>
  );
}
