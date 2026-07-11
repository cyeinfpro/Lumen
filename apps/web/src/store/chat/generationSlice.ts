import { recommendedActionsForError } from "../../lib/errors";
import { logWarn } from "../../lib/logger";
import type { BackendGeneration } from "../../lib/apiClient";
import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
  ImageGenerationDiagnostics,
  ImageProviderAttempt,
  Message,
} from "../../lib/types";
import {
  coerceGenerationStage,
  coerceGenerationStatus,
  coerceGenerationSubstage,
} from "../chatGenerationEvents";
import { DEFAULT_PARAMS } from "./imageParams";
import { shouldAcceptTaskSnapshot } from "./messageReconciliation";
import {
  coerceAspectRatio,
  firstOptionalRecord,
  isoToMs,
  optionalRecord,
  optionalRecordArray,
  optionalString,
  recommendedActionsFromUnknown,
  stringArray,
  structuredAttachmentsFromUnknown,
} from "./payload";

export type GenerationExplainabilityMeta = Pick<
  Generation,
  | "diagnostics"
  | "revised_prompt"
  | "requested_params"
  | "effective_params"
  | "provider_attempts"
  | "source"
  | "action_source"
  | "trace_id"
  | "attachment_roles"
  | "queue_lane"
  | "workflow_type"
  | "workflow_step_key"
  | "pixel_count"
  | "size_bucket"
  | "cost_class"
  | "queue_wait_ms"
>;

export type GenerationTaskMeta = Pick<
  Generation,
  | "substage"
  | "queue_position"
  | "retrying"
  | "waiting_provider"
  | "cancelled"
  | "retryable"
  | "recommended_actions"
  | "source"
  | "conversation_id"
  | "project_id"
  | "thumb_url"
>;

function explainabilityRecord(
  value: unknown,
): Record<string, unknown> | undefined {
  return optionalRecord(value, logWarn);
}

function firstExplainabilityRecord(
  first: unknown,
  second: unknown,
): Record<string, unknown> | undefined {
  return firstOptionalRecord(first, second, logWarn);
}

function nullishFallback<T>(
  value: T | null | undefined,
  fallback: T | undefined,
): T | undefined {
  return value == null ? fallback : value;
}

function undefinedIfNullish<T>(
  value: T | null | undefined,
): T | undefined {
  return value == null ? undefined : value;
}

function firstOptionalRecordArray(
  first: unknown,
  second: unknown,
): Array<Record<string, unknown>> | undefined {
  return optionalRecordArray(first) ?? optionalRecordArray(second);
}

function numberOrUndefined(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined;
}

function generationAspectRatio(
  value: unknown,
  fallback?: Generation["aspect_ratio"],
): Generation["aspect_ratio"] {
  return coerceAspectRatio(value, fallback ?? DEFAULT_PARAMS.aspect_ratio);
}

export function generationIdsOfMessage(
  message: AssistantMessage,
): string[] {
  if (message.generation_ids && message.generation_ids.length > 0) {
    return message.generation_ids;
  }
  return message.generation_id ? [message.generation_id] : [];
}

export function assistantHasGeneration(
  message: AssistantMessage,
  generationId: string,
): boolean {
  return generationIdsOfMessage(message).includes(generationId);
}

export function aggregateGenerationStatus(
  generationIds: string[],
  generations: Record<string, Generation>,
  fallback: AssistantMessage["status"],
): AssistantMessage["status"] {
  const items = generationIds.map((id) => generations[id]).filter(Boolean);
  if (items.length === 0) return fallback;
  if (items.some((item) => isInflightGeneration(item))) return "pending";
  if (items.every((item) => item.status === "canceled")) return "canceled";
  if (items.every((item) => item.status === "failed")) return "failed";
  if (items.some((item) => item.status === "succeeded")) return "succeeded";
  return fallback;
}

