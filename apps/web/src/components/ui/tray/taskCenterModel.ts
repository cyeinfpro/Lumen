import type { TaskItemResponse } from "@/lib/apiClient";
import { errorCodeToFullText, recommendedActionsForError } from "@/lib/errors";
import type { Generation, RecommendedErrorAction } from "@/lib/types";

export type TaskFilter = "all" | "active" | "failed";

export const TASK_FILTERS: Array<{ key: TaskFilter; label: string }> = [
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

export interface TaskCenterViewState {
  visibleActive: Generation[];
  visibleHistory: TaskItemResponse[];
  isEmpty: boolean;
}

export interface TaskHistoryPresentation {
  active: boolean;
  failed: boolean;
  recoverable: boolean;
  succeeded: boolean;
  title: string;
  statusText: string;
  sourceText: string;
  timeText: string;
  errorText: string | null | undefined;
  actions: RecommendedErrorAction[];
}

export function taskFilterStatus(
  filter: TaskFilter,
): "active" | "failed" | undefined {
  if (filter === "active") return "active";
  if (filter === "failed") return "failed";
  return undefined;
}

export function taskKindPath(
  task: TaskItemResponse,
): "generations" | "completions" {
  return task.kind === "generation" ? "generations" : "completions";
}

export function deriveTaskCenterViewState({
  activeGenerations,
  serverItems,
  filter,
  queryLoading,
}: {
  activeGenerations: Generation[];
  serverItems: TaskItemResponse[];
  filter: TaskFilter;
  queryLoading: boolean;
}): TaskCenterViewState {
  const activeIds = new Set(activeGenerations.map((generation) => generation.id));
  const historyItems = serverItems.filter((item) => !activeIds.has(item.id));
  const visibleActive =
    filter === "failed"
      ? []
      : activeGenerations.slice(0, filter === "active" ? 12 : 5);
  const visibleHistory = historyItems.slice(
    0,
    filter === "active" ? 20 : 40,
  );

  return {
    visibleActive,
    visibleHistory,
    isEmpty:
      visibleActive.length === 0 &&
      visibleHistory.length === 0 &&
      !queryLoading,
  };
}

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

export function deriveTaskHistoryPresentation(
  task: TaskItemResponse,
): TaskHistoryPresentation {
  const active =
    task.status === "queued" ||
    task.status === "running" ||
    task.status === "streaming";
  const failed = task.status === "failed";

  return {
    active,
    failed,
    recoverable: failed || task.status === "canceled",
    succeeded: task.status === "succeeded",
    title:
      task.title ||
      task.prompt ||
      (task.kind === "generation" ? "图像生成" : "文本回复"),
    statusText: taskStatusText(task),
    sourceText: task.source
      ? (SOURCE_LABEL[task.source] ?? task.source)
      : "",
    timeText: taskTime(task),
    errorText:
      failed && task.error_code
        ? (errorCodeToFullText(task.error_code) ?? task.error_message)
        : task.error_message,
    actions: actionList(task),
  };
}
