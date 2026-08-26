import { ApiError } from "@/lib/api/errors";
import type {
  AgentBackendMessage,
  AgentImageDefaults,
  AgentMessageCreateResult,
  AgentMessageList,
  AgentReference,
  AgentRun,
  AgentRunStatus,
  AgentSession,
  AgentSessionImageList,
  AgentSessionList,
  AgentStatus,
  AgentToolCall,
  AgentToolStatus,
} from "../model/contracts";

type JsonObject = Record<string, unknown>;

const RUN_STATUSES = new Set<AgentRunStatus>([
  "queued",
  "running",
  "succeeded",
  "partial",
  "failed",
  "cancelled",
]);
const TOOL_STATUSES = new Set<AgentToolStatus>([
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
]);
const QUALITIES = new Set(["1k", "2k", "4k"]);
const RENDER_QUALITIES = new Set(["auto", "low", "medium", "high"]);
const BACKGROUNDS = new Set(["auto", "opaque", "transparent"]);
const FORMATS = new Set(["png", "jpeg", "webp"]);

function invalid(endpoint: string, path: string): never {
  throw new ApiError({
    code: "response_schema_error",
    message: `Agent response is invalid at ${path}`,
    status: 502,
    payload: { endpoint, path },
  });
}

function object(value: unknown, endpoint: string, path: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    invalid(endpoint, path);
  }
  return value as JsonObject;
}

function array(value: unknown, endpoint: string, path: string): unknown[] {
  if (!Array.isArray(value)) invalid(endpoint, path);
  return value;
}

function string(
  value: unknown,
  endpoint: string,
  path: string,
  nullable = false,
): string | null {
  if (nullable && value === null) return null;
  if (typeof value !== "string") invalid(endpoint, path);
  return value;
}

function nonempty(value: unknown, endpoint: string, path: string): string {
  const result = string(value, endpoint, path);
  if (!result) invalid(endpoint, path);
  return result;
}

function number(value: unknown, endpoint: string, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    invalid(endpoint, path);
  }
  return value;
}

function boolean(value: unknown, endpoint: string, path: string): boolean {
  if (typeof value !== "boolean") invalid(endpoint, path);
  return value;
}

function imageDefaults(
  value: unknown,
  endpoint: string,
  path: string,
): AgentImageDefaults {
  const item = object(value, endpoint, path);
  const count = number(item.count, endpoint, `${path}.count`);
  const aspectRatio = nonempty(
    item.aspect_ratio,
    endpoint,
    `${path}.aspect_ratio`,
  );
  const quality = nonempty(item.quality, endpoint, `${path}.quality`);
  const renderQuality = nonempty(
    item.render_quality,
    endpoint,
    `${path}.render_quality`,
  );
  const background = nonempty(
    item.background,
    endpoint,
    `${path}.background`,
  );
  const outputFormat = nonempty(
    item.output_format,
    endpoint,
    `${path}.output_format`,
  );
  if (
    count < 1 ||
    count > 4 ||
    !/^\d+:\d+$/.test(aspectRatio) ||
    !QUALITIES.has(quality) ||
    !RENDER_QUALITIES.has(renderQuality) ||
    !BACKGROUNDS.has(background) ||
    !FORMATS.has(outputFormat)
  ) {
    invalid(endpoint, path);
  }
  return item as unknown as AgentImageDefaults;
}

function reference(
  value: unknown,
  endpoint: string,
  path: string,
): AgentReference {
  const item = object(value, endpoint, path);
  nonempty(item.id, endpoint, `${path}.id`);
  nonempty(item.image_id, endpoint, `${path}.image_id`);
  number(item.ordinal, endpoint, `${path}.ordinal`);
  nonempty(item.reference_label, endpoint, `${path}.reference_label`);
  nonempty(item.role, endpoint, `${path}.role`);
  string(item.display_label, endpoint, `${path}.display_label`, true);
  return item as unknown as AgentReference;
}

