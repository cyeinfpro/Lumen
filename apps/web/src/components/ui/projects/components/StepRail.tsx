"use client";

// 桌面侧栏的 8 步进度脉络。
// 视觉：
//   • 圆点（已完成 / 当前 / 待办）+ 连接线
//   • 当前步骤 lumen-pulse-soft 呼吸 + 琥珀辉光
//   • 已完成填充琥珀；待办灰色虚线连接
//   • 完成度百分比顶在最上方（光圈进度条）

import { CheckCircle2 } from "lucide-react";

import { cn } from "@/lib/utils";
import type { WorkflowRun } from "@/lib/apiClient";
import { STEPS, STEP_INDEX } from "../types";
import { stepOf, workflowProgress } from "../utils";

export function StepRail({ workflow }: { workflow: WorkflowRun }) {
  const currentIndex = STEP_INDEX[workflow.current_step] ?? 0;
  const progress = workflowProgress(workflow);
  return (
    <div className="space-y-3">
      <div>
        <p className="text-[11px] tracking-[0.16em] text-[var(--fg-2)]">闭环进度</p>
        <div className="mt-2 flex items-center justify-between">
          <span className="text-sm font-medium tabular-nums text-[var(--fg-0)]">
            {Math.round(progress * 100)}%
          </span>
          <span className="text-[11px] text-[var(--fg-2)]">
            {currentIndex + 1} / {STEPS.length}
          </span>
        </div>
        <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/[0.06]">
          <div
            className="h-full rounded-full bg-[var(--accent)] shadow-[var(--shadow-amber)] transition-[width] duration-[var(--dur-slow)] ease-out"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
      </div>

      <ol className="relative space-y-0">
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
          const last = index === STEPS.length - 1;

          return (
            <li key={step.key} className="relative flex gap-3 pb-3">
              {/* 连接线 */}
              {!last ? (
                <span
                  aria-hidden
                  className={cn(
                    "absolute left-[14px] top-7 h-[calc(100%-1.25rem)] w-px",
                    done
                      ? "bg-gradient-to-b from-[var(--accent)] to-[var(--border-amber)]"
                      : "bg-[var(--border)]",
                  )}
                />
              ) : null}

              {/* 节点圆 */}
              <span
                className={cn(
                  "relative z-[1] flex h-7 w-7 shrink-0 items-center justify-center rounded-full border transition-all duration-[var(--dur-base)]",
                  isCurrent
                    ? "border-[var(--border-amber)] bg-[var(--accent)] text-black shadow-[var(--shadow-amber)]"
                    : done
                      ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                      : isFailed
                        ? "border-[var(--danger)]/40 bg-[var(--danger-soft)] text-[var(--danger)]"
                        : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]",
                )}
              >
                {done && !isCurrent ? (
                  <CheckCircle2 className="h-3.5 w-3.5" />
                ) : (
                  <Icon className="h-3.5 w-3.5" />
                )}
                {isCurrent ? (
                  <span className="absolute inset-0 -z-[1] animate-ping rounded-full bg-[var(--amber-glow)] opacity-50" />
                ) : null}
              </span>

              <div className="min-w-0 flex-1 pt-0.5">
                <div className="flex items-center justify-between gap-2">
                  <p
                    className={cn(
                      "truncate text-[13px]",
                      isCurrent
                        ? "font-medium text-[var(--fg-0)]"
                        : done
                          ? "text-[var(--fg-1)]"
                          : "text-[var(--fg-2)]",
                    )}
                  >
                    {step.label}
                  </p>
                  <span className="text-[10px] tabular-nums text-[var(--fg-3)]">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                </div>
                {isCurrent && row?.status === "running" ? (
                  <p className="mt-0.5 text-[11px] text-[var(--amber-300)]">运行中…</p>
                ) : isFailed ? (
                  <p className="mt-0.5 text-[11px] text-[var(--danger)]">失败，可在该阶段重试</p>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// 移动版横向条：上下箭头 + chip 列表 + 简化连接线。
export function MobileStageStrip({ workflow }: { workflow: WorkflowRun }) {
  const currentIndex = STEP_INDEX[workflow.current_step] ?? 0;
  const progress = workflowProgress(workflow);
  return (
    <div className="mb-4 rounded-xl border border-[var(--border)] bg-white/[0.028] p-3 lg:hidden">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-[11px] tracking-[0.16em] text-[var(--fg-2)]">闭环进度</p>
          <p className="mt-1 text-sm font-medium text-[var(--fg-0)]">
            {currentIndex + 1} / {STEPS.length}
          </p>
        </div>
        <span className="font-mono text-[18px] text-[var(--amber-300)]">
          {Math.round(progress * 100)}%
        </span>
      </div>
      <div className="mt-3 h-1 overflow-hidden rounded-full bg-white/[0.06]">
        <div
          className="h-full rounded-full bg-[var(--accent)] shadow-[var(--shadow-amber)] transition-[width] duration-[var(--dur-slow)] ease-out"
          style={{ width: `${progress * 100}%` }}
        />
      </div>
      <div className="scrollbar-none -mx-1 mt-3 flex gap-2 overflow-x-auto px-1 pb-0.5">
        {STEPS.map((step, index) => {
          const isCurrent = workflow.current_step === step.key;
          const isPast = index < currentIndex;
          const Icon = step.Icon;
          return (
            <span
              key={step.key}
              className={cn(
                "inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs transition-colors",
                isCurrent
                  ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                  : isPast
                    ? "border-[var(--border-amber)]/40 text-[var(--fg-1)]"
                    : "border-[var(--border)] text-[var(--fg-2)]",
              )}
            >
              <Icon className="h-3 w-3" />
              {step.label}
            </span>
          );
        })}
      </div>
    </div>
  );
}
