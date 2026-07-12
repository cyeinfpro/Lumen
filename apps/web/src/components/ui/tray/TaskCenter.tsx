"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Clock3,
  ImageIcon,
  MessageSquareText,
  RefreshCw,
  RotateCw,
  X,
} from "lucide-react";

import {
  cancelTask,
  listTasks,
  retryTask,
  type TaskItemResponse,
} from "@/lib/apiClient";
import { errorCodeToFullText, recommendedActionsForError } from "@/lib/errors";
import type { Generation, RecommendedErrorAction } from "@/lib/types";
import { logWarn } from "@/lib/logger";
import { cn } from "@/lib/utils";
import { IconButton } from "@/components/ui/primitives";
import { TaskItem } from "./TaskItem";

type TaskFilter = "all" | "active" | "failed";

interface TaskCenterProps {
  activeGenerations: Generation[];
  localGenerations: Record<string, Generation>;
  onCancelGeneration: (gen: Generation) => void;
  onRetryGeneration: (gen: Generation) => void;
  onViewGeneration: (gen: Generation) => void;
  onClose: () => void;
}

const FILTERS: Array<{ key: TaskFilter; label: string }> = [
  { key: "all", label: "最近" },
  { key: "active", label: "进行中" },
  { key: "failed", label: "失败" },
];

const SOURCE_LABEL: Record<string, string> = {
  chat: "聊天",
  project: "项目",
  telegram: "Telegram",
};

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "生成中",
  streaming: "回复中",
  succeeded: "已完成",
  failed: "失败",
  canceled: "已取消",
};

const SUBSTAGE_LABEL: Record<string, string> = {
  waiting_queue: "排队中",
  waiting_provider: "等待可用通道",
  upstream_retrying: "上游重试中",
  preparing_refs: "准备参考图",
  upstream_started: "模型生成中",
  postprocessing: "图片后处理中",
  processing: "图片后处理中",
  storing: "保存图片中",
  display_ready: "图片已完成",
  retryable: "失败，可重试",
  terminal: "失败",
  cancelled: "已取消",
  completed: "已完成",
};

function taskStatusText(task: TaskItemResponse): string {
  const base =
    (task.substage && SUBSTAGE_LABEL[task.substage]) ||
    STATUS_LABEL[task.status] ||
    task.status;
  if (
    task.status === "queued" &&
    task.queue_position != null &&
    task.queue_position > 0
  ) {
    return `${base} · 第 ${task.queue_position} 位`;
  }
  return base;
}

function taskTime(task: TaskItemResponse): string {
  const raw =
    task.finished_at ?? task.started_at ?? task.created_at ?? task.date;
  if (!raw) return "";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function actionList(task: TaskItemResponse): RecommendedErrorAction[] {
  return task.recommended_actions?.length
    ? task.recommended_actions
    : recommendedActionsForError(task.error_code, {
        retryable: task.retryable,
        status: task.status,
      });
}

function taskKindPath(task: TaskItemResponse): "generations" | "completions" {
  return task.kind === "generation" ? "generations" : "completions";
}

function TaskRecoveryActions({
  actions,
  retryable,
  busy,
  onRetry,
}: {
  actions: RecommendedErrorAction[];
  retryable: boolean;
  busy: boolean;
  onRetry: () => void;
}) {
  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {actions.map((action) => {
        if (action.kind === "retry" && retryable) {
          return (
            <button
              key={action.id}
              type="button"
              disabled={busy}
              onClick={onRetry}
              className="inline-flex min-h-11 items-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 text-[11px] text-[var(--fg-1)] hover:text-[var(--fg-0)] disabled:opacity-50"
            >
              {action.label}
            </button>
          );
        }
        if (action.kind === "link" && action.href) {
          return (
            <a
              key={action.id}
              href={action.href}
              className="inline-flex min-h-11 items-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 text-[11px] text-[var(--fg-1)] hover:text-[var(--fg-0)]"
            >
              {action.label}
            </a>
          );
        }
        return (
          <span
            key={action.id}
            className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-[11px] text-[var(--fg-2)]"
          >
            {action.label}
          </span>
        );
      })}
    </div>
  );
}

