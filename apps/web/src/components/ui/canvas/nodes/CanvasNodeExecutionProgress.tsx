"use client";

import {
  canvasExecutionElapsedMs,
  canvasExecutionPrimaryTask,
  canvasExecutionProgressPercent,
  canvasExecutionStageLabel,
  formatCanvasTaskElapsed,
  isCanvasExecutionActive,
} from "@/lib/canvas/executionPresentation";
import type { CanvasNodeExecution } from "@/lib/canvas/types";
import { cn } from "@/lib/utils";

export function CanvasNodeExecutionProgress({
  execution,
}: {
  execution?: CanvasNodeExecution | null;
}) {
  if (!execution || !isCanvasExecutionActive(execution)) return null;
  const task = canvasExecutionPrimaryTask(execution);
  const progress = canvasExecutionProgressPercent(execution);
  const elapsed = formatCanvasTaskElapsed(canvasExecutionElapsedMs(execution));
  const stage = canvasExecutionStageLabel(execution);
  return (
    <div
      role="status"
      className="grid gap-1.5 border-t border-[var(--border-subtle)] bg-[var(--accent-soft)]/45 px-3 py-2"
    >
      <div className="flex items-center justify-between gap-2 type-caption">
        <span className="min-w-0 truncate font-medium text-[var(--fg-1)]">
          {stage}
          {task?.model ? ` · ${task.model}` : ""}
        </span>
        <span className="shrink-0 tabular-nums text-[var(--fg-2)]">
          {progress !== null
            ? `${progress}%`
            : elapsed
              ? `已用 ${elapsed}`
              : "进行中"}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={`${stage}进度`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress ?? undefined}
        className="h-1.5 overflow-hidden rounded-full bg-[var(--bg-3)]"
      >
        <span
          className={cn(
            "block h-full rounded-full bg-[var(--accent)] transition-[width]",
            progress === null &&
              "w-1/3 animate-pulse motion-reduce:animate-none",
          )}
          style={progress === null ? undefined : { width: `${progress}%` }}
        />
      </div>
    </div>
  );
}
