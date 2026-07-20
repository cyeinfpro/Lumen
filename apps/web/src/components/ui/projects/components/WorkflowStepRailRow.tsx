"use client";

import { CheckCircle2, type LucideIcon } from "lucide-react";

import type { WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { stepOf } from "../utils";

interface WorkflowStepRailRowProps {
  workflow: WorkflowRun;
  step: {
    key: string;
    label: string;
    Icon: LucideIcon;
  };
  index: number;
  currentIndex: number;
}

function stepState(
  workflow: WorkflowRun,
  stepKey: string,
  index: number,
  currentIndex: number,
) {
  const status = stepOf(workflow, stepKey)?.status;
  const isFailed = status === "failed";
  const isApproved =
    status === "approved" || status === "completed" || status === "selected";
  const isCurrent = workflow.current_step === stepKey;
  return {
    status,
    isFailed,
    isCurrent,
    done: isApproved || index < currentIndex,
  };
}

function statusLabel(
  status: string | undefined,
  isCurrent: boolean,
  isFailed: boolean,
) {
  if (isCurrent && status === "running") {
    return (
      <p className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)]">
        Running
      </p>
    );
  }
  if (isFailed) {
    return (
      <p className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--danger)]">
        失败
      </p>
    );
  }
  return null;
}

export function WorkflowStepRailRow({
  workflow,
  step,
  index,
  currentIndex,
}: WorkflowStepRailRowProps) {
  const { status, isFailed, isCurrent, done } = stepState(
    workflow,
    step.key,
    index,
    currentIndex,
  );
  const Icon = step.Icon;
  return (
    <li className="relative grid grid-cols-[24px_minmax(0,1fr)] gap-x-3 py-2.5">
      <span
        className={cn(
          "relative flex h-6 w-6 shrink-0 items-center justify-center rounded-full border transition-all duration-[var(--dur-base)]",
          isCurrent
            ? "border-[var(--amber-400)] bg-[var(--amber-400)] text-[var(--accent-on)]"
            : done
              ? "border-[var(--border-amber)] bg-transparent text-[var(--amber-300)]"
              : isFailed
                ? "border-[var(--danger)]/40 text-[var(--danger)]"
                : "border-[var(--border)] text-[var(--fg-3)]",
        )}
      >
        {done && !isCurrent ? (
          <CheckCircle2 className="h-3 w-3" />
        ) : (
          <Icon className="h-3 w-3" />
        )}
        {isCurrent ? (
          <span
            aria-hidden
            className="absolute inset-0 -z-[1] animate-ping rounded-full bg-[var(--amber-glow)] opacity-50"
          />
        ) : null}
      </span>

      <div className="min-w-0 pt-0.5">
        <div className="flex items-baseline justify-between gap-2">
          <p
            className={cn(
              "truncate text-[13px] transition-colors",
              isCurrent
                ? "font-medium text-[var(--fg-0)]"
                : done
                  ? "text-[var(--fg-1)]"
                  : "text-[var(--fg-2)]",
            )}
          >
            {step.label}
          </p>
          <span className="shrink-0 font-mono text-[10px] tabular-nums text-[var(--fg-3)]">
            {String(index + 1).padStart(2, "0")}
          </span>
        </div>
        {statusLabel(status, isCurrent, isFailed)}
      </div>
    </li>
  );
}
