import type { BackendMessage } from "../../lib/apiClient";
import type {
  AssistantMessage,
  AttachmentImage,
  CompletionToolCall,
  ImageParams,
  Intent,
  MemoryWrite,
  UserMessage,
  UsedMemorySummary,
} from "../../lib/types";
import {
  isoToMs,
  optionalString,
  stringArray,
  stringOrNull,
} from "#chat-payload";

const ASSIST_INTENTS = new Set<Exclude<Intent, "auto">>([
  "chat",
  "vision_qa",
  "text_to_image",
  "image_to_image",
]);

const ASSIST_STATUSES = new Set<AssistantMessage["status"]>([
  "pending",
  "streaming",
  "succeeded",
  "failed",
  "canceled",
]);

type NormalizedToolStatus = CompletionToolCall["status"];

const TOOL_STATUS_MAP: Record<string, NormalizedToolStatus> = {
  queued: "queued",
  pending: "queued",
  created: "queued",
  running: "running",
  in_progress: "running",
  searching: "running",
  interpreting: "running",
  generating: "running",
  completed: "succeeded",
  complete: "succeeded",
  succeeded: "succeeded",
  success: "succeeded",
  failed: "failed",
  error: "failed",
  incomplete: "failed",
  cancelled: "cancelled",
  canceled: "cancelled",
  timed_out: "timed_out",
  timeout: "timed_out",
};

const MEMORY_WRITE_KINDS = new Set<MemoryWrite["kind"]>([
  "added",
  "updated",
  "merged",
  "superseded",
  "staged",
  "rejected_pii",
]);

type MemoryWriteType = Exclude<MemoryWrite["type"], null | undefined>;

const MEMORY_WRITE_TYPES = new Set<MemoryWriteType>([
  "profile",
  "preference",
  "avoid",
  "project",
]);

export function adaptBackendUserMessage(
  message: BackendMessage,
  attachments: AttachmentImage[],
  params: ImageParams,
  intent: Intent,
): UserMessage {
  const content = message.content ?? {};
  const text = typeof content.text === "string" ? content.text : "";
  const webSearch = content.web_search === true;
  const fileSearch = content.file_search === true;
  const codeInterpreter = content.code_interpreter === true;
  const imageGeneration = content.image_generation === true;
  return {
    id: message.id,
    role: "user",
    text,
    attachments,
    intent,
    image_params: params,
    web_search: webSearch,
    file_search: fileSearch,
    code_interpreter: codeInterpreter,
    image_generation: imageGeneration,
    created_at: isoToMs(message.created_at),
  };
}

export function coerceAssistantIntent(
  value: unknown,
  fallback: Exclude<Intent, "auto">,
): Exclude<Intent, "auto"> {
  if (
    typeof value === "string" &&
    ASSIST_INTENTS.has(value as Exclude<Intent, "auto">)
  ) {
    return value as Exclude<Intent, "auto">;
  }
  return fallback;
}

export function optionalAssistantIntent(
  value: unknown,
): Exclude<Intent, "auto"> | undefined {
  return typeof value === "string" &&
    ASSIST_INTENTS.has(value as Exclude<Intent, "auto">)
    ? (value as Exclude<Intent, "auto">)
    : undefined;
}

export function coerceAssistantStatus(
  value: unknown,
): AssistantMessage["status"] {
  if (
    typeof value === "string" &&
    ASSIST_STATUSES.has(value as AssistantMessage["status"])
  ) {
    return value as AssistantMessage["status"];
  }
  return "pending";
}

export function normalizeCompletionToolStatus(
  value: unknown,
): NormalizedToolStatus {
  if (typeof value !== "string") return "unknown";
  return TOOL_STATUS_MAP[value.trim().toLowerCase()] ?? "unknown";
}

export function coerceCompletionToolCalls(
  value: unknown,
): CompletionToolCall[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): CompletionToolCall[] => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    const id = optionalString(raw.id) ?? "";
    if (!id) return [];
    const status = normalizeCompletionToolStatus(raw.status);
    const type = typeof raw.type === "string" && raw.type ? raw.type : "tool";
    const label =
      typeof raw.label === "string" && raw.label ? raw.label : "调用工具";
    return [
      {
        id,
        type,
        status,
        label,
        name: optionalString(raw.name),
        title: typeof raw.title === "string" ? raw.title : undefined,
        error: typeof raw.error === "string" ? raw.error : undefined,
      },
    ];
  });
}

