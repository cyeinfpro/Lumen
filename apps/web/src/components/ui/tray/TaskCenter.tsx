"use client";

import { useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  Clock3,
  ImageIcon,
  MessageSquareText,
  RefreshCw,
  RotateCw,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";

import {
  userTaskQueryKeys,
  useUserQueryScope,
} from "@/components/QueryProvider";
import { IconButton } from "@/components/ui/primitives";
import { listTasks, type TaskItemResponse } from "@/lib/apiClient";
import type { Generation, RecommendedErrorAction } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  deriveTaskCenterViewState,
  deriveTaskHistoryPresentation,
  TASK_FILTERS,
  taskFilterStatus,
  type TaskFilter,
  type TaskHistoryPresentation,
} from "./taskCenterModel";
import { TaskItem } from "./TaskItem";
import { useTaskCenterActions } from "./useTaskCenterActions";

interface TaskCenterProps {
  activeGenerations: Generation[];
  localGenerations: Record<string, Generation>;
  onCancelGeneration: (gen: Generation) => void;
  onRetryGeneration: (gen: Generation) => void;
  onViewGeneration: (gen: Generation) => void;
  onClose: () => void;
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
  const userScope = useUserQueryScope();
  const status = taskFilterStatus(filter);
  const query = useQuery({
    queryKey: userTaskQueryKeys.recent(
      userScope.userId,
      status ?? "all",
    ),
    queryFn: ({ signal }) => listTasks({ status, limit: 80 }, { signal }),
    enabled: userScope.enabled,
    staleTime: 8_000,
  });
  const actions = useTaskCenterActions(userScope.userId);
  const viewState = useMemo(
    () =>
      deriveTaskCenterViewState({
        activeGenerations,
        serverItems: query.data?.items ?? [],
        filter,
        queryLoading: query.isLoading,
      }),
    [activeGenerations, filter, query.data?.items, query.isLoading],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <TaskCenterHeader
        activeCount={activeGenerations.length}
        fetching={query.isFetching}
        refreshEnabled={userScope.enabled}
        onRefresh={() => void query.refetch()}
        onClose={onClose}
      />
      <TaskCenterFilters filter={filter} onChange={setFilter} />
      <div className="mobile-dialog-scroll min-h-0 flex-1 space-y-1.5 overflow-y-auto p-2 sm:max-h-[56vh]">
        {viewState.visibleActive.map((generation) => (
          <TaskItem
            key={generation.id}
            gen={generation}
            onCancel={onCancelGeneration}
            onRetry={onRetryGeneration}
            onView={onViewGeneration}
          />
        ))}
        {viewState.visibleHistory.map((task) => (
          <TaskCenterHistoryItem
            key={task.id}
            task={task}
            localGeneration={
              task.kind === "generation"
                ? localGenerations[task.id]
                : undefined
            }
            busy={actions.busy}
            onRetry={() => actions.retry(task)}
            onCancel={() => actions.cancel(task)}
            onCancelGeneration={onCancelGeneration}
            onRetryGeneration={onRetryGeneration}
            onViewGeneration={onViewGeneration}
          />
        ))}
        {query.isLoading && (
          <TaskCenterMessage>正在读取最近任务</TaskCenterMessage>
        )}
        {viewState.isEmpty && <TaskCenterMessage>暂无任务</TaskCenterMessage>}
      </div>
    </div>
  );
}

