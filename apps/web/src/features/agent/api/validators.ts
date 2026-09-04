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
  AgentToolDetails,
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
const ASPECT_RATIOS = new Set([
  "1:1",
  "16:9",
  "9:16",
  "21:9",
  "9:21",
  "10:7",
  "7:10",
  "4:5",
  "3:4",
  "4:3",
  "3:2",
  "2:3",
]);

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

function nullableNumber(
  value: unknown,
  endpoint: string,
  path: string,
): number | null {
  if (value === null) return null;
  return number(value, endpoint, path);
}

function allowedKeys(
  value: JsonObject,
  allowed: readonly string[],
  endpoint: string,
  path: string,
): void {
  const expected = new Set(allowed);
  for (const key of Object.keys(value)) {
    if (!expected.has(key)) invalid(endpoint, `${path}.${key}`);
  }
}

function exactKeys(
  value: JsonObject,
  allowed: readonly string[],
  endpoint: string,
  path: string,
): void {
  allowedKeys(value, allowed, endpoint, path);
  for (const key of allowed) {
    if (!(key in value)) invalid(endpoint, `${path}.${key}`);
  }
}

function boundedStrings(
  value: unknown,
  endpoint: string,
  path: string,
  maximumItems: number,
  maximumLength: number,
): void {
  const items = array(value, endpoint, path);
  if (items.length > maximumItems) invalid(endpoint, path);
  items.forEach((item, index) => {
    const parsed = string(item, endpoint, `${path}[${index}]`);
    if (parsed === null || parsed.length > maximumLength) {
      invalid(endpoint, `${path}[${index}]`);
    }
  });
}

function boundedNullableString(
  value: unknown,
  endpoint: string,
  path: string,
  maximum: number,
): string | null {
  const parsed = string(value, endpoint, path, true);
  if (parsed !== null && parsed.length > maximum) invalid(endpoint, path);
  return parsed;
}

function boundedInteger(
  value: unknown,
  endpoint: string,
  path: string,
  minimum: number,
  maximum: number,
): number {
  const parsed = number(value, endpoint, path);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    invalid(endpoint, path);
  }
  return parsed;
}

function boundedNullableInteger(
  value: unknown,
  endpoint: string,
  path: string,
  minimum: number,
  maximum: number,
): number | null {
  const parsed = nullableNumber(value, endpoint, path);
  if (
    parsed !== null &&
    (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum)
  ) {
    invalid(endpoint, path);
  }
  return parsed;
}

