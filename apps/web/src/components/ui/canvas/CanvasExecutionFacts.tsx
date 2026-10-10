"use client";

import type { CanvasNodeExecution } from "@/lib/canvas/types";
import {
  canvasBillingRows,
  canvasRecoveryExplanation,
  canvasRecoveryLabel,
} from "@/lib/canvas/generationDetails";
import { Button } from "@/components/ui/primitives";

export function CanvasExecutionFacts({
  execution, querying, onQuery,
}: {
  execution: CanvasNodeExecution;
  querying: boolean;
  onQuery: () => void;
}) {
  const tasks = (execution.tasks ?? []).filter((task) => task.recovery);
  if (tasks.length === 0 && !execution.billing) return null;
  return (
    <section aria-label="任务恢复与费用" className="mt-2 grid gap-2">
      {tasks.map((task, index) => {
        const recovery = task.recovery!;
        const explanation = canvasRecoveryExplanation(recovery);
        return (
          <div key={task.id || index} className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-3"
            data-canvas-recovery-state={recovery.state}>
            <p className="type-caption font-medium text-[var(--fg-1)]">
              {tasks.length > 1 ? `任务 ${index + 1} · ` : ""}{canvasRecoveryLabel(recovery)}
            </p>
            {explanation ? <p className="mt-1 type-caption text-[var(--fg-muted-aa)]">{explanation}</p> : null}
          </div>
        );
      })}
      {tasks.some((task) => task.recovery?.can_query) ? (
        <Button variant="secondary" className="min-h-11 w-full" disabled={querying}
          aria-busy={querying} onClick={onQuery}>
          {querying ? "正在查询…" : "查询原任务状态"}
        </Button>
      ) : null}
      {execution.billing ? (
        <details className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] px-3">
          <summary className="flex min-h-11 cursor-pointer items-center type-caption font-medium text-[var(--fg-1)]">
            费用记录{execution.billing.task_count !== undefined ? ` · ${execution.billing.task_count} 个任务` : ""}
          </summary>
          <dl className="grid gap-2 pb-3 type-caption">
            {canvasBillingRows(execution.billing).map(([label, value]) => (
              <div key={label} className="grid grid-cols-[72px_minmax(0,1fr)] gap-2">
                <dt className="text-[var(--fg-muted-aa)]">{label}</dt>
                <dd className="break-words text-[var(--fg-1)]">{value}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </section>
  );
}