function coerceMemoryWriteKind(value: unknown): MemoryWrite["kind"] | null {
  return typeof value === "string" &&
    MEMORY_WRITE_KINDS.has(value as MemoryWrite["kind"])
    ? (value as MemoryWrite["kind"])
    : null;
}

function coerceMemoryWriteType(value: unknown): MemoryWriteType | null {
  return typeof value === "string" &&
    MEMORY_WRITE_TYPES.has(value as MemoryWriteType)
    ? (value as MemoryWriteType)
    : null;
}

function coerceMemoryWrite(value: unknown): MemoryWrite | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const kind = coerceMemoryWriteKind(raw.kind);
  if (!kind) return null;
  return {
    id: stringOrNull(raw.id),
    kind,
    type: coerceMemoryWriteType(raw.type),
    content: typeof raw.content === "string" ? raw.content : "",
    source_excerpt:
      typeof raw.source_excerpt === "string" ? raw.source_excerpt : null,
    undo_token: typeof raw.undo_token === "string" ? raw.undo_token : null,
    scope_id: typeof raw.scope_id === "string" ? raw.scope_id : null,
    recommended_scope_id:
      typeof raw.recommended_scope_id === "string"
        ? raw.recommended_scope_id
        : null,
  };
}

export function coerceMemoryWrites(value: unknown): MemoryWrite[] {
  if (!Array.isArray(value)) return [];
  const writes: MemoryWrite[] = [];
  for (const item of value) {
    const write = coerceMemoryWrite(item);
    if (write) writes.push(write);
  }
  return writes;
}

export function coerceUsedMemorySummary(
  value: unknown,
): UsedMemorySummary[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): UsedMemorySummary[] => {
    if (!item || typeof item !== "object") return [];
    const raw = item as Record<string, unknown>;
    if (
      typeof raw.id !== "string" ||
      typeof raw.type !== "string" ||
      typeof raw.content !== "string"
    ) {
      return [];
    }
    return [{ id: raw.id, type: raw.type, content: raw.content }];
  });
}

export function mergeCompletionToolCall(
  current: CompletionToolCall[] | undefined,
  incoming: CompletionToolCall,
): CompletionToolCall[] {
  const existing = current ?? [];
  const index = existing.findIndex((item) => item.id === incoming.id);
  if (index < 0) return [...existing, incoming];
  const next = existing.slice();
  next[index] = {
    ...next[index],
    ...incoming,
    name: incoming.name ?? next[index].name,
    title: incoming.title ?? next[index].title,
    error: incoming.error ?? next[index].error,
  };
  return next;
}

export function adaptBackendAssistantMessage(
  message: BackendMessage,
  parentUserId: string,
  fallbackIntent: Exclude<Intent, "auto">,
  generationIds: string[] | undefined,
  completionId: string | undefined,
): AssistantMessage {
  const content = message.content ?? {};
  const text = typeof content.text === "string" ? content.text : undefined;
  const thinking =
    typeof content.thinking === "string" ? content.thinking : undefined;
  const toolCalls = coerceCompletionToolCalls(content.tool_calls);
  const memoryWrites = coerceMemoryWrites(content.memory_writes);
  const usedMemorySummary = coerceUsedMemorySummary(
    content.used_memory_summary,
  );
  const ids = generationIds ?? [];
  return {
    id: message.id,
    role: "assistant",
    parent_user_message_id: message.parent_message_id ?? parentUserId,
    intent_resolved: coerceAssistantIntent(message.intent, fallbackIntent),
    status: coerceAssistantStatus(message.status),
    generation_ids: ids.length > 0 ? ids : undefined,
    generation_id: ids[0],
    completion_id: completionId,
    text,
    thinking,
    tool_calls: toolCalls.length > 0 ? toolCalls : undefined,
    memory_writes: memoryWrites.length > 0 ? memoryWrites : undefined,
    used_memory_ids: stringArray(content.used_memory_ids),
    used_memory_summary:
      usedMemorySummary.length > 0 ? usedMemorySummary : undefined,
    confirmation_candidate_id:
      typeof content.confirmation_candidate_id === "string"
        ? content.confirmation_candidate_id
        : undefined,
    created_at: isoToMs(message.created_at),
  };
}
