"use client";

import { CloudAlert, CloudCheck, CloudUpload, Download, Loader2, RefreshCw } from "lucide-react";

import type { CanvasSaveState } from "@/lib/canvas/types";
import { IconButton } from "@/components/ui/primitives";

export interface CanvasSaveStatusProps {
  state: CanvasSaveState;
  revision: number;
  pendingCount: number;
  locallyDurable: boolean;
  durabilityWarning: string | null;
  message?: string | null;
  onRetry?: () => void;
  onExport: () => void;
}

export function canvasCanRetrySave(
  state: CanvasSaveState, retryPrefixCount: number, durabilityWarning: string | null,
): boolean {
  return state !== "error" || retryPrefixCount > 0 || Boolean(durabilityWarning);
}

function canvasSaveLabel(state: CanvasSaveState, revision: number, pendingCount: number): string {
  switch (state) {
    case "conflict": return "版本冲突";
    case "error": return "保存失败";
    case "saving": return "保存中";
    default: return pendingCount > 0 ? "待保存" : `已保存 · 版本 ${revision}`;
  }
}

function canvasLocalCopyLabel(warning: string | null, locallyDurable: boolean): string {
  if (warning) return "本地恢复不可用";
  return locallyDurable ? "本地副本可用" : "本地副本待确认";
}

function CanvasSaveIcon({ state, pendingCount }: { state: CanvasSaveState; pendingCount: number }) {
  const Icon = state === "saving" ? Loader2
    : state === "error" || state === "conflict" ? CloudAlert
    : pendingCount > 0 ? CloudUpload : CloudCheck;
  return <Icon aria-hidden="true" className={`h-3.5 w-3.5 shrink-0 ${state === "saving" ? "animate-spin motion-reduce:animate-none" : ""}`} />;
}

export function CanvasSaveStatus({
  state, revision, pendingCount, locallyDurable, durabilityWarning,
  message, onRetry, onExport,
}: CanvasSaveStatusProps) {
  const failed = state === "error" || state === "conflict";
  const label = canvasSaveLabel(state, revision, pendingCount);
  const localLabel = canvasLocalCopyLabel(durabilityWarning, locallyDurable);
  return (
    <div
      data-canvas-save-status
      data-canvas-local-durable={locallyDurable}
      className="flex min-h-10 shrink-0 items-center gap-2 border-b border-[var(--border)] bg-[var(--surface-chrome)] px-[max(12px,env(safe-area-inset-left,0px))] py-1 pr-[max(12px,env(safe-area-inset-right,0px))]"
    >
      <div className="min-w-0 flex-1" role="status" aria-live="polite" aria-atomic="true">
        <span className={`flex flex-wrap items-center gap-x-3 gap-y-1 type-caption ${failed ? "text-[var(--danger-fg)]" : "text-[var(--fg-1)]"}`}>
          <span className="inline-flex items-center gap-1.5">
            <CanvasSaveIcon state={state} pendingCount={pendingCount} />
            {label}
          </span>
          {pendingCount > 0 || durabilityWarning ? <span className="text-[var(--fg-2)]">{localLabel}</span> : null}
        </span>
        {failed && message ? <span className="sr-only">{message}</span> : null}
      </div>
      {onRetry && (failed || durabilityWarning) ? (
        <IconButton
          aria-label={state === "conflict" ? "重新检查版本" : "重试保存"}
          tooltip={state === "conflict" ? "重新检查版本" : "重试保存"}
          onClick={onRetry}
          className="shrink-0"
        >
          <RefreshCw className="h-4 w-4" />
        </IconButton>
      ) : null}
      {pendingCount > 0 || failed ? (
        <IconButton aria-label="导出当前副本" tooltip="导出当前副本" onClick={onExport} className="shrink-0">
          <Download className="h-4 w-4" />
        </IconButton>
      ) : null}
    </div>
  );
}