export function generationExplainabilityFromBackend(
  generation: BackendGeneration,
): GenerationExplainabilityMeta {
  const diagnostics = explainabilityRecord(generation.diagnostics) as
    | ImageGenerationDiagnostics
    | undefined;
  const providerAttempts = firstOptionalRecordArray(
    generation.provider_attempts,
    diagnostics?.provider_attempts,
  );
  return {
    diagnostics: undefinedIfNullish(diagnostics),
    revised_prompt: optionalString(
      nullishFallback(
        generation.revised_prompt,
        diagnostics?.revised_prompt ?? undefined,
      ),
    ),
    requested_params: firstExplainabilityRecord(
      generation.requested_params,
      diagnostics?.requested_params,
    ),
    effective_params: firstExplainabilityRecord(
      generation.effective_params,
      diagnostics?.effective_params,
    ),
    provider_attempts: providerAttempts as ImageProviderAttempt[] | undefined,
    source: undefinedIfNullish(generation.source),
    action_source: undefinedIfNullish(generation.action_source),
    trace_id: nullishFallback(
      generation.trace_id,
      optionalString(diagnostics?.trace_id),
    ),
    attachment_roles:
      undefinedIfNullish(
        structuredAttachmentsFromUnknown(generation.attachment_roles),
      ),
    queue_lane: undefinedIfNullish(generation.queue_lane),
    workflow_type: undefinedIfNullish(generation.workflow_type),
    workflow_step_key: undefinedIfNullish(generation.workflow_step_key),
    pixel_count: numberOrUndefined(generation.pixel_count),
    size_bucket: undefinedIfNullish(generation.size_bucket),
    cost_class: undefinedIfNullish(generation.cost_class),
    queue_wait_ms: numberOrUndefined(generation.queue_wait_ms),
  };
}

export function generationTaskMetaFromBackend(
  generation: BackendGeneration,
): GenerationTaskMeta {
  return {
    substage: coerceGenerationSubstage(generation.substage),
    queue_position:
      typeof generation.queue_position === "number" &&
      Number.isFinite(generation.queue_position)
        ? generation.queue_position
        : null,
    retrying: generation.retrying === true || undefined,
    waiting_provider: generation.waiting_provider === true || undefined,
    cancelled:
      generation.cancelled === true ||
      generation.status === "canceled" ||
      undefined,
    retryable: generation.retryable === true || undefined,
    recommended_actions:
      recommendedActionsFromUnknown(generation.recommended_actions) ??
      recommendedActionsForError(generation.error_code, {
        retryable: generation.retryable === true,
        status: generation.status,
      }),
    source: generation.source ?? undefined,
    conversation_id: generation.conversation_id ?? null,
    project_id: generation.project_id ?? null,
    thumb_url: generation.thumb_url ?? null,
  };
}

export function activeGenerationFromBackend(
  generation: BackendGeneration,
): Generation {
  return {
    id: generation.id,
    message_id: generation.message_id,
    parent_generation_id: generation.parent_generation_id ?? null,
    action: generation.action === "edit" ? "edit" : "generate",
    prompt: typeof generation.prompt === "string" ? generation.prompt : "",
    size_requested:
      typeof generation.size_requested === "string"
        ? generation.size_requested
        : "auto",
    aspect_ratio: generationAspectRatio(generation.aspect_ratio),
    input_image_ids: stringArray(generation.input_image_ids),
    primary_input_image_id:
      typeof generation.primary_input_image_id === "string"
        ? generation.primary_input_image_id
        : null,
    status: coerceGenerationStatus(generation.status, "queued"),
    stage: coerceGenerationStage(generation.progress_stage, "queued"),
    image: undefined,
    error_code: generation.error_code ?? undefined,
    error_message: generation.error_message ?? undefined,
    ...generationExplainabilityFromBackend(generation),
    ...generationTaskMetaFromBackend(generation),
    attempt:
      typeof generation.attempt === "number" &&
      Number.isFinite(generation.attempt)
        ? generation.attempt
        : 0,
    started_at: isoToMs(generation.started_at),
    finished_at: generation.finished_at
      ? isoToMs(generation.finished_at)
      : undefined,
  };
}

function shouldKeepInflightGeneration(
  existing: Generation | undefined,
  incomingStatus: Generation["status"],
): boolean {
  return Boolean(
    existing &&
      isInflightGeneration(existing) &&
      !["succeeded", "failed", "canceled"].includes(incomingStatus),
  );
}

export function preferredGenerationSnapshot(
  existing: Generation | undefined,
  incoming: Generation,
): Generation {
  if (!existing) return incoming;
  if (!shouldAcceptTaskSnapshot(existing.status, incoming.status)) {
    return existing;
  }
  return shouldKeepInflightGeneration(existing, incoming.status)
    ? existing
    : incoming;
}

