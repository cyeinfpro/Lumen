import { ApiError } from "./errors";

type JsonRecord = Record<string, unknown>;
type TaskStatus =
  | "queued"
  | "running"
  | "streaming"
  | "succeeded"
  | "failed"
  | "canceled";

const GENERATION_STATUSES = new Set<TaskStatus>([
  "queued",
  "running",
  "succeeded",
  "failed",
  "canceled",
]);
const COMPLETION_STATUSES = new Set<TaskStatus>([
  "queued",
  "streaming",
  "succeeded",
  "failed",
  "canceled",
]);

function schemaError(endpoint: string, issue: string): never {
  throw new ApiError({
    code: "response_schema_error",
    message: `Invalid response from ${endpoint}: ${issue}`,
    status: 502,
    payload: { endpoint, issue },
  });
}

function record(value: unknown, endpoint: string, path = "response"): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    schemaError(endpoint, `${path} must be an object`);
  }
  return value as JsonRecord;
}

function requiredString(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): string {
  const field = value[key];
  if (typeof field !== "string" || field.length === 0) {
    schemaError(endpoint, `${path}.${key} must be a non-empty string`);
  }
  return field;
}

function requiredText(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): string {
  const field = value[key];
  if (typeof field !== "string") {
    schemaError(endpoint, `${path}.${key} must be a string`);
  }
  return field;
}

function optionalString(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): void {
  const field = value[key];
  if (field !== undefined && typeof field !== "string") {
    schemaError(endpoint, `${path}.${key} must be a string when present`);
  }
}

function nullableString(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): void {
  const field = value[key];
  if (field !== null && typeof field !== "string") {
    schemaError(endpoint, `${path}.${key} must be a string or null`);
  }
}

function optionalNullableString(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): void {
  const field = value[key];
  if (field !== undefined && field !== null && typeof field !== "string") {
    schemaError(endpoint, `${path}.${key} must be a string or null when present`);
  }
}

function requiredBoolean(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): void {
  if (typeof value[key] !== "boolean") {
    schemaError(endpoint, `${path}.${key} must be a boolean`);
  }
}

function optionalBoolean(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): void {
  const field = value[key];
  if (field !== undefined && typeof field !== "boolean") {
    schemaError(endpoint, `${path}.${key} must be a boolean when present`);
  }
}

function requiredNumber(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): void {
  const field = value[key];
  if (typeof field !== "number" || !Number.isFinite(field)) {
    schemaError(endpoint, `${path}.${key} must be a finite number`);
  }
}

function optionalNumber(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): void {
  const field = value[key];
  if (
    field !== undefined &&
    (typeof field !== "number" || !Number.isFinite(field))
  ) {
    schemaError(endpoint, `${path}.${key} must be a finite number when present`);
  }
}

function requiredStringArray(
  value: JsonRecord,
  key: string,
  endpoint: string,
  path = "response",
): string[] {
  const field = value[key];
  if (
    !Array.isArray(field) ||
    field.some((item) => typeof item !== "string" || item.length === 0)
  ) {
    schemaError(endpoint, `${path}.${key} must contain non-empty strings`);
  }
  return field as string[];
}

function requiredStatus(
  value: JsonRecord,
  allowed: Set<TaskStatus>,
  endpoint: string,
  path: string,
): void {
  const status = value.status;
  if (typeof status !== "string" || !allowed.has(status as TaskStatus)) {
    schemaError(endpoint, `${path}.status is invalid`);
  }
}

export function validateAuthUser<T>(value: unknown): T {
  const endpoint = "/auth/me";
  const user = record(value, endpoint);
  requiredString(user, "id", endpoint);
  optionalString(user, "email", endpoint);
  optionalString(user, "name", endpoint);
  optionalString(user, "display_name", endpoint);
  optionalBoolean(user, "notification_email", endpoint);
  optionalNullableString(user, "default_system_prompt_id", endpoint);
  if (
    user.account_mode !== undefined &&
    user.account_mode !== "wallet" &&
    user.account_mode !== "byok"
  ) {
    schemaError(endpoint, "response.account_mode is invalid");
  }
  if (
    user.role !== undefined &&
    user.role !== "admin" &&
    user.role !== "member"
  ) {
    schemaError(endpoint, "response.role is invalid");
  }
  if (user.runtime_defaults !== undefined) {
    const defaults = record(
      user.runtime_defaults,
      endpoint,
      "response.runtime_defaults",
    );
    optionalBoolean(defaults, "fast", endpoint, "response.runtime_defaults");
    optionalNumber(
      defaults,
      "upload_max_source_bytes",
      endpoint,
      "response.runtime_defaults",
    );
    optionalBoolean(
      defaults,
      "canvas_enabled",
      endpoint,
      "response.runtime_defaults",
    );
    if (defaults.nav_visibility !== undefined) {
      const visibility = record(
        defaults.nav_visibility,
        endpoint,
        "response.runtime_defaults.nav_visibility",
      );
      for (const key of ["studio", "video", "projects", "assets"]) {
        optionalBoolean(
          visibility,
          key,
          endpoint,
          "response.runtime_defaults.nav_visibility",
        );
      }
    }
  }
  return value as T;
}