function toolCall(
  value: unknown,
  endpoint: string,
  path: string,
): AgentToolCall {
  const item = object(value, endpoint, path);
  nonempty(item.id, endpoint, `${path}.id`);
  nonempty(item.agent_run_id, endpoint, `${path}.agent_run_id`);
  number(item.ordinal, endpoint, `${path}.ordinal`);
  nonempty(item.name, endpoint, `${path}.name`);
  if (
    item.mode !== null &&
    item.mode !== "text_to_image" &&
    item.mode !== "image_to_image"
  ) {
    invalid(endpoint, `${path}.mode`);
  }
  if (typeof item.status !== "string" || !TOOL_STATUSES.has(item.status as AgentToolStatus)) {
    invalid(endpoint, `${path}.status`);
  }
  array(item.generation_ids, endpoint, `${path}.generation_ids`).forEach(
    (id, index) => nonempty(id, endpoint, `${path}.generation_ids[${index}]`),
  );
  number(item.generation_count, endpoint, `${path}.generation_count`);
  for (const field of ["error_code", "started_at", "finished_at"] as const) {
    string(item[field], endpoint, `${path}.${field}`, true);
  }
  nonempty(item.created_at, endpoint, `${path}.created_at`);
  nonempty(item.updated_at, endpoint, `${path}.updated_at`);
  return item as unknown as AgentToolCall;
}

export function parseAgentRun(
  value: unknown,
  endpoint: string,
  path = "response",
): AgentRun {
  const item = object(value, endpoint, path);
  for (const field of [
    "id",
    "agent_session_id",
    "user_message_id",
    "assistant_message_id",
    "idempotency_key",
    "created_at",
    "updated_at",
  ] as const) {
    nonempty(item[field], endpoint, `${path}.${field}`);
  }
  if (typeof item.status !== "string" || !RUN_STATUSES.has(item.status as AgentRunStatus)) {
    invalid(endpoint, `${path}.status`);
  }
  for (const field of [
    "execution_epoch",
    "last_event_seq",
    "turn_count",
    "tool_call_count",
  ] as const) {
    number(item[field], endpoint, `${path}.${field}`);
  }
  for (const field of [
    "model",
    "reasoning_effort",
    "error_code",
    "error_message",
    "started_at",
    "finished_at",
    "cancel_requested_at",
  ] as const) {
    string(item[field], endpoint, `${path}.${field}`, true);
  }
  if (
    item.memory_state !== undefined &&
    item.memory_state !== null &&
    !new Set(["disabled", "empty", "ready", "degraded"]).has(
      String(item.memory_state),
    )
  ) {
    invalid(endpoint, `${path}.memory_state`);
  }
  if (item.continuable !== undefined) {
    boolean(item.continuable, endpoint, `${path}.continuable`);
  }
  object(item.usage, endpoint, `${path}.usage`);
  array(item.references, endpoint, `${path}.references`).forEach((entry, index) =>
    reference(entry, endpoint, `${path}.references[${index}]`),
  );
  array(item.tool_calls, endpoint, `${path}.tool_calls`).forEach((entry, index) =>
    toolCall(entry, endpoint, `${path}.tool_calls[${index}]`),
  );
  return item as unknown as AgentRun;
}

export function parseAgentSession(
  value: unknown,
  endpoint: string,
  path = "response",
): AgentSession {
  const item = object(value, endpoint, path);
  for (const field of [
    "id",
    "conversation_id",
    "title",
    "runtime_version",
    "last_activity_at",
    "created_at",
    "updated_at",
  ] as const) {
    string(item[field], endpoint, `${path}.${field}`);
  }
  for (const field of [
    "pinned",
    "archived",
    "memory_disabled",
    "allow_image",
  ] as const) {
    boolean(item[field], endpoint, `${path}.${field}`);
  }
  for (const field of [
    "active_scope_id",
    "default_system",
    "default_system_prompt_id",
  ] as const) {
    string(item[field], endpoint, `${path}.${field}`, true);
  }
  imageDefaults(item.image_defaults, endpoint, `${path}.image_defaults`);
  if (item.active_run !== null) {
    parseAgentRun(item.active_run, endpoint, `${path}.active_run`);
  }
  return item as unknown as AgentSession;
}

