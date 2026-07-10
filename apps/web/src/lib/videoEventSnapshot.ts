import type {
  VideoGenerationOut,
  VideoStage,
  VideoStatus,
} from "@/lib/types";

const STATUS_RANK: Record<VideoStatus, number> = {
  queued: 0,
  submitting: 10,
  submit_unknown: 15,
  submitted: 20,
  running: 30,
  succeeded: 100,
  failed: 100,
  canceled: 100,
  expired: 100,
};

const STAGE_RANK: Record<VideoStage, number> = {
  queued: 0,
  submitting: 10,
  rendering: 20,
  fetching: 30,
  storing: 40,
  billing: 50,
  finished: 100,
};

const TERMINAL_STATUSES = new Set<VideoStatus>([
  "succeeded",
  "failed",
  "canceled",
  "expired",
]);

type LifecycleSnapshot = {
  epoch: number;
  status?: VideoStatus;
  stage?: VideoStage;
  progress?: number;
  errorCode?: string | null;
  errorMessage?: string | null;
  retryTransition: boolean;
};

function recordOf(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : null;
}

function statusOf(value: unknown): VideoStatus | undefined {
  return typeof value === "string" && value in STATUS_RANK
    ? (value as VideoStatus)
    : undefined;
}

function stageOf(value: unknown): VideoStage | undefined {
  return typeof value === "string" && value in STAGE_RANK
    ? (value as VideoStage)
    : undefined;
}

function epochOf(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.trunc(value)
    : undefined;
}

function progressOf(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, Math.min(100, value))
    : undefined;
}

function nullableString(value: unknown): string | null | undefined {
  return typeof value === "string" || value === null ? value : undefined;
}

function lifecycleOf(
  raw: Record<string, unknown>,
  currentEpoch: number,
): LifecycleSnapshot {
  return {
    epoch: epochOf(raw.submission_epoch) ?? currentEpoch,
    status: statusOf(raw.status),
    stage: stageOf(raw.stage ?? raw.progress_stage),
    progress: progressOf(raw.progress_pct),
    errorCode: nullableString(raw.error_code),
    errorMessage: nullableString(raw.error_message),
    retryTransition: raw.retry_transition === true,
  };
}

function rejectsLifecycle(
  current: VideoGenerationOut,
  incoming: LifecycleSnapshot,
): boolean {
  const currentEpoch = Math.max(0, current.submission_epoch ?? 0);
  if (incoming.epoch < currentEpoch) return true;
  if (incoming.epoch > currentEpoch) return false;
  if (
    incoming.retryTransition &&
    current.status === "submitting" &&
    incoming.status === "queued" &&
    incoming.stage === "queued"
  ) {
    return false;
  }
  if (
    incoming.status &&
    STATUS_RANK[incoming.status] < STATUS_RANK[current.status]
  ) {
    return true;
  }
  if (
    incoming.status &&
    TERMINAL_STATUSES.has(current.status) &&
    incoming.status !== current.status
  ) {
    return true;
  }
  return Boolean(
    incoming.stage &&
      STAGE_RANK[incoming.stage] < STAGE_RANK[current.progress_stage],
  );
}

function applyLifecycle(
  base: VideoGenerationOut,
  current: VideoGenerationOut,
  incoming: LifecycleSnapshot,
): VideoGenerationOut {
  const next = { ...base, submission_epoch: incoming.epoch };
  if (incoming.status) next.status = incoming.status;
  if (incoming.stage) next.progress_stage = incoming.stage;
  if (incoming.progress !== undefined) {
    const sameEpoch = incoming.epoch === (current.submission_epoch ?? 0);
    next.progress_pct = sameEpoch
      ? Math.max(current.progress_pct, incoming.progress)
      : incoming.progress;
  }
  if (incoming.errorCode !== undefined) next.error_code = incoming.errorCode;
  if (incoming.errorMessage !== undefined) {
    next.error_message = incoming.errorMessage;
  }
  return next;
}

export function videoGenerationEventId(data: unknown): string {
  const raw = recordOf(data);
  return typeof raw?.video_generation_id === "string"
    ? raw.video_generation_id
    : "";
}

export function isTerminalVideoEvent(data: unknown): boolean {
  const status = statusOf(recordOf(data)?.status);
  return status !== undefined && TERMINAL_STATUSES.has(status);
}

export function mergeVideoGenerationEvent(
  current: VideoGenerationOut,
  data: unknown,
): VideoGenerationOut {
  const raw = recordOf(data);
  if (!raw || videoGenerationEventId(raw) !== current.id) return current;
  const incoming = lifecycleOf(raw, Math.max(0, current.submission_epoch ?? 0));
  if (rejectsLifecycle(current, incoming)) return current;
  return applyLifecycle(current, current, incoming);
}

export function mergeVideoGenerationSnapshot(
  current: VideoGenerationOut,
  incoming: VideoGenerationOut,
): VideoGenerationOut {
  if (incoming.id !== current.id) return current;
  const raw = incoming as unknown as Record<string, unknown>;
  const lifecycle = lifecycleOf(
    raw,
    Math.max(0, current.submission_epoch ?? 0),
  );
  if (rejectsLifecycle(current, lifecycle)) return current;
  return applyLifecycle({ ...current, ...incoming }, current, lifecycle);
}

export function mergeVideoGenerationLists(
  current: VideoGenerationOut[],
  updates: VideoGenerationOut[],
): VideoGenerationOut[] {
  const map = new Map(current.map((item) => [item.id, item]));
  for (const item of updates) {
    const existing = map.get(item.id);
    map.set(
      item.id,
      existing ? mergeVideoGenerationSnapshot(existing, item) : item,
    );
  }
  return Array.from(map.values()).sort(
    (left, right) =>
      new Date(right.created_at).getTime() - new Date(left.created_at).getTime(),
  );
}