function validateGeneration<T>(
  value: unknown,
  endpoint: string,
  index: number,
): T {
  const path = `response.generations[${index}]`;
  const task = record(value, endpoint, path);
  for (const key of ["id", "message_id"]) {
    requiredString(task, key, endpoint, path);
  }
  for (const key of [
    "action",
    "prompt",
    "size_requested",
    "aspect_ratio",
    "progress_stage",
  ]) {
    requiredText(task, key, endpoint, path);
  }
  requiredStringArray(task, "input_image_ids", endpoint, path);
  nullableString(task, "primary_input_image_id", endpoint, path);
  requiredStatus(task, GENERATION_STATUSES, endpoint, path);
  requiredNumber(task, "attempt", endpoint, path);
  for (const key of [
    "error_code",
    "error_message",
    "started_at",
    "finished_at",
  ]) {
    nullableString(task, key, endpoint, path);
  }
  return value as T;
}

function validateCompletion<T>(
  value: unknown,
  endpoint: string,
  index: number,
): T {
  const path = `response.completions[${index}]`;
  const task = record(value, endpoint, path);
  for (const key of ["id", "message_id"]) {
    requiredString(task, key, endpoint, path);
  }
  for (const key of ["model", "text", "progress_stage"]) {
    requiredText(task, key, endpoint, path);
  }
  requiredStringArray(task, "input_image_ids", endpoint, path);
  requiredStatus(task, COMPLETION_STATUSES, endpoint, path);
  for (const key of ["tokens_in", "tokens_out", "attempt"]) {
    requiredNumber(task, key, endpoint, path);
  }
  for (const key of [
    "error_code",
    "error_message",
    "started_at",
    "finished_at",
  ]) {
    nullableString(task, key, endpoint, path);
  }
  return value as T;
}

export function validateActiveTasksResponse<T>(
  value: unknown,
): T {
  const endpoint = "/tasks/mine/active";
  const response = record(value, endpoint);
  if (!Array.isArray(response.generations)) {
    schemaError(endpoint, "response.generations must be an array");
  }
  if (!Array.isArray(response.completions)) {
    schemaError(endpoint, "response.completions must be an array");
  }
  response.generations.forEach((item, index) =>
    validateGeneration(item, endpoint, index),
  );
  response.completions.forEach((item, index) =>
    validateCompletion(item, endpoint, index),
  );
  return value as T;
}

export function validateSystemSettings<T>(value: unknown): T {
  const endpoint = "/admin/settings";
  const response = record(value, endpoint);
  if (!Array.isArray(response.items)) {
    schemaError(endpoint, "response.items must be an array");
  }
  response.items.forEach((item, index) => {
    const path = `response.items[${index}]`;
    const setting = record(item, endpoint, path);
    requiredString(setting, "key", endpoint, path);
    nullableString(setting, "value", endpoint, path);
    requiredBoolean(setting, "has_value", endpoint, path);
    requiredBoolean(setting, "is_sensitive", endpoint, path);
    requiredText(setting, "description", endpoint, path);
  });
  return value as T;
}

export function validateMemorySettings<T>(value: unknown): T {
  const endpoint = "/me/memory-settings";
  const response = record(value, endpoint);
  for (const key of [
    "paused",
    "disabled",
    "confirmation_enabled",
    "embedding_available",
  ]) {
    requiredBoolean(response, key, endpoint);
  }
  requiredNumber(response, "extraction_threshold", endpoint);
  requiredNumber(response, "onboarding_seen", endpoint);
  return value as T;
}

export function validateByokSettings<T>(value: unknown): T {
  const endpoint = "/admin/byok-settings";
  const response = record(value, endpoint);
  for (const key of [
    "mode_enabled",
    "byok_signup_enabled",
    "byok_signup_bypasses_allowlist",
    "fallback_to_admin_provider",
    "retention_hide_enabled",
    "retention_delete_enabled",
  ]) {
    requiredBoolean(response, key, endpoint);
  }
  requiredText(response, "validation_model", endpoint);
  for (const key of [
    "validation_timeout_ms",
    "pending_token_ttl_seconds",
    "retention_hide_days",
    "retention_delete_days",
  ]) {
    requiredNumber(response, key, endpoint);
  }
  return value as T;
}

export function validateShare<T>(value: unknown): T {
  const endpoint = "/images/share";
  const response = record(value, endpoint);
  for (const key of [
    "id",
    "image_id",
    "token",
    "url",
    "image_url",
    "created_at",
  ]) {
    requiredString(response, key, endpoint);
  }
  requiredStringArray(response, "image_ids", endpoint);
  requiredBoolean(response, "show_prompt", endpoint);
  nullableString(response, "expires_at", endpoint);
  nullableString(response, "revoked_at", endpoint);
  return value as T;
}

export function validateUploadedImage<T>(value: unknown): T {
  const endpoint = "/images/upload";
  const response = record(value, endpoint);
  requiredString(response, "id", endpoint);
  requiredString(response, "url", endpoint);
  requiredNumber(response, "width", endpoint);
  requiredNumber(response, "height", endpoint);
  for (const key of ["display_url", "preview_url", "thumb_url"]) {
    optionalNullableString(response, key, endpoint);
  }
  optionalString(response, "mime", endpoint);
  if (
    response.metadata_jsonb !== undefined &&
    response.metadata_jsonb !== null
  ) {
    record(response.metadata_jsonb, endpoint, "response.metadata_jsonb");
  }
  return value as T;
}