function nullableChoice(
  value: unknown,
  endpoint: string,
  path: string,
  choices: ReadonlySet<string>,
): string | null {
  const parsed = string(value, endpoint, path, true);
  if (parsed !== null && !choices.has(parsed)) invalid(endpoint, path);
  return parsed;
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

function webSearchToolDetails(
  item: JsonObject,
  endpoint: string,
  path: string,
): void {
  exactKeys(item, ["kind", "query", "result_snippets"], endpoint, path);
  const query = string(item.query, endpoint, `${path}.query`, true);
  if (query !== null && query.length > 2_000) invalid(endpoint, `${path}.query`);
  boundedStrings(item.result_snippets, endpoint, `${path}.result_snippets`, 6, 600);
}

function fileToolDetails(
  item: JsonObject,
  endpoint: string,
  path: string,
): void {
  exactKeys(
    item,
    ["kind", "file_names", "query", "line_start", "line_end", "result_snippets"],
    endpoint,
    path,
  );
  boundedStrings(item.file_names, endpoint, `${path}.file_names`, 8, 128);
  const query = string(item.query, endpoint, `${path}.query`, true);
  if (query !== null && query.length > 256) invalid(endpoint, `${path}.query`);
  for (const field of ["line_start", "line_end"] as const) {
    const line = nullableNumber(item[field], endpoint, `${path}.${field}`);
    if (line !== null && (!Number.isSafeInteger(line) || line < 1)) {
      invalid(endpoint, `${path}.${field}`);
    }
  }
  boundedStrings(item.result_snippets, endpoint, `${path}.result_snippets`, 6, 600);
}

function imageToolDetails(
  item: JsonObject,
  endpoint: string,
  path: string,
): void {
  exactKeys(
    item,
    [
      "kind",
      "prompt",
      "reference_count",
      "count",
      "aspect_ratio",
      "quality",
      "render_quality",
      "background",
      "output_format",
    ],
    endpoint,
    path,
  );
  boundedNullableString(item.prompt, endpoint, `${path}.prompt`, 4_000);
  boundedInteger(
    item.reference_count,
    endpoint,
    `${path}.reference_count`,
    0,
    16,
  );
  boundedNullableInteger(item.count, endpoint, `${path}.count`, 1, 4);
  nullableChoice(
    item.aspect_ratio,
    endpoint,
    `${path}.aspect_ratio`,
    ASPECT_RATIOS,
  );
  nullableChoice(item.quality, endpoint, `${path}.quality`, QUALITIES);
  nullableChoice(
    item.render_quality,
    endpoint,
    `${path}.render_quality`,
    RENDER_QUALITIES,
  );
  nullableChoice(item.background, endpoint, `${path}.background`, BACKGROUNDS);
  nullableChoice(item.output_format, endpoint, `${path}.output_format`, FORMATS);
}

function toolDetails(
  value: unknown,
  endpoint: string,
  path: string,
): void {
  const item = object(value, endpoint, path);
  const kind = nonempty(item.kind, endpoint, `${path}.kind`);
  if (kind === "web_search") return webSearchToolDetails(item, endpoint, path);
  if (kind === "file_list" || kind === "file_read" || kind === "file_search") {
    return fileToolDetails(item, endpoint, path);
  }
  if (kind === "image") return imageToolDetails(item, endpoint, path);
  invalid(endpoint, `${path}.kind`);
}

function toolCall(
  value: unknown,
  endpoint: string,
  path: string,
): AgentToolCall {
  const item = object(value, endpoint, path);
  allowedKeys(
    item,
    [
      "id",
      "agent_run_id",
      "ordinal",
      "name",
      "mode",
      "status",
      "generation_ids",
      "generation_count",
      "details",
      "duration_ms",
      "error_code",
      "started_at",
      "finished_at",
      "created_at",
      "updated_at",
    ],
    endpoint,
    path,
  );
  const id = nonempty(item.id, endpoint, `${path}.id`);
  const agentRunId = nonempty(
    item.agent_run_id,
    endpoint,
    `${path}.agent_run_id`,
  );
  const ordinal = number(item.ordinal, endpoint, `${path}.ordinal`);
  const name = nonempty(item.name, endpoint, `${path}.name`);
  if (
    item.mode !== null &&
    !new Set([
      "text_to_image",
      "image_to_image",
      "web_search",
      "file_list",
      "file_read",
      "file_search",
    ]).has(String(item.mode))
  ) {
    invalid(endpoint, `${path}.mode`);
  }
  if (typeof item.status !== "string" || !TOOL_STATUSES.has(item.status as AgentToolStatus)) {
    invalid(endpoint, `${path}.status`);
  }
  const generationIds = array(
    item.generation_ids,
    endpoint,
    `${path}.generation_ids`,
  ).map((generationId, index) =>
    nonempty(generationId, endpoint, `${path}.generation_ids[${index}]`),
  );
  const generationCount = number(
    item.generation_count,
    endpoint,
    `${path}.generation_count`,
  );
  const details = item.details === undefined ? null : item.details;
  if (details !== null) {
    toolDetails(details, endpoint, `${path}.details`);
  }
  const durationValue = item.duration_ms === undefined ? null : item.duration_ms;
  const duration = nullableNumber(
    durationValue,
    endpoint,
    `${path}.duration_ms`,
  );
  if (duration !== null && (!Number.isSafeInteger(duration) || duration < 0)) {
    invalid(endpoint, `${path}.duration_ms`);
  }
  const errorCode = string(
    item.error_code,
    endpoint,
    `${path}.error_code`,
    true,
  );
  const startedAt = string(
    item.started_at,
    endpoint,
    `${path}.started_at`,
    true,
  );
  const finishedAt = string(
    item.finished_at,
    endpoint,
    `${path}.finished_at`,
    true,
  );
  const createdAt = nonempty(item.created_at, endpoint, `${path}.created_at`);
  const updatedAt = nonempty(item.updated_at, endpoint, `${path}.updated_at`);
  return {
    id,
    agent_run_id: agentRunId,
    ordinal,
    name,
    mode: item.mode as AgentToolCall["mode"],
    status: item.status as AgentToolStatus,
    generation_ids: generationIds,
    generation_count: generationCount,
    details: details as AgentToolDetails | null,
    duration_ms: duration,
    error_code: errorCode,
    started_at: startedAt,
    finished_at: finishedAt,
    created_at: createdAt,
    updated_at: updatedAt,
  };
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
  for (const field of ["output_revision", "output_runtime_seq"] as const) {
    if (item[field] !== undefined) number(item[field], endpoint, `${path}.${field}`);
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
  item.tool_calls = array(
    item.tool_calls,
    endpoint,
    `${path}.tool_calls`,
  ).map((entry, index) =>
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
    "allow_web_search",
    "allow_file_tools",
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
  if (root.default_model === undefined) root.default_model = null;
  boundedNullableString(
    root.default_model,
    endpoint,
    "response.default_model",
    128,
  );
  if (root.models === undefined) root.models = [];
  root.models = array(root.models, endpoint, "response.models").map(
    (value, index) => {
      const path = `response.models[${index}]`;
      const option = object(value, endpoint, path);
      exactKeys(
        option,
        ["model", "vision_supported", "reasoning_supported"],
        endpoint,
        path,
      );
      const model = nonempty(option.model, endpoint, `${path}.model`);
      if (model.length > 128) invalid(endpoint, `${path}.model`);
      return {
        model,
        vision_supported: boolean(
          option.vision_supported,
          endpoint,
          `${path}.vision_supported`,
        ),
        reasoning_supported: boolean(
          option.reasoning_supported,
          endpoint,
          `${path}.reasoning_supported`,
        ),
      };
    },
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