function TaskHistoryThumb({ task }: { task: TaskItemResponse }) {
  if (task.thumb_url) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img src={task.thumb_url} alt="" className="h-full w-full object-cover" />
    );
  }
  return task.kind === "generation" ? (
    <ImageIcon className="h-4 w-4 text-[var(--fg-2)]" />
  ) : (
    <MessageSquareText className="h-4 w-4 text-[var(--fg-2)]" />
  );
}

export function TaskCenter({
  activeGenerations,
  localGenerations,
  onCancelGeneration,
  onRetryGeneration,
  onViewGeneration,
  onClose,
}: TaskCenterProps) {
  const [filter, setFilter] = useState<TaskFilter>("all");
  const qc = useQueryClient();
  const status =
    filter === "active" ? "active" : filter === "failed" ? "failed" : undefined;
  const query = useQuery({
    queryKey: ["tasks", "recent", status ?? "all"],
    queryFn: ({ signal }) => listTasks({ status, limit: 80 }, { signal }),
    staleTime: 8_000,
  });

  const retryMutation = useMutation({
    mutationFn: async (task: TaskItemResponse) => {
      await retryTask(taskKindPath(task), task.id);
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (err, task) => {
      logWarn("task-center.retry_failed", {
        scope: "tray",
        extra: { taskId: task.id, err: String(err) },
      });
    },
  });

  const cancelMutation = useMutation({
    mutationFn: async (task: TaskItemResponse) => {
      await cancelTask(taskKindPath(task), task.id);
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (err, task) => {
      logWarn("task-center.cancel_failed", {
        scope: "tray",
        extra: { taskId: task.id, err: String(err) },
      });
    },
  });

  const serverItems = query.data?.items ?? [];
  const activeIds = useMemo(
    () => new Set(activeGenerations.map((gen) => gen.id)),
    [activeGenerations],
  );
  const historyItems = serverItems.filter((item) => !activeIds.has(item.id));
  const visibleActive =
    filter === "failed"
      ? []
      : activeGenerations.slice(0, filter === "active" ? 12 : 5);
  const visibleHistory = historyItems.slice(0, filter === "active" ? 20 : 40);
  const isEmpty =
    visibleActive.length === 0 &&
    visibleHistory.length === 0 &&
    !query.isLoading;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2.5">
        <span
          className={cn(
            "h-2 w-2 shrink-0 rounded-full",
            activeGenerations.length > 0
              ? "animate-pulse bg-[var(--accent)]"
              : "bg-[var(--ok)]",
          )}
        />
        <h4 className="min-w-0 flex-1 truncate text-xs font-medium text-[var(--fg-0)]">
          任务中心
        </h4>
        <IconButton
          variant="ghost"
          size="sm"
          tooltip="刷新任务"
          onClick={() => void query.refetch()}
          aria-label="刷新任务"
        >
          <RefreshCw
            className={cn("h-3.5 w-3.5", query.isFetching && "animate-spin")}
          />
        </IconButton>
        <IconButton
          variant="ghost"
          size="sm"
          tooltip="收起任务中心"
          onClick={onClose}
          aria-label="收起任务中心"
        >
          <X className="h-3.5 w-3.5" />
        </IconButton>
      </div>

      <div className="flex shrink-0 gap-1 border-b border-[var(--border-subtle)] px-2 py-2">
        {FILTERS.map((item) => (
          <button
            key={item.key}
            type="button"
            onClick={() => setFilter(item.key)}
            className={cn(
              "min-h-11 flex-1 rounded-[var(--radius-control)] px-2 text-xs transition",
              filter === item.key
                ? "bg-[var(--accent-soft)] text-[var(--accent)]"
                : "text-[var(--fg-2)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="mobile-dialog-scroll min-h-0 flex-1 space-y-1.5 overflow-y-auto p-2 sm:max-h-[56vh]">
        {visibleActive.map((gen) => (
          <TaskItem
            key={gen.id}
            gen={gen}
            onCancel={onCancelGeneration}
            onRetry={onRetryGeneration}
            onView={onViewGeneration}
          />
        ))}

        {visibleHistory.map((task) => {
          const local =
            task.kind === "generation" ? localGenerations[task.id] : undefined;
          if (local) {
            return (
              <TaskItem
                key={task.id}
                gen={local}
                onCancel={onCancelGeneration}
                onRetry={onRetryGeneration}
                onView={onViewGeneration}
              />
            );
          }
          return (
            <TaskHistoryRow
              key={task.id}
              task={task}
              busy={retryMutation.isPending || cancelMutation.isPending}
              onRetry={() => retryMutation.mutate(task)}
              onCancel={() => cancelMutation.mutate(task)}
            />
          );
        })}

        {query.isLoading && (
          <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] p-3 text-xs text-[var(--fg-2)]">
            正在读取最近任务
          </div>
        )}
        {isEmpty && (
          <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] p-3 text-xs text-[var(--fg-2)]">
            暂无任务
          </div>
        )}
      </div>
    </div>
  );
}

function TaskHistoryRow({
  task,
  busy,
  onRetry,
  onCancel,
}: {
  task: TaskItemResponse;
  busy: boolean;
  onRetry: () => void;
  onCancel: () => void;
}) {
  const active =
    task.status === "queued" ||
    task.status === "running" ||
    task.status === "streaming";
  const failed = task.status === "failed";
  const recoverable = failed || task.status === "canceled";
  const succeeded = task.status === "succeeded";
  const actions = actionList(task);
  const title =
    task.title ||
    task.prompt ||
    (task.kind === "generation" ? "图像生成" : "文本回复");
  const errorText =
    failed && task.error_code
      ? (errorCodeToFullText(task.error_code) ?? task.error_message)
      : task.error_message;

  return (
    <div
      className={cn(
        "rounded-[var(--radius-card)] border p-2 transition",
        failed
          ? "border-danger-border bg-danger-soft"
          : "border-[var(--border)] bg-[var(--bg-1)]",
      )}
    >
      <div className="flex gap-2">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center overflow-hidden rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)]">
          <TaskHistoryThumb task={task} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-1.5">
            <p className="truncate text-[13px] font-medium text-[var(--fg-0)]">
              {title}
            </p>
            {succeeded && (
              <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-[var(--ok)]" />
            )}
            {active && (
              <Clock3 className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
            )}
          </div>
          <p
            className={cn(
              "mt-0.5 text-[11px]",
              failed ? "text-danger" : "text-[var(--fg-2)]",
            )}
            aria-live={failed ? "assertive" : "polite"}
            role={failed ? "alert" : "status"}
          >
            {taskStatusText(task)}
            {task.source && (
              <span className="text-[var(--fg-3)]">
                {" "}
                · {SOURCE_LABEL[task.source] ?? task.source}
              </span>
            )}
            {taskTime(task) && (
              <span className="text-[var(--fg-3)]"> · {taskTime(task)}</span>
            )}
          </p>
          {failed && errorText && (
            <p
              className="mt-1 line-clamp-2 text-[11px] text-danger"
              role="alert"
            >
              {errorText}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-start gap-0.5">
          {active && (
            <button
              type="button"
              disabled={busy}
              onClick={onCancel}
              aria-label="取消任务"
              className="inline-flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-2)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:opacity-50"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}
          {recoverable && task.retryable && (
            <button
              type="button"
              disabled={busy}
              onClick={onRetry}
              aria-label="重试任务"
              className="inline-flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] text-[var(--accent)] hover:bg-[var(--accent-soft)] disabled:opacity-50"
            >
              <RotateCw className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>
      {recoverable && actions.length > 0 && (
        <TaskRecoveryActions
          actions={actions}
          retryable={Boolean(task.retryable)}
          busy={busy}
          onRetry={onRetry}
        />
      )}
    </div>
  );
}
