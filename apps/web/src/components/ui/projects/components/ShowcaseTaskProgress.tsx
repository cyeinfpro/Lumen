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
import {
  buildShowcaseProgressModel,
  type ProgressState,
} from "./ShowcaseTaskProgressModel";

interface ShowcaseTaskProgressProps {
  workflow: WorkflowRun;
  step: WorkflowStep;
  images: BackendImageMeta[];
}

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
  running: "text-accent",
  succeeded: "text-[var(--success)]",
  failed: "text-[var(--danger)]",
  canceled: "text-[var(--fg-3)]",
};

const STATUS_DOT: Record<string, string> = {
  queued: "bg-[var(--fg-3)]",
  running: "bg-accent",
  succeeded: "bg-[var(--success)]",
  failed: "bg-[var(--danger)]",
  canceled: "bg-[var(--fg-3)]",
};

export function ShowcaseTaskProgress({
  workflow,
  step,
  images,
}: ShowcaseTaskProgressProps) {
  const model = buildShowcaseProgressModel(workflow, step, images);
  return (
    <section className="border-t border-[var(--border)] py-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="type-caption text-[var(--fg-2)]">
            任务进度
          </p>
          <p className="mt-1 type-body-sm leading-6 text-[var(--fg-1)]">
            {model.phase}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 type-caption">
          <Metric
            label="完成"
            value={`${model.progressCount}/${model.plannedCount}`}
            tone="success"
          />
          {model.runningCount > 0 ? (
            <Metric label="进行中" value={model.runningCount} tone="amber" />
          ) : null}
          {model.failedCount > 0 ? (
            <Metric label="失败" value={model.failedCount} tone="danger" />
          ) : null}
          {model.canceledCount > 0 ? (
            <Metric label="取消" value={model.canceledCount} tone="neutral" />
          ) : null}
          {model.historyTaskCount > 0 ? (
            <Metric
              label="历史"
              value={model.historyTaskCount}
              tone="neutral"
            />
          ) : null}
        </div>
      </div>

      <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <div
          className={cn(
            "h-full rounded-full transition-all duration-500",
            (model.failedCount > 0 || model.canceledCount > 0) &&
              model.runningCount === 0
              ? "bg-[var(--danger)]"
              : "bg-accent",
          )}
          style={{ width: `${model.percent}%` }}
        />
      </div>

      <ol className="mt-4 grid gap-2 md:grid-cols-4">
        {model.milestones.map((item) => (
          <li
            key={item.label}
            className="flex min-h-12 items-center gap-2 border border-[var(--border)] px-3 py-2"
          >
            <MilestoneIcon state={item.state} />
            <div className="min-w-0">
              <p className="truncate type-caption text-[var(--fg-2)]">
                {item.label}
              </p>
              <p className="mt-0.5 truncate type-caption text-[var(--fg-1)]">
                {item.detail}
              </p>
            </div>
          </li>
        ))}
      </ol>

      <div className="mt-4 divide-y divide-[var(--border)] border-y border-[var(--border)]">
        {model.tasks.length > 0 ? (
          model.tasks.map((task) => (
            <TaskRow
              key={task.id}
              index={task.index}
              generation={task.generation}
            />
          ))
        ) : (
          <div className="flex min-h-14 items-center justify-between gap-3 px-1 py-3">
            <div className="min-w-0">
              <p className="type-caption text-[var(--fg-2)]">
                等待派发图像任务
              </p>
              <p className="mt-1 type-caption text-[var(--fg-1)]">
                {model.preflightDisplay}
              </p>
            </div>
            <Loader2 className="h-4 w-4 shrink-0 animate-spin text-accent" />
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
          ? "text-accent"
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
  const presentation = taskRowPresentation(generation);
  return (
    <div className="grid min-h-14 gap-2 px-1 py-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="type-caption text-[var(--fg-2)]">
            图像 {String(index + 1).padStart(2, "0")}
          </p>
          <span
            className={cn(
              "inline-flex items-center gap-1.5 type-caption",
              STATUS_TONE[presentation.status],
            )}
          >
            <span
              aria-hidden
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                STATUS_DOT[presentation.status],
                presentation.status === "running" && "animate-pulse",
              )}
            />
            {STATUS_LABEL[presentation.status] ?? presentation.status}
          </span>
        </div>
        <p className="mt-1 truncate type-caption text-[var(--fg-1)]">
          {presentation.detail}
        </p>
        {presentation.error ? (
          <p
            role="alert"
            className="mt-1 line-clamp-2 type-caption leading-5 text-[var(--danger)]"
          >
            {presentation.error}
          </p>
        ) : null}
      </div>
      <TaskIcon status={presentation.status} />
    </div>
  );
}

function taskRowPresentation(generation?: BackendGeneration) {
  const status = generation?.status ?? "queued";
  if (!generation) {
    return {
      detail: "任务已登记，等待状态同步",
      error: null,
      status,
    };
  }
  const stage = generation.progress_stage ?? "queued";
  const time = generation.finished_at ?? generation.started_at ?? null;
  const detail = [
    STAGE_LABEL[stage] ?? stage,
    generation.attempt ? `第 ${generation.attempt} 次尝试` : null,
    time ? formatRelativeTime(time) : null,
  ]
    .filter(Boolean)
    .join(" · ");
  return {
    detail,
    error: generation.error_message,
    status,
  };
}

function TaskIcon({ status }: { status: string }) {
  if (status === "succeeded") {
    return <CheckCircle2 className="h-4 w-4 text-[var(--success)]" />;
  }
  if (status === "failed" || status === "canceled") {
    return <AlertCircle className="h-4 w-4 text-[var(--danger)]" />;
  }
  if (status === "running") {
    return (
      <Loader2 className="h-4 w-4 animate-spin text-accent" />
    );
  }
  return <Clock3 className="h-4 w-4 text-[var(--fg-2)]" />;
}

function MilestoneIcon({ state }: { state: ProgressState }) {
  if (state === "done") {
    return <CheckCircle2 className="h-4 w-4 text-[var(--success)]" />;
  }
  if (state === "failed") {
    return <AlertCircle className="h-4 w-4 text-[var(--danger)]" />;
  }
  if (state === "active") {
    return (
      <Loader2 className="h-4 w-4 animate-spin text-accent" />
    );
  }
  return <Clock3 className="h-4 w-4 text-[var(--fg-3)]" />;
}
