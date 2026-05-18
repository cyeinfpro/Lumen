"use client";

import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  Loader2,
} from "lucide-react";

import type {
  BackendGeneration,
  BackendImageMeta,
  WorkflowRun,
  WorkflowStep,
} from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { formatRelativeTime } from "../utils";

type ProgressState = "done" | "active" | "pending" | "failed";

interface ShowcaseTaskProgressProps {
  workflow: WorkflowRun;
  step: WorkflowStep;
  images: BackendImageMeta[];
}

const PREFLIGHT_LABEL: Record<string, string> = {
  queued: "等待场景规划",
  running: "规划镜头与提示词",
  dispatched: "图像任务已派发",
  failed: "场景规划失败",
};

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "生成中",
  succeeded: "已完成",
  failed: "失败",
  canceled: "已取消",
};

const STAGE_LABEL: Record<string, string> = {
  queued: "等待调度",
  understanding: "理解商品与参考图",
  rendering: "上游出图中",
  finalizing: "保存结果",
  provider_selected: "已选择生成通道",
  stream_started: "开始生成",
  partial_received: "收到部分结果",
  final_received: "收到最终结果",
  processing: "处理图像",
  storing: "存储图像",
};

const STATUS_TONE: Record<string, string> = {
  queued: "text-[var(--fg-2)]",
  running: "text-[var(--amber-300)]",
  succeeded: "text-[var(--success)]",
  failed: "text-[var(--danger)]",
  canceled: "text-[var(--fg-3)]",
};

const STATUS_DOT: Record<string, string> = {
  queued: "bg-[var(--fg-3)]",
  running: "bg-[var(--amber-400)]",
  succeeded: "bg-[var(--success)]",
  failed: "bg-[var(--danger)]",
  canceled: "bg-[var(--fg-3)]",
};

