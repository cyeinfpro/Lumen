import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Loader2,
} from "lucide-react";

import { canvasExecutionStatusLabel } from "@/lib/canvas/executionPresentation";
import type { CanvasNodeExecution } from "@/lib/canvas/types";

const ACTIVE = new Set([
  "pending",
  "ready",
  "queued",
  "running",
  "reconciling",
  "canceling",
]);
const TERMINAL_OK = new Set(["succeeded", "reused"]);

export function CanvasNodeStatus({
  execution,
}: {
  execution?: CanvasNodeExecution | null;
}) {
  if (!execution) return null;
  const label = canvasExecutionStatusLabel(execution.status);
  const title = executionStatusTitle(execution, label);
  if (ACTIVE.has(execution.status)) {
    return (
      <span role="status" title={title} className="inline-flex shrink-0">
        <Loader2
          className="h-4 w-4 animate-spin text-[var(--accent)] motion-reduce:animate-none"
          aria-hidden
        />
        <span className="sr-only">状态：{label}</span>
      </span>
    );
  }
  if (execution.status === "failed" || execution.status === "blocked") {
    return (
      <span role="alert" title={title} className="inline-flex shrink-0">
        <AlertCircle className="h-4 w-4 text-[var(--danger-fg)]" aria-hidden />
        <span className="sr-only">状态：{label}</span>
      </span>
    );
  }
  if (execution.status === "partial_failed") {
    return (
      <span role="status" title={title} className="inline-flex shrink-0">
        <AlertTriangle
          className="h-4 w-4 text-[var(--warning-fg)]"
          aria-hidden
        />
        <span className="sr-only">状态：{label}</span>
      </span>
    );
  }
  if (TERMINAL_OK.has(execution.status)) {
    return (
      <span role="status" title={label} className="inline-flex shrink-0">
        <CheckCircle2
          className="h-4 w-4 text-[var(--success-fg)]"
          aria-hidden
        />
        <span className="sr-only">状态：{label}</span>
      </span>
    );
  }
  return (
    <span
      role="status"
      title={title}
      className="inline-flex h-4 w-4 shrink-0 items-center justify-center"
    >
      <span className="h-2 w-2 rounded-full bg-[var(--fg-3)]" aria-hidden />
      <span className="sr-only">状态：{label}</span>
    </span>
  );
}

function executionStatusTitle(
  execution: CanvasNodeExecution,
  label: string,
): string {
  const reason =
    execution.error_message ??
    execution.tasks?.find((task) => task.error_message)?.error_message ??
    null;
  return reason ? `${label}：${reason}` : label;
}
