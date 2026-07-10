import type { Generation } from "../lib/types";

const GENERATION_STATUSES = new Set<Generation["status"]>([
  "queued",
  "running",
  "succeeded",
  "failed",
  "canceled",
]);

const GENERATION_STAGES = new Set<Generation["stage"]>([
  "queued",
  "understanding",
  "rendering",
  "finalizing",
]);

const GENERATION_SUBSTAGES = new Set<NonNullable<Generation["substage"]>>([
  "waiting_queue",
  "waiting_provider",
  "preparing_refs",
  "upstream_started",
  "upstream_retrying",
  "postprocessing",
  "display_ready",
  "retryable",
  "terminal",
  "cancelled",
  "completed",
  "provider_selected",
  "stream_started",
  "partial_received",
  "final_received",
  "processing",
  "storing",
]);

type LifecycleReducer = (
  payload: Record<string, unknown>,
  generation: Generation,
  eventNow: number,
) => Partial<Generation>;

export function coerceGenerationStatus(
  value: unknown,
  fallback: Generation["status"],
): Generation["status"] {
  return typeof value === "string" &&
    GENERATION_STATUSES.has(value as Generation["status"])
    ? (value as Generation["status"])
    : fallback;
}

export function coerceGenerationStage(
  value: unknown,
  fallback: Generation["stage"],
): Generation["stage"] {
  return typeof value === "string" &&
    GENERATION_STAGES.has(value as Generation["stage"])
    ? (value as Generation["stage"])
    : fallback;
}

export function coerceGenerationSubstage(
  value: unknown,
): Generation["substage"] | undefined {
  return typeof value === "string" &&
    GENERATION_SUBSTAGES.has(value as NonNullable<Generation["substage"]>)
    ? (value as Generation["substage"])
    : undefined;
}

function finiteNumber(
  payload: Record<string, unknown>,
  key: string,
): number | undefined {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function stringValue(
  payload: Record<string, unknown>,
  key: string,
): string | undefined {
  const value = payload[key];
  return typeof value === "string" ? value : undefined;
}

function queuedPatch(
  payload: Record<string, unknown>,
  generation: Generation,
): Partial<Generation> {
  const substage =
    coerceGenerationSubstage(payload.substage) ??
    (payload.reason === "image_provider_unavailable"
      ? "waiting_provider"
      : "waiting_queue");
  return {
    status: "queued",
    stage: "queued",
    substage,
    queue_position:
      finiteNumber(payload, "queue_position") ??
      generation.queue_position ??
      null,
    retrying: payload.retrying === true,
    waiting_provider:
      payload.waiting_provider === true ||
      payload.reason === "image_provider_unavailable" ||
      substage === "waiting_provider",
    cancelled: false,
    started_at: 0,
  };
}

function startedPatch(
  payload: Record<string, unknown>,
  generation: Generation,
  eventNow: number,
): Partial<Generation> {
  const patch: Partial<Generation> = {
    status: "running",
    stage: "understanding",
    substage: coerceGenerationSubstage(payload.substage) ?? "upstream_started",
    started_at: generation.started_at > 0 ? generation.started_at : eventNow,
    retry_eta: undefined,
    retry_error: undefined,
    retrying: false,
    waiting_provider: false,
    cancelled: false,
  };
  const attempt = finiteNumber(payload, "attempt");
  if (attempt !== undefined) patch.attempt = attempt;
  return patch;
}

function progressPatch(
  payload: Record<string, unknown>,
  generation: Generation,
  eventNow: number,
): Partial<Generation> {
  const patch: Partial<Generation> = {
    status: "running",
    queue_position:
      finiteNumber(payload, "queue_position") ??
      generation.queue_position ??
      null,
  };
  if (
    typeof payload.stage === "string" &&
    GENERATION_STAGES.has(payload.stage as Generation["stage"])
  ) {
    patch.stage = payload.stage as Generation["stage"];
  }
  const substage = coerceGenerationSubstage(payload.substage);
  if (substage) patch.substage = substage;
  if (payload.retrying === true) patch.retrying = true;
  if (payload.waiting_provider === true) patch.waiting_provider = true;
  if (payload.cancelled === true) patch.cancelled = true;
  if (payload.provider_failover === true) {
    patch.failover_count = (generation.failover_count ?? 0) + 1;
  }
  if (!(generation.started_at > 0)) patch.started_at = eventNow;
  return patch;
}

function partialImagePatch(
  _payload: Record<string, unknown>,
  generation: Generation,
  eventNow: number,
): Partial<Generation> {
  return {
    status: "running",
    stage: "rendering",
    substage: "partial_received",
    retrying: false,
    waiting_provider: false,
    ...(generation.started_at > 0 ? {} : { started_at: eventNow }),
  };
}

function retryingPatch(
  payload: Record<string, unknown>,
): Partial<Generation> {
  const patch: Partial<Generation> = {
    status: "queued",
    stage: "queued",
    substage: "upstream_retrying",
    retrying: true,
    waiting_provider: payload.reason === "image_provider_unavailable",
    cancelled: false,
    started_at: 0,
  };
  const attempt = finiteNumber(payload, "attempt");
  if (attempt !== undefined) patch.attempt = attempt;
  const maxAttempts = finiteNumber(payload, "max_attempts");
  if (maxAttempts !== undefined) patch.max_attempts = maxAttempts;
  const retryDelaySeconds = finiteNumber(payload, "retry_delay_seconds");
  if (retryDelaySeconds !== undefined) {
    patch.retry_eta = retryDelaySeconds;
  }
  patch.retry_error =
    stringValue(payload, "error_message") ?? stringValue(payload, "message");
  patch.error_code = stringValue(payload, "error_code");
  patch.error_message = patch.retry_error;
  return patch;
}

const LIFECYCLE_REDUCERS: Record<string, LifecycleReducer> = {
  "generation.queued": queuedPatch,
  "generation.started": startedPatch,
  "generation.progress": progressPatch,
  "generation.partial_image": partialImagePatch,
  "generation.retrying": (payload, generation, eventNow) => {
    const patch = retryingPatch(payload);
    if (patch.retry_eta !== undefined) {
      patch.retry_eta = eventNow + patch.retry_eta * 1000;
    }
    return patch;
  },
};

/**
 * Pure state transition for non-terminal generation lifecycle events.
 * Terminal result materialization stays in useChatStore because it also updates
 * messages and the image index.
 */
export function reduceGenerationLifecycleEvent(
  eventName: string,
  payload: Record<string, unknown>,
  generation: Generation,
  eventNow: number,
): Partial<Generation> | null {
  const reducer = LIFECYCLE_REDUCERS[eventName];
  return reducer ? reducer(payload, generation, eventNow) : null;
}