function TaskCenterHeader({
  activeCount,
  fetching,
  refreshEnabled,
  onRefresh,
  onClose,
}: {
  activeCount: number;
  fetching: boolean;
  refreshEnabled: boolean;
  onRefresh: () => void;
  onClose: () => void;
}) {
  return (
    <div className="flex shrink-0 items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2.5">
      <span
        className={cn(
          "h-2 w-2 shrink-0 rounded-full",
          activeCount > 0
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
        onClick={onRefresh}
        disabled={!refreshEnabled}
        aria-label="刷新任务"
      >
        <RefreshCw className={cn("h-3.5 w-3.5", fetching && "animate-spin")} />
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
  );
}

function TaskCenterFilters({
  filter,
  onChange,
}: {
  filter: TaskFilter;
  onChange: (filter: TaskFilter) => void;
}) {
  return (
    <div className="flex shrink-0 gap-1 border-b border-[var(--border-subtle)] px-2 py-2">
      {TASK_FILTERS.map((item) => (
        <button
          key={item.key}
          type="button"
          onClick={() => onChange(item.key)}
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
  );
}

function TaskCenterHistoryItem({
  task,
  localGeneration,
  busy,
  onRetry,
  onCancel,
  onCancelGeneration,
  onRetryGeneration,
  onViewGeneration,
}: {
  task: TaskItemResponse;
  localGeneration: Generation | undefined;
  busy: boolean;
  onRetry: () => void;
  onCancel: () => void;
  onCancelGeneration: (generation: Generation) => void;
  onRetryGeneration: (generation: Generation) => void;
  onViewGeneration: (generation: Generation) => void;
}) {
  if (localGeneration) {
    return (
      <TaskItem
        gen={localGeneration}
        onCancel={onCancelGeneration}
        onRetry={onRetryGeneration}
        onView={onViewGeneration}
      />
    );
  }
  return (
    <TaskHistoryRow
      task={task}
      busy={busy}
      onRetry={onRetry}
      onCancel={onCancel}
    />
  );
}

function TaskCenterMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] p-3 text-xs text-[var(--fg-2)]">
      {children}
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
  const presentation = deriveTaskHistoryPresentation(task);

  return (
    <div
      className={cn(
        "rounded-[var(--radius-card)] border p-2 transition",
        presentation.failed
          ? "border-danger-border bg-danger-soft"
          : "border-[var(--border)] bg-[var(--bg-1)]",
      )}
    >
      <div className="flex gap-2">
        <TaskHistoryThumb task={task} />
        <TaskHistorySummary presentation={presentation} />
        <TaskHistoryControls
          task={task}
          presentation={presentation}
          busy={busy}
          onRetry={onRetry}
          onCancel={onCancel}
        />
      </div>
      <TaskRecoveryActions
        presentation={presentation}
        retryable={Boolean(task.retryable)}
        busy={busy}
        onRetry={onRetry}
      />
    </div>
  );
}

function TaskHistoryThumb({ task }: { task: TaskItemResponse }) {
  return (
    <div className="flex h-10 w-10 shrink-0 items-center justify-center overflow-hidden rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)]">
      {task.thumb_url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={task.thumb_url} alt="" className="h-full w-full object-cover" />
      ) : task.kind === "generation" ? (
        <ImageIcon className="h-4 w-4 text-[var(--fg-2)]" />
      ) : (
        <MessageSquareText className="h-4 w-4 text-[var(--fg-2)]" />
      )}
    </div>
  );
}

function TaskHistorySummary({
  presentation,
}: {
  presentation: TaskHistoryPresentation;
}) {
  return (
    <div className="min-w-0 flex-1">
      <div className="flex min-w-0 items-center gap-1.5">
        <p className="truncate text-[13px] font-medium text-[var(--fg-0)]">
          {presentation.title}
        </p>
        {presentation.succeeded && (
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-[var(--ok)]" />
        )}
        {presentation.active && (
          <Clock3 className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
        )}
      </div>
      <p
        className={cn(
          "mt-0.5 text-[11px]",
          presentation.failed ? "text-danger" : "text-[var(--fg-2)]",
        )}
        aria-live={presentation.failed ? "assertive" : "polite"}
        role={presentation.failed ? "alert" : "status"}
      >
        {presentation.statusText}
        {presentation.sourceText && (
          <span className="text-[var(--fg-3)]">
            {" "}
            · {presentation.sourceText}
          </span>
        )}
        {presentation.timeText && (
          <span className="text-[var(--fg-3)]">
            {" "}
            · {presentation.timeText}
          </span>
        )}
      </p>
      {presentation.failed && presentation.errorText && (
        <p className="mt-1 line-clamp-2 text-[11px] text-danger" role="alert">
          {presentation.errorText}
        </p>
      )}
    </div>
  );
}

function TaskHistoryControls({
  task,
  presentation,
  busy,
  onRetry,
  onCancel,
}: {
  task: TaskItemResponse;
  presentation: TaskHistoryPresentation;
  busy: boolean;
  onRetry: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="flex shrink-0 items-start gap-0.5">
      {presentation.active && (
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
      {presentation.recoverable && task.retryable && (
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
  );
}

function TaskRecoveryActions({
  presentation,
  retryable,
  busy,
  onRetry,
}: {
  presentation: TaskHistoryPresentation;
  retryable: boolean;
  busy: boolean;
  onRetry: () => void;
}) {
  if (!presentation.recoverable || presentation.actions.length === 0) {
    return null;
  }

  return (
    <div className="mt-2 flex flex-wrap gap-1">
      {presentation.actions.map((action) => (
        <TaskRecoveryAction
          key={action.id}
          action={action}
          retryable={retryable}
          busy={busy}
          onRetry={onRetry}
        />
      ))}
    </div>
  );
}

function TaskRecoveryAction({
  action,
  retryable,
  busy,
  onRetry,
}: {
  action: RecommendedErrorAction;
  retryable: boolean;
  busy: boolean;
  onRetry: () => void;
}) {
  if (action.kind === "retry" && retryable) {
    return (
      <button
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
        href={action.href}
        className="inline-flex min-h-11 items-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 text-[11px] text-[var(--fg-1)] hover:text-[var(--fg-0)]"
      >
        {action.label}
      </a>
    );
  }
  return (
    <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-[11px] text-[var(--fg-2)]">
      {action.label}
    </span>
  );
}