export function ShowcaseTaskProgress({
  workflow,
  step,
  images,
}: ShowcaseTaskProgressProps) {
  const taskIds = step.task_ids ?? [];
  const generationsById = new Map(workflow.generations.map((task) => [task.id, task]));
  const bonusByParentId = new Map(
    workflow.generations
      .filter((task) => task.is_dual_race_bonus && task.parent_generation_id)
      .map((task) => [task.parent_generation_id as string, task]),
  );
  const requestedCount =
    numberValue(step.input_json?.active_output_count) ??
    numberValue(step.input_json?.output_count);
  const explicitActiveTaskIds = stringArray(step.input_json?.active_task_ids);
  const currentTaskIds =
    explicitActiveTaskIds.length > 0
      ? explicitActiveTaskIds
      : requestedCount && taskIds.length > requestedCount
        ? taskIds.slice(-requestedCount)
        : taskIds;
  const historyTaskCount = Math.max(0, taskIds.length - currentTaskIds.length);
  const tasks = currentTaskIds.map((taskId, index) => ({
    id: taskId,
    index,
    generation: effectiveGeneration(generationsById.get(taskId), bonusByParentId.get(taskId)),
  }));
  const taskStatuses = tasks
    .map((task) => task.generation?.status)
    .filter((status): status is BackendGeneration["status"] => Boolean(status));
  const runningCount = taskStatuses.filter(
    (status) => status === "queued" || status === "running",
  ).length;
  const succeededCount = taskStatuses.filter((status) => status === "succeeded").length;
  const failedCount = taskStatuses.filter((status) => status === "failed").length;
  const canceledCount = taskStatuses.filter((status) => status === "canceled").length;
  const plannedCount = Math.max(
    explicitActiveTaskIds.length,
    requestedCount ?? 0,
    currentTaskIds.length,
  );
  const targetImageCount = numberValue(step.input_json?.target_image_count);
  const baselineImageCount =
    numberValue(step.input_json?.baseline_image_count) ??
    (typeof targetImageCount === "number" && typeof requestedCount === "number"
      ? Math.max(0, targetImageCount - requestedCount)
      : 0);
  const currentImageCount = Math.max(0, images.length - baselineImageCount);
  const progressCount = Math.max(
    Math.min(plannedCount, currentImageCount),
    Math.min(plannedCount, succeededCount),
  );
  const preflightStatus = stringValue(step.input_json?.preflight_status);
  const phase = resolvePhase({
    preflightStatus,
    taskCount: tasks.length,
    runningCount,
    failedCount,
    canceledCount,
    plannedCount,
    progressCount,
    stepStatus: step.status,
  });
  const percent = progressPercent({
    preflightStatus,
    taskCount: tasks.length,
    plannedCount,
    progressCount,
    stepStatus: step.status,
  });
  const milestones = buildMilestones({
    preflightStatus,
    taskCount: tasks.length,
    runningCount,
    failedCount,
    canceledCount,
    plannedCount,
    progressCount,
    stepStatus: step.status,
  });

  return (
    <section className="border-t border-[var(--border)] py-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            Task Progress
          </p>
          <p className="mt-1 text-[13px] leading-6 text-[var(--fg-1)]">
            {phase}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 font-mono text-[10px] uppercase tracking-[0.16em]">
          <Metric label="完成" value={`${progressCount}/${plannedCount}`} tone="success" />
          {runningCount > 0 ? <Metric label="进行中" value={runningCount} tone="amber" /> : null}
          {failedCount > 0 ? <Metric label="失败" value={failedCount} tone="danger" /> : null}
          {canceledCount > 0 ? <Metric label="取消" value={canceledCount} tone="neutral" /> : null}
          {historyTaskCount > 0 ? <Metric label="历史" value={historyTaskCount} tone="neutral" /> : null}
        </div>
      </div>

      <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <div
          className={cn(
            "h-full rounded-full transition-all duration-500",
            (failedCount > 0 || canceledCount > 0) && runningCount === 0
              ? "bg-[var(--danger)]"
              : "bg-[var(--amber-400)]",
          )}
          style={{ width: `${percent}%` }}
        />
      </div>

      <ol className="mt-4 grid gap-2 md:grid-cols-4">
        {milestones.map((item) => (
          <li
            key={item.label}
            className="flex min-h-12 items-center gap-2 border border-[var(--border)] px-3 py-2"
          >
            <MilestoneIcon state={item.state} />
            <div className="min-w-0">
              <p className="truncate font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
                {item.label}
              </p>
              <p className="mt-0.5 truncate text-[12px] text-[var(--fg-1)]">
                {item.detail}
              </p>
            </div>
          </li>
        ))}
      </ol>

      <div className="mt-4 divide-y divide-[var(--border)] border-y border-[var(--border)]">
        {tasks.length > 0 ? (
          tasks.map((task) => (
            <TaskRow
              key={task.id}
              index={task.index}
              generation={task.generation}
            />
          ))
        ) : (
          <div className="flex min-h-14 items-center justify-between gap-3 px-1 py-3">
            <div className="min-w-0">
              <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
                等待派发图像任务
              </p>
              <p className="mt-1 text-[12px] text-[var(--fg-1)]">
                {PREFLIGHT_LABEL[preflightStatus ?? "queued"] ?? "已提交生成请求"}
              </p>
            </div>
            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-[var(--amber-300)]" />
          </div>
        )}
      </div>
    </section>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: number | string;
  tone: "success" | "amber" | "danger" | "neutral";
}) {
  const toneClass =
    tone === "success"
      ? "text-[var(--success)]"
      : tone === "danger"
        ? "text-[var(--danger)]"
        : tone === "amber"
          ? "text-[var(--amber-300)]"
          : "text-[var(--fg-1)]";
  return (
    <span className="inline-flex min-h-7 items-center gap-1.5 border border-[var(--border)] px-2 text-[var(--fg-2)]">
      <span className={cn("tabular-nums", toneClass)}>{value}</span>
      {label}
    </span>
  );
}

function TaskRow({
  index,
  generation,
}: {
  index: number;
  generation?: BackendGeneration;
}) {
  const status = generation?.status ?? "queued";
  const stage = generation?.progress_stage ?? "queued";
  const time =
    generation?.finished_at ??
    generation?.started_at ??
    null;
  const detail = [
    STAGE_LABEL[stage] ?? stage,
    generation?.attempt ? `第 ${generation.attempt} 次尝试` : null,
    time ? formatRelativeTime(time) : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="grid min-h-14 gap-2 px-1 py-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            Image {String(index + 1).padStart(2, "0")}
          </p>
          <span
            className={cn(
              "inline-flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.14em]",
              STATUS_TONE[status],
            )}
          >
            <span
              aria-hidden
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                STATUS_DOT[status],
                status === "running" && "animate-pulse",
              )}
            />
            {STATUS_LABEL[status] ?? status}
          </span>
        </div>
        <p className="mt-1 truncate text-[12px] text-[var(--fg-1)]">
          {generation ? detail : "任务已登记，等待状态同步"}
        </p>
        {generation?.error_message ? (
          <p
            role="alert"
            className="mt-1 line-clamp-2 text-[12px] leading-5 text-[var(--danger)]"
          >
            {generation.error_message}
          </p>
        ) : null}
      </div>
      <TaskIcon status={status} />
    </div>
  );
}

