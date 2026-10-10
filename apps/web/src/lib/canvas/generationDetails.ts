export type CanvasTaskRecoveryState =
  | "unavailable" | "submission_unknown" | "cancel_requested" | "saving_artifact"
  | "queued" | "running" | "reconciling" | "succeeded" | "failed" | "canceled" | "expired";
export interface CanvasTaskRecovery {
  state: CanvasTaskRecoveryState;
  can_query: boolean;
  can_cancel: boolean;
  can_generate_new: boolean;
  automatic_resubmit: false;
}
export interface CanvasBillingDetails {
  currency: "CNY";
  estimated_cost_micro: number | null;
  reserved_micro: number | null;
  actual_cost_micro: number | null;
  source: "wallet_ledger" | "task_snapshot" | "unknown" | "task_ledger_aggregate";
  task_count?: number;
  known_estimated_cost_micro?: number | null;
  known_reserved_micro?: number | null;
  known_actual_cost_micro?: number | null;
}
const RECOVERY_STATES = new Set<CanvasTaskRecoveryState>([
  "unavailable", "submission_unknown", "cancel_requested", "saving_artifact",
  "queued", "running", "reconciling", "succeeded", "failed", "canceled", "expired",
]);
const TERMINAL_STATES = new Set<CanvasTaskRecoveryState>(["succeeded", "failed", "canceled", "expired"]);
const BILLING_SOURCES = new Set(["wallet_ledger", "task_snapshot", "unknown", "task_ledger_aggregate"]);
const BILLING_FIELDS = ["estimated_cost_micro", "reserved_micro", "actual_cost_micro"] as const;
const UNKNOWN_SUBMISSION_MARKERS = new Set([
  "submit_unknown", "submission_unknown", "direct_image_result_unknown",
  "image_job_result_unknown", "no_image_returned", "result_unknown",
]);
function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
export function canvasMicroAmount(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}
export function canvasTaskSubmissionIsUnknown(task: {
  status?: unknown; progress_stage?: unknown; error_code?: unknown; recovery?: unknown;
}): boolean {
  return record(task.recovery)?.state === "submission_unknown" ||
    [task.status, task.progress_stage, task.error_code]
      .some((field) => typeof field === "string" && UNKNOWN_SUBMISSION_MARKERS.has(field));
}
function isRecoveryContract(raw: Record<string, unknown> | null): raw is Record<string, unknown> & CanvasTaskRecovery {
  return raw !== null && typeof raw.state === "string" &&
    RECOVERY_STATES.has(raw.state as CanvasTaskRecoveryState) && raw.automatic_resubmit === false &&
    typeof raw.can_query === "boolean" && typeof raw.can_cancel === "boolean" &&
    typeof raw.can_generate_new === "boolean";
}
function unknownRecovery(
  raw: Record<string, unknown> | null, task: Record<string, unknown>,
): CanvasTaskRecovery {
  return { state: "submission_unknown", can_query: true, automatic_resubmit: false,
    can_generate_new: false, can_cancel: raw?.automatic_resubmit === false &&
      raw.can_cancel === true && !TERMINAL_STATES.has(task.status as CanvasTaskRecoveryState) };
}
export function normalizeCanvasTaskRecovery(
  value: unknown, task: Record<string, unknown> = {},
): CanvasTaskRecovery | undefined {
  const raw = record(value);
  // Timeout projection can say expired while the provider submission is still
  // uncertain. Never let that legacy terminal label create a second paid intent.
  if (canvasTaskSubmissionIsUnknown(task)) return unknownRecovery(raw, task);
  if (!isRecoveryContract(raw)) return undefined;
  const state = raw.state;
  return { state, automatic_resubmit: false, can_query: raw.can_query,
    can_cancel: raw.can_cancel && !TERMINAL_STATES.has(state) && state !== "cancel_requested" && state !== "unavailable",
    can_generate_new: raw.can_generate_new && TERMINAL_STATES.has(state) };
}
export function normalizeCanvasBilling(value: unknown): CanvasBillingDetails | undefined {
  const raw = record(value);
  if (!raw || raw.currency !== "CNY" || typeof raw.source !== "string" || !BILLING_SOURCES.has(raw.source)) return undefined;
  const result: CanvasBillingDetails = { currency: "CNY",
    estimated_cost_micro: canvasMicroAmount(raw.estimated_cost_micro),
    reserved_micro: canvasMicroAmount(raw.reserved_micro),
    actual_cost_micro: canvasMicroAmount(raw.actual_cost_micro),
    source: raw.source as CanvasBillingDetails["source"] };
  if (raw.source === "task_ledger_aggregate") {
    const taskCount = canvasMicroAmount(raw.task_count);
    if (taskCount !== null) result.task_count = taskCount;
    for (const field of BILLING_FIELDS) result[`known_${field}`] = canvasMicroAmount(raw[`known_${field}`]);
  }
  return result;
}
export function formatCanvasMicroAmount(value: number | null): string {
  const amount = canvasMicroAmount(value);
  if (amount === null) return "待确认";
  // Integer arithmetic avoids showing rounded/negative monetary amounts.
  const whole = Math.floor(amount / 1_000_000);
  const fraction = (amount % 1_000_000).toString().padStart(6, "0").replace(/0+$/, "");
  return `¥${whole.toLocaleString("zh-CN")}${fraction ? "." + fraction : ""}`;
}
export function canvasBillingRows(billing: CanvasBillingDetails): Array<[string, string]> {
  const labels = ["预估费用", "当前预留", "实际结算"];
  return BILLING_FIELDS.map((field, index) => {
    const amount = billing[field], known = billing[`known_${field}`];
    const value = amount !== null ? formatCanvasMicroAmount(amount)
      : typeof known === "number" && known > 0
        ? `已知小计 ${formatCanvasMicroAmount(known)}（尚未完整）` : "待确认";
    return [labels[index], value];
  });
}
export function canvasRecoveryLabel(recovery: CanvasTaskRecovery): string {
  const labels: Record<CanvasTaskRecoveryState, string> = {
    unavailable: "任务详情暂不可用", submission_unknown: "提交状态待确认",
    cancel_requested: "已请求取消，等待确认", saving_artifact: "保存成品中",
    queued: "排队中", running: "生成中", reconciling: "同步结果中",
    succeeded: "已完成", failed: "已失败", canceled: "已取消", expired: "已过期",
  };
  return labels[recovery.state];
}
export function canvasRecoveryExplanation(recovery: CanvasTaskRecovery): string | null {
  if (recovery.state === "submission_unknown") return "先查询原任务状态，避免重复提交和扣费。";
  if (recovery.state === "saving_artifact") return "正在取回或保存已有成品，查询状态不会重新生成。";
  if (recovery.state === "cancel_requested") return "取消申请已发出，最终状态和费用以任务记录为准。";
  return null;
}
export function canvasTaskCancelTarget(task: {
  kind: string; generation_id?: string | null; video_generation_id?: string | null;
  recovery?: CanvasTaskRecovery;
}): { kind: "generation" | "video_generation"; id: string } | null {
  if (!task.recovery?.can_cancel) return null;
  const id = task.kind === "generation" ? task.generation_id
    : task.kind === "video_generation" ? task.video_generation_id : null;
  return typeof id === "string" && id.trim().length > 0 && id.length <= 128
    ? { kind: task.kind as "generation" | "video_generation", id } : null;
}
