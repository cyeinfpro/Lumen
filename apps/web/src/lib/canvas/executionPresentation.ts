import type {
  CanvasExecutionStatus,
  CanvasExecutionTaskDetail,
  CanvasNodeExecution,
} from "#canvas-types";

const ACTIVE_EXECUTION_STATUSES = new Set<CanvasExecutionStatus>([
  "pending",
  "ready",
  "queued",
  "running",
  "reconciling",
  "canceling",
]);

const TERMINAL_TASK_STATUSES = new Set([
  "succeeded",
  "failed",
  "canceled",
  "expired",
]);

const EXECUTION_STATUS_LABELS: Record<CanvasExecutionStatus, string> = {
  pending: "待处理",
  ready: "已就绪",
  queued: "排队中",
  running: "运行中",
  reconciling: "同步结果中",
  canceling: "正在取消",
  succeeded: "已成功",
  partial_failed: "部分失败",
  failed: "已失败",
  blocked: "已阻塞",
  canceled: "已取消",
  skipped: "已跳过",
  reused: "已复用",
};

const TASK_STAGE_LABELS: Record<string, string> = {
  queued: "排队中",
  pending: "等待处理",
  ready: "准备运行",
  submitting: "提交任务",
  submitted: "等待生成",
  understanding: "理解需求",
  rendering: "生成画面",
  running: "生成中",
  fetching: "取回成品",
  processing: "处理成品",
  storing: "保存成品",
  finalizing: "整理结果",
  billing: "结算中",
  finished: "已完成",
  succeeded: "已完成",
  failed: "生成失败",
  canceled: "已取消",
  expired: "已过期",
};

export function isCanvasExecutionActive(
  execution: Pick<CanvasNodeExecution, "status">,
): boolean {
  return ACTIVE_EXECUTION_STATUSES.has(execution.status);
}

export function canvasExecutionStatusLabel(
  status: CanvasExecutionStatus,
): string {
  return EXECUTION_STATUS_LABELS[status] ?? status;
}

export function canvasExecutionPrimaryTask(
  execution: Pick<CanvasNodeExecution, "tasks">,
): CanvasExecutionTaskDetail | null {
  const tasks = execution.tasks ?? [];
  return (
    tasks.find((task) => !TERMINAL_TASK_STATUSES.has(task.status)) ??
    tasks.find((task) => task.kind === "video_generation") ??
    tasks[0] ??
    null
  );
}

export function canvasExecutionProgressPercent(
  execution: CanvasNodeExecution,
): number | null {
  const task = canvasExecutionPrimaryTask(execution);
  if (
    typeof task?.progress_pct === "number" &&
    Number.isFinite(task.progress_pct)
  ) {
    return Math.max(0, Math.min(100, Math.round(task.progress_pct)));
  }
  if (execution.status === "succeeded" || execution.status === "reused") {
    return 100;
  }
  return null;
}

export function canvasExecutionStageLabel(
  execution: CanvasNodeExecution,
): string {
  const task = canvasExecutionPrimaryTask(execution);
  const stage = task?.progress_stage || task?.status;
  if (stage) return TASK_STAGE_LABELS[stage] ?? stage;
  return canvasExecutionStatusLabel(execution.status);
}

export function canvasExecutionElapsedMs(
  execution: CanvasNodeExecution,
  now = Date.now(),
): number | null {
  const task = canvasExecutionPrimaryTask(execution);
  const reportedElapsed = nonNegativeNumber(task?.elapsed_ms);
  if (reportedElapsed !== null) return reportedElapsed;
  const startedAt = firstTimestamp([
    task?.started_at,
    task?.submit_started_at,
    execution.started_at,
    task?.created_at,
    execution.created_at,
  ]);
  if (startedAt === null) return null;
  const finishedAt = firstTimestamp([
    task?.finished_at,
    execution.finished_at,
  ]);
  return Math.max(0, (finishedAt ?? now) - startedAt);
}

export function formatCanvasTaskElapsed(ms: number | null): string | null {
  if (ms === null || !Number.isFinite(ms) || ms < 0) return null;
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${seconds} 秒`;
  return `${seconds} 秒`;
}

function timestamp(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function firstTimestamp(
  values: Array<string | null | undefined>,
): number | null {
  for (const value of values) {
    const parsed = timestamp(value);
    if (parsed !== null) return parsed;
  }
  return null;
}

function nonNegativeNumber(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}