function effectiveGeneration(
  base?: BackendGeneration,
  bonus?: BackendGeneration,
): BackendGeneration | undefined {
  if (!bonus) return base;
  if (!base) return bonus;
  if (base.status === "succeeded") return base;
  if (bonus.status === "succeeded") return bonus;
  if (
    (base.status === "failed" || base.status === "canceled") &&
    (bonus.status === "queued" || bonus.status === "running")
  ) {
    return bonus;
  }
  if (
    (base.status === "failed" || base.status === "canceled") &&
    (bonus.status === "failed" || bonus.status === "canceled")
  ) {
    return bonus;
  }
  return base;
}

function TaskIcon({ status }: { status: string }) {
  if (status === "succeeded") {
    return <CheckCircle2 className="h-4 w-4 text-[var(--success)]" />;
  }
  if (status === "failed" || status === "canceled") {
    return <AlertCircle className="h-4 w-4 text-[var(--danger)]" />;
  }
  if (status === "running") {
    return <Loader2 className="h-4 w-4 animate-spin text-[var(--amber-300)]" />;
  }
  return <Clock3 className="h-4 w-4 text-[var(--fg-2)]" />;
}

function MilestoneIcon({ state }: { state: ProgressState }) {
  if (state === "done") return <CheckCircle2 className="h-4 w-4 text-[var(--success)]" />;
  if (state === "failed") return <AlertCircle className="h-4 w-4 text-[var(--danger)]" />;
  if (state === "active") return <Loader2 className="h-4 w-4 animate-spin text-[var(--amber-300)]" />;
  return <Clock3 className="h-4 w-4 text-[var(--fg-3)]" />;
}

function buildMilestones({
  preflightStatus,
  taskCount,
  runningCount,
  failedCount,
  canceledCount,
  plannedCount,
  progressCount,
  stepStatus,
}: {
  preflightStatus: string | null;
  taskCount: number;
  runningCount: number;
  failedCount: number;
  canceledCount: number;
  plannedCount: number;
  progressCount: number;
  stepStatus: string;
}): Array<{ label: string; detail: string; state: ProgressState }> {
  const preflightFailed = preflightStatus === "failed" || stepStatus === "failed";
  const dispatchDone = taskCount > 0 || stepStatus === "completed";
  const outputDone = plannedCount > 0 && progressCount >= plannedCount;
  const terminalProblemCount = failedCount + canceledCount;

  return [
    {
      label: "Submitted",
      detail: "生成请求已提交",
      state: "done" as ProgressState,
    },
    {
      label: "Planning",
      detail: PREFLIGHT_LABEL[preflightStatus ?? "queued"] ?? "等待场景规划",
      state: preflightFailed
        ? "failed"
        : dispatchDone
          ? "done"
          : "active",
    },
    {
      label: "Queue",
      detail: taskCount > 0 ? `${taskCount} 条任务` : "等待派发",
      state: preflightFailed
        ? "failed"
        : taskCount > 0
          ? runningCount > 0
            ? "active"
            : "done"
          : "pending",
    },
    {
      label: "Outputs",
      detail: `${progressCount}/${plannedCount} 张`,
      state:
        terminalProblemCount > 0 && runningCount === 0 && !outputDone
          ? "failed"
          : outputDone
            ? "done"
            : taskCount > 0
              ? "active"
              : "pending",
    },
  ];
}

function resolvePhase({
  preflightStatus,
  taskCount,
  runningCount,
  failedCount,
  canceledCount,
  plannedCount,
  progressCount,
  stepStatus,
}: {
  preflightStatus: string | null;
  taskCount: number;
  runningCount: number;
  failedCount: number;
  canceledCount: number;
  plannedCount: number;
  progressCount: number;
  stepStatus: string;
}): string {
  if (stepStatus === "failed" || preflightStatus === "failed") return "生成任务失败";
  if (plannedCount > 0 && progressCount >= plannedCount) return "本轮成品图已完成";
  if (taskCount === 0) {
    return PREFLIGHT_LABEL[preflightStatus ?? "queued"] ?? "等待场景规划";
  }
  if (runningCount > 0) return "图像任务正在生成";
  if (failedCount > 0) return "部分图像任务失败";
  if (canceledCount > 0) return "部分图像任务已取消";
  return "等待任务状态同步";
}

function progressPercent({
  preflightStatus,
  taskCount,
  plannedCount,
  progressCount,
  stepStatus,
}: {
  preflightStatus: string | null;
  taskCount: number;
  plannedCount: number;
  progressCount: number;
  stepStatus: string;
}): number {
  if (stepStatus === "failed" || preflightStatus === "failed") return 100;
  if (plannedCount > 0 && progressCount >= plannedCount) return 100;
  if (taskCount === 0) {
    return preflightStatus === "running" ? 18 : 8;
  }
  const taskProgress = plannedCount > 0 ? progressCount / plannedCount : 0;
  return Math.max(25, Math.min(98, Math.round(25 + taskProgress * 70)));
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.length > 0)
    : [];
}
