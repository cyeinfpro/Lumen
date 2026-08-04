"use client";

// Editorial 步骤侧栏：8 步进度脉络。
// - 顶部进度数字（紧凑 tabular）
// - 列表：N°NN 序号 + 节点圆 + 中文标题 + 状态色
// - 当前步：amber dot pulse + 琥珀文字
// - 移动条：横向滚 chip + 大数字进度

import { cn } from "@/lib/utils";
import type { WorkflowRun } from "@/lib/apiClient";
import { STEPS, STEP_INDEX } from "../types";
import { workflowProgress } from "../utils";
import { WorkflowStepRailRow } from "./WorkflowStepRailRow";

export function StepRail({ workflow }: { workflow: WorkflowRun }) {
  const currentIndex = STEP_INDEX[workflow.current_step] ?? 0;
  const progress = workflowProgress(workflow);
  return (
    <div className="grid gap-6">
      <div>
        <p className="type-caption text-[var(--fg-2)]">
          进度
        </p>
        <div className="mt-2 flex items-baseline gap-3">
          <span className="type-metric ">
            {Math.round(progress * 100)}
          </span>
          <span className="type-caption text-[var(--fg-2)]">
            % · {currentIndex + 1} / {STEPS.length}
          </span>
        </div>
        <div className="mt-3 h-0.5 w-full overflow-hidden rounded-full bg-[var(--bg-3)]">
          <div
            className="h-full rounded-full bg-accent transition-[width] duration-[var(--dur-slow)] ease-out"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
      </div>

      <ol className="grid gap-0">
        {STEPS.map((step, index) => (
          <WorkflowStepRailRow
            key={step.key}
            workflow={workflow}
            step={step}
            index={index}
            currentIndex={currentIndex}
          />
        ))}
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
          <p className="type-caption text-[var(--fg-2)]">
            进度
          </p>
          <p className="mt-1 type-caption text-[var(--fg-1)]">
            {currentIndex + 1} / {STEPS.length}
          </p>
        </div>
        <div className="flex items-baseline gap-1">
          <span className="type-metric  text-accent">
            {Math.round(progress * 100)}
          </span>
          <span className="type-caption text-[var(--fg-2)]">
            %
          </span>
        </div>
      </div>
      <div className="mt-3 h-0.5 w-full overflow-hidden rounded-full bg-[var(--bg-3)]">
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-[var(--dur-slow)] ease-out"
          style={{ width: `${progress * 100}%` }}
        />
      </div>
      <div className="scrollbar-none -mx-1 mt-3 flex snap-x snap-mandatory gap-1 overflow-x-auto px-1 pb-0.5">
        {STEPS.map((step, index) => {
          const isCurrent = workflow.current_step === step.key;
          const isPast = index < currentIndex;
          const Icon = step.Icon;
          return (
            <span
              key={step.key}
              className={cn(
                "inline-flex min-h-11 shrink-0 snap-start items-center gap-1.5 px-2 type-caption transition-colors md:min-h-9",
                isCurrent
                  ? "text-accent"
                  : isPast
                    ? "text-[var(--fg-1)]"
                    : "text-[var(--fg-3)]",
              )}
            >
              <Icon className="h-3 w-3" />
              {step.label}
              {isCurrent ? (
                <span aria-hidden className="ml-1 inline-block h-1 w-1 rounded-full bg-accent animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]" />
              ) : null}
            </span>
          );
        })}
      </div>
    </div>
  );
}
