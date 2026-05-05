"use client";

// Editorial 步骤侧栏：8 步进度脉络。
// - 顶部进度数字（紧凑 tabular）
// - 列表：N°NN 序号 + 节点圆 + 中文标题 + 状态色
// - 当前步：amber dot pulse + 琥珀文字
// - 移动条：横向滚 chip + 大数字进度

import { CheckCircle2 } from "lucide-react";

import { cn } from "@/lib/utils";
import type { WorkflowRun } from "@/lib/apiClient";
import { STEPS, STEP_INDEX } from "../types";
import { stepOf, workflowProgress } from "../utils";

export function StepRail({ workflow }: { workflow: WorkflowRun }) {
  const currentIndex = STEP_INDEX[workflow.current_step] ?? 0;
  const progress = workflowProgress(workflow);
  return (
    <div className="grid gap-6">
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          Progress
        </p>
        <div className="mt-2 flex items-baseline gap-3">
          <span className="text-[32px] font-semibold leading-none tabular-nums text-[var(--fg-0)]">
            {Math.round(progress * 100)}
          </span>
          <span className="font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            % · {currentIndex + 1} / {STEPS.length}
          </span>
        </div>
        <div className="mt-3 h-px w-full bg-[var(--border)]">
          <div
            className="h-px bg-[var(--amber-400)] transition-[width] duration-[var(--dur-slow)] ease-out"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
      </div>

      <ol className="grid gap-0">
        {STEPS.map((step, index) => {
          const row = stepOf(workflow, step.key);
          const Icon = step.Icon;
          const status = row?.status;
          const isFailed = status === "failed";
          const isApproved =
            status === "approved" || status === "completed" || status === "selected";
          const isCurrent = workflow.current_step === step.key;
          const isPast = index < currentIndex;
          const done = isApproved || isPast;

          return (
            <li key={step.key} className="relative grid grid-cols-[24px_minmax(0,1fr)] gap-x-3 py-2.5">
              <span
                className={cn(
                  "relative flex h-6 w-6 shrink-0 items-center justify-center rounded-full border transition-all duration-[var(--dur-base)]",
                  isCurrent
                    ? "border-[var(--amber-400)] bg-[var(--amber-400)] text-black"
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
                {isCurrent && row?.status === "running" ? (
                  <p className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)]">
                    Running
                  </p>
                ) : isFailed ? (
                  <p className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--danger)]">
                    Failed
                  </p>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// 移动版横向条：大数字进度 + chip 列表
export function MobileStageStrip({ workflow }: { workflow: WorkflowRun }) {
  const currentIndex = STEP_INDEX[workflow.current_step] ?? 0;
  const progress = workflowProgress(workflow);
  return (
    <div className="mb-6 border-y border-[var(--border)] py-4 lg:hidden">
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
            Progress
          </p>
          <p className="mt-1 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-1)]">
            {currentIndex + 1} / {STEPS.length}
          </p>
        </div>
        <div className="flex items-baseline gap-1">
          <span className="font-display text-[28px] italic leading-none tabular-nums text-[var(--amber-300)]">
            {Math.round(progress * 100)}
          </span>
          <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
            %
          </span>
        </div>
      </div>
      <div className="mt-3 h-px w-full bg-[var(--border)]">
        <div
          className="h-px bg-[var(--amber-400)] transition-[width] duration-[var(--dur-slow)] ease-out"
          style={{ width: `${progress * 100}%` }}
        />
      </div>
      <div className="scrollbar-none -mx-1 mt-3 flex gap-1 overflow-x-auto px-1">
        {STEPS.map((step, index) => {
          const isCurrent = workflow.current_step === step.key;
          const isPast = index < currentIndex;
          const Icon = step.Icon;
          return (
            <span
              key={step.key}
              className={cn(
                "inline-flex min-h-9 shrink-0 items-center gap-1.5 px-2 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors",
                isCurrent
                  ? "text-[var(--amber-300)]"
                  : isPast
                    ? "text-[var(--fg-1)]"
                    : "text-[var(--fg-3)]",
              )}
            >
              <Icon className="h-3 w-3" />
              {step.label}
              {isCurrent ? (
                <span aria-hidden className="ml-1 inline-block h-1 w-1 rounded-full bg-[var(--amber-400)] animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]" />
              ) : null}
            </span>
          );
        })}
      </div>
    </div>
  );
}