function message(
  value: unknown,
  endpoint: string,
  path: string,
): AgentBackendMessage {
  const item = object(value, endpoint, path);
  for (const field of ["id", "conversation_id", "created_at"] as const) {
    nonempty(item[field], endpoint, `${path}.${field}`);
  }
  if (!new Set(["user", "assistant", "system"]).has(String(item.role))) {
    invalid(endpoint, `${path}.role`);
  }
  object(item.content, endpoint, `${path}.content`);
  for (const field of ["intent", "status", "parent_message_id"] as const) {
    string(item[field], endpoint, `${path}.${field}`, true);
  }
  return item as unknown as AgentBackendMessage;
}

function taskEnvelopeArray(
  root: JsonObject,
  key: string,
  endpoint: string,
): unknown[] {
  return array(root[key], endpoint, `response.${key}`);
}

export function validateAgentSessionList<T>(value: unknown): T {
  const endpoint = "/agent/sessions";
  const root = object(value, endpoint, "response");
  array(root.items, endpoint, "response.items").forEach((item, index) =>
    parseAgentSession(item, endpoint, `response.items[${index}]`),
  );
  string(root.next_cursor, endpoint, "response.next_cursor", true);
  return root as unknown as T;
}

export function validateAgentSession<T>(value: unknown): T {
  return parseAgentSession(value, "/agent/sessions") as T;
}

export function validateAgentRun<T>(value: unknown): T {
  return parseAgentRun(value, "/agent/runs") as T;
}

export function validateNullableAgentRun<T>(value: unknown): T {
  if (value === null) return value as T;
  return parseAgentRun(value, "/agent/sessions/active-run") as T;
}

export function validateAgentMessages<T>(value: unknown): T {
  const endpoint = "/agent/sessions/messages";
  const root = object(value, endpoint, "response");
  taskEnvelopeArray(root, "items", endpoint).forEach((item, index) =>
    message(item, endpoint, `response.items[${index}]`),
  );
  taskEnvelopeArray(root, "runs", endpoint).forEach((item, index) =>
    parseAgentRun(item, endpoint, `response.runs[${index}]`),
  );
  for (const key of ["generations", "completions", "images"] as const) {
    taskEnvelopeArray(root, key, endpoint).forEach((item, index) =>
      object(item, endpoint, `response.${key}[${index}]`),
    );
  }
  string(root.next_cursor, endpoint, "response.next_cursor", true);
  return root as unknown as T;
}

export function validateAgentMessageCreate<T>(value: unknown): T {
  const endpoint = "/agent/sessions/messages";
  const root = object(value, endpoint, "response");
  message(root.user_message, endpoint, "response.user_message");
  message(root.assistant_message, endpoint, "response.assistant_message");
  parseAgentRun(root.agent_run, endpoint, "response.agent_run");
  return root as unknown as T;
}

export function validateAgentStatus<T>(value: unknown): T {
  const endpoint = "/agent/status";
  const root = object(value, endpoint, "response");
  boolean(root.enabled, endpoint, "response.enabled");
  boolean(
    root.tool_gateway_configured,
    endpoint,
    "response.tool_gateway_configured",
  );
  return root as unknown as T;
}

export function validateAgentSessionImages<T>(value: unknown): T {
  const endpoint = "/agent/sessions/images";
  const root = object(value, endpoint, "response");
  number(root.used, endpoint, "response.used");
  number(root.maximum, endpoint, "response.maximum");
  array(root.items, endpoint, "response.items").forEach((value, index) => {
    const item = object(value, endpoint, `response.items[${index}]`);
    for (const field of [
      "image_id",
      "reference_label",
      "role",
      "source",
    ] as const) {
      nonempty(item[field], endpoint, `response.items[${index}].${field}`);
    }
    string(
      item.display_label,
      endpoint,
      `response.items[${index}].display_label`,
      true,
    );
    boolean(item.active, endpoint, `response.items[${index}].active`);
  });
  return root as unknown as T;
}

export type {
  AgentMessageCreateResult,
  AgentMessageList,
  AgentSessionList,
  AgentSessionImageList,
  AgentStatus,
};