export function generationExplainabilityFromPayload(
  payload: Record<string, unknown>,
): GenerationExplainabilityMeta {
  const diagnostics = explainabilityRecord(payload.diagnostics) as
    | ImageGenerationDiagnostics
    | undefined;
  const providerAttempts = firstOptionalRecordArray(
    payload.provider_attempts,
    diagnostics?.provider_attempts,
  );
  return {
    diagnostics: diagnostics ?? undefined,
    revised_prompt:
      optionalString(payload.revised_prompt) ?? diagnostics?.revised_prompt,
    requested_params: firstExplainabilityRecord(
      payload.requested_params,
      diagnostics?.requested_params,
    ),
    effective_params: firstExplainabilityRecord(
      payload.effective_params,
      diagnostics?.effective_params,
    ),
    provider_attempts: providerAttempts as ImageProviderAttempt[] | undefined,
    source: optionalString(payload.source),
    action_source: optionalString(payload.action_source),
    trace_id:
      optionalString(payload.trace_id) ?? optionalString(diagnostics?.trace_id),
    attachment_roles: structuredAttachmentsFromUnknown(
      payload.attachment_roles,
    ),
    queue_lane: optionalString(payload.queue_lane),
    workflow_type: optionalString(payload.workflow_type),
    workflow_step_key: optionalString(payload.workflow_step_key),
    pixel_count:
      typeof payload.pixel_count === "number" ? payload.pixel_count : undefined,
    size_bucket: optionalString(payload.size_bucket),
    cost_class: optionalString(payload.cost_class),
    queue_wait_ms:
      typeof payload.queue_wait_ms === "number"
        ? payload.queue_wait_ms
        : undefined,
  };
}

function hasExplainabilityMetadata(
  meta: GenerationExplainabilityMeta,
): boolean {
  return [
    meta.diagnostics,
    meta.revised_prompt,
    meta.requested_params,
    meta.effective_params,
    meta.provider_attempts,
    meta.trace_id,
    meta.action_source,
    meta.attachment_roles,
  ].some(Boolean);
}

function setMetadataIfMissing(
  metadata: Record<string, unknown>,
  key: string,
  value: unknown,
): void {
  if (value && metadata[key] == null) metadata[key] = value;
}

export function mergeExplainabilityIntoImage(
  image: GeneratedImage | undefined,
  meta: GenerationExplainabilityMeta,
): GeneratedImage | undefined {
  if (!image || !hasExplainabilityMetadata(meta)) return image;
  const metadata = { ...(image.metadata_jsonb ?? {}) };
  setMetadataIfMissing(
    metadata,
    "generation_diagnostics",
    meta.diagnostics,
  );
  setMetadataIfMissing(metadata, "revised_prompt", meta.revised_prompt);
  setMetadataIfMissing(metadata, "requested_params", meta.requested_params);
  setMetadataIfMissing(metadata, "effective_params", meta.effective_params);
  setMetadataIfMissing(metadata, "provider_attempts", meta.provider_attempts);
  setMetadataIfMissing(metadata, "trace_id", meta.trace_id);
  setMetadataIfMissing(metadata, "action_source", meta.action_source);
  setMetadataIfMissing(metadata, "attachment_roles", meta.attachment_roles);
  return {
    ...image,
    ...meta,
    metadata_jsonb: metadata,
  };
}

export function completionToolGenerationId(completionId: string): string {
  return `completion-tool-${completionId}`;
}

export function terminalGenerationEventStatus(
  eventName: string,
): "succeeded" | "failed" | null {
  if (eventName === "generation.succeeded") return "succeeded";
  if (eventName === "generation.failed") return "failed";
  return null;
}

export function updateGenerationAssistantStatuses(
  messages: Message[],
  generationId: string,
  generations: Record<string, Generation>,
): Message[] {
  return messages.map((message) => {
    if (
      message.role !== "assistant" ||
      !assistantHasGeneration(message, generationId)
    ) {
      return message;
    }
    return {
      ...message,
      status: aggregateGenerationStatus(
        generationIdsOfMessage(message),
        generations,
        message.status,
      ),
    };
  });
}

export function isInflightGeneration(generation: Generation): boolean {
  return generation.status === "queued" || generation.status === "running";
}

export function mergeUnknownActiveGenerations(
  existing: Record<string, Generation>,
  incoming: BackendGeneration[],
): Record<string, Generation> | null {
  const next = { ...existing };
  let changed = false;
  for (const generation of incoming) {
    if (existing[generation.id]) continue;
    next[generation.id] = activeGenerationFromBackend(generation);
    changed = true;
  }
  return changed ? next : null;
}
