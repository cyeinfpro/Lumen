"use client";

import {
  ArrowLeft,
  CloudAlert,
  CloudCheck,
  Loader2,
  Maximize2,
  PanelRight,
  Play,
  Redo2,
  Undo2,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";

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
  running,
}: {
  title: string;
  saveState: CanvasSaveState;
  saveMessage?: string | null;
  onRename: (title: string) => void;
  onFitView: () => void;
  onRunSelected: () => void;
  onOpenInspector: () => void;
  running: boolean;
}) {
  const historyLength = useCanvasStore((state) => state.history.length);
  const futureLength = useCanvasStore((state) => state.future.length);
  const selectedNodeId = useCanvasStore((state) => state.selectedNodeId);
  const graph = useCanvasStore((state) => state.graph);
  const undo = useCanvasStore((state) => state.undo);
  const redo = useCanvasStore((state) => state.redo);
  const selectedNode = graph.nodes.find((node) => node.id === selectedNodeId);
  const runnable =
    selectedNode?.type === "image_generate" ||
    selectedNode?.type === "video_generate";

  return (
    <>
      <header className="hidden h-[var(--appbar-h)] shrink-0 items-center gap-2 border-b border-[var(--border)] bg-[var(--surface-chrome)] px-3 md:flex">
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
        <SaveIndicator state={saveState} message={saveMessage} />
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
            <Maximize2 className="h-4 w-4" />
          </IconButton>
          <button
            type="button"
            disabled={!runnable || running}
            onClick={onRunSelected}
            className="ml-2 inline-flex h-9 items-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-3 type-body-sm font-medium text-[var(--accent-on)] transition-opacity hover:opacity-[var(--op-hover)] disabled:pointer-events-none disabled:opacity-40"
          >
            {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            运行节点
          </button>
        </div>
      </header>

      <header className="flex h-[var(--mobile-topbar-h)] shrink-0 items-center gap-1 border-b border-[var(--border)] bg-[var(--surface-chrome)] px-2 md:hidden">
        <Link
          href="/projects/canvas"
          aria-label="返回画布列表"
          className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full text-[var(--fg-1)] active:bg-[var(--bg-2)]"
        >
          <ArrowLeft className="h-[18px] w-[18px]" />
        </Link>
        <div className="min-w-0 flex-1">
          <p className="truncate type-body-sm font-medium text-[var(--fg-0)]">{title}</p>
          <SaveIndicator state={saveState} message={saveMessage} compact />
        </div>
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
  compact = false,
}: {
  state: CanvasSaveState;
  message?: string | null;
  compact?: boolean;
}) {
  const content = (() => {
    if (state === "saving") {
      return { icon: <Loader2 className="h-3.5 w-3.5 animate-spin" />, label: "保存中" };
    }
    if (state === "dirty") {
      return { icon: <Loader2 className="h-3.5 w-3.5" />, label: "待保存" };
    }
    if (state === "conflict" || state === "error") {
      return { icon: <CloudAlert className="h-3.5 w-3.5" />, label: state === "conflict" ? "版本冲突" : "保存失败" };
    }
    return { icon: <CloudCheck className="h-3.5 w-3.5" />, label: "已保存" };
  })();
  return (
    <span
      title={message ?? content.label}
      className={`inline-flex items-center gap-1.5 type-caption ${
        state === "conflict" || state === "error"
          ? "text-[var(--danger-fg)]"
          : "text-[var(--fg-2)]"
      } ${compact ? "mt-0.5" : ""}`}
    >
      {content.icon}
      {content.label}
    </span>
  );
}
