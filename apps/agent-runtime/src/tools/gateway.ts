import { createHash } from "node:crypto";

import type { RuntimeRequest } from "../contracts.js";

export interface CreateImageArguments {
  readonly prompt: string;
  readonly reference_labels?: string[];
  readonly count?: number;
  readonly aspect_ratio?: string;
  readonly quality?: "1k" | "2k" | "4k";
  readonly render_quality?: "auto" | "low" | "medium" | "high";
  readonly background?: "auto" | "opaque" | "transparent";
  readonly output_format?: "png" | "jpeg" | "webp";
}

export interface CreateImageResult {
  readonly generation_ids: string[];
  readonly mode: "text_to_image" | "image_to_image";
  readonly replayed: boolean;
  readonly accepted: Required<CreateImageArguments>;
  readonly pi_tool_call_id?: string;
  readonly ordinal?: number;
  readonly request_hash?: string;
}

export interface GatewayTransportPolicy {
  readonly timeoutMs: number;
  readonly maxResponseBytes: number;
}

export const DEFAULT_GATEWAY_TRANSPORT_POLICY: GatewayTransportPolicy = {
  timeoutMs: 30_000,
  maxResponseBytes: 64 * 1024,
};

export class ToolGatewayError extends Error {
  constructor(
    readonly code: string,
    readonly resultUnknown: boolean,
  ) {
    super("Image request could not be submitted");
    this.name = "ToolGatewayError";
  }
}

const GATEWAY_ERROR_POLICY = new Map<string, boolean>([
  ["agent_capability_required", false],
  ["agent_capability_unconfigured", false],
  ["agent_capability_invalid", false],
  ["agent_capability_not_yet_valid", false],
  ["agent_capability_expired", false],
  ["agent_capability_scope_mismatch", false],
  ["agent_capability_redeemed", false],
  ["agent_image_provider_unavailable", false],
  ["agent_image_limit_reached", false],
  ["agent_reference_not_allowed", false],
  ["agent_reference_not_found", false],
  ["agent_run_not_active", false],
  ["agent_session_reference_limit_reached", false],
  ["agent_snapshot_incomplete", false],
  ["agent_stale_execution_epoch", false],
  ["agent_tool_in_progress", false],
  ["agent_tool_failed", false],
  ["agent_tool_preflight_failed", false],
  ["agent_tool_limit_reached", false],
  ["agent_tool_not_allowed", false],
  ["agent_tool_ordinal_conflict", false],
  ["agent_tool_receipt_incomplete", true],
  ["agent_tool_result_unknown", true],
  ["agent_tool_cancelled", false],
  ["agent_tool_timed_out", false],
  ["user_deleted", false],
  ["byok_disabled", false],
  ["INSUFFICIENT_BALANCE", false],
  ["NO_ACTIVE_API_KEY", false],
]);

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

function gatewayFailure(
  value: unknown,
  status: number,
): { code: string; resultUnknown: boolean } {
  const rejectedBeforeSideEffect = status >= 400 && status < 500 && status !== 408;
  if (value === null || typeof value !== "object") {
    return { code: "agent_tool_failed", resultUnknown: !rejectedBeforeSideEffect };
  }
  const root = value as { detail?: unknown; error?: unknown };
  const detail = root.detail;
  const container =
    detail !== null && typeof detail === "object"
      ? (detail as { error?: unknown })
      : root;
  const error = container.error;
  if (error === null || typeof error !== "object") {
    return { code: "agent_tool_failed", resultUnknown: !rejectedBeforeSideEffect };
  }
  const code = (error as { code?: unknown }).code;
  if (typeof code !== "string" || !GATEWAY_ERROR_POLICY.has(code)) {
    return { code: "agent_tool_failed", resultUnknown: !rejectedBeforeSideEffect };
  }
  return { code, resultUnknown: GATEWAY_ERROR_POLICY.get(code) === true };
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

function normalizedArguments(
  request: RuntimeRequest,
  arguments_: CreateImageArguments,
): Required<CreateImageArguments> {
  const prompt = arguments_.prompt.trim();
  if (prompt.length === 0) {
    throw new ToolGatewayError("agent_tool_preflight_failed", false);
  }
  return {
    prompt,
    reference_labels: [...(arguments_.reference_labels ?? [])],
    count: arguments_.count ?? request.image_defaults.count,
    aspect_ratio: arguments_.aspect_ratio ?? request.image_defaults.aspect_ratio,
    quality: arguments_.quality ?? request.image_defaults.quality,
    render_quality:
      arguments_.render_quality ?? request.image_defaults.render_quality,
    background: arguments_.background ?? request.image_defaults.background,
    output_format: arguments_.output_format ?? request.image_defaults.output_format,
  };
}

function requestHash(value: Required<CreateImageArguments>): string {
  return createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}

function sameNormalizedArguments(
  left: Required<CreateImageArguments>,
  right: Required<CreateImageArguments>,
): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

function parseGatewayResult(
  value: unknown,
  expected: {
    readonly toolCallId: string;
    readonly ordinal: number;
    readonly accepted: Required<CreateImageArguments>;
    readonly requireIdentity: boolean;
  },
): CreateImageResult {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ToolGatewayError("agent_tool_result_unknown", true);
  }
  const raw = value as Record<string, unknown>;
  const generationIds = raw.generation_ids;
  const mode = raw.mode;
  const replayed = raw.replayed;
  const accepted = raw.accepted;
  if (
    !Array.isArray(generationIds) ||
    generationIds.length < 1 ||
    generationIds.length > 4 ||
    new Set(generationIds).size !== generationIds.length ||
    generationIds.some(
      (item) => typeof item !== "string" || item.length < 1 || item.length > 96,
    ) ||
    (mode !== "text_to_image" && mode !== "image_to_image") ||
    typeof replayed !== "boolean" ||
    accepted === null ||
    typeof accepted !== "object" ||
    Array.isArray(accepted)
  ) {
    throw new ToolGatewayError("agent_tool_result_unknown", true);
  }
  const normalized = accepted as Record<string, unknown>;
  const labels = normalized.reference_labels;
  if (
    typeof normalized.prompt !== "string" ||
    normalized.prompt.length < 1 ||
    normalized.prompt.length > 10_000 ||
    !Array.isArray(labels) ||
    labels.length > 16 ||
    new Set(labels).size !== labels.length ||
    labels.some(
      (item) =>
        typeof item !== "string" ||
        !/^ref_(?:[1-9]|[1-5][0-9]|6[0-4])$/u.test(item),
    ) ||
    !Number.isInteger(normalized.count) ||
    (normalized.count as number) < 1 ||
    (normalized.count as number) > 4 ||
    normalized.count !== generationIds.length ||
    typeof normalized.aspect_ratio !== "string" ||
    !ASPECT_RATIOS.has(normalized.aspect_ratio) ||
    !new Set(["1k", "2k", "4k"]).has(String(normalized.quality)) ||
    !new Set(["auto", "low", "medium", "high"]).has(String(normalized.render_quality)) ||
    !new Set(["auto", "opaque", "transparent"]).has(String(normalized.background)) ||
    !new Set(["png", "jpeg", "webp"]).has(String(normalized.output_format))
  ) {
    throw new ToolGatewayError("agent_tool_result_unknown", true);
  }
  const acceptedResult = normalized as unknown as Required<CreateImageArguments>;
  const expectedMode = labels.length > 0 ? "image_to_image" : "text_to_image";
  const expectedHash = requestHash(expected.accepted);
  const identityPresent =
    typeof raw.pi_tool_call_id === "string" &&
    Number.isInteger(raw.ordinal) &&
    typeof raw.request_hash === "string";
  if (
    mode !== expectedMode ||
    !sameNormalizedArguments(acceptedResult, expected.accepted) ||
    (expected.requireIdentity && !identityPresent) ||
    (identityPresent &&
      (raw.pi_tool_call_id !== expected.toolCallId ||
        raw.ordinal !== expected.ordinal ||
        raw.request_hash !== expectedHash))
  ) {
    throw new ToolGatewayError("agent_tool_result_unknown", true);
  }
  return {
    generation_ids: generationIds as string[],
    mode,
    replayed,
    accepted: acceptedResult,
    ...(identityPresent
      ? {
          pi_tool_call_id: raw.pi_tool_call_id as string,
          ordinal: raw.ordinal as number,
          request_hash: raw.request_hash as string,
        }
      : {}),
  };
}

async function readBoundedJson(response: Response, maximum: number): Promise<unknown> {
  const declared = response.headers.get("content-length");
  if (declared !== null) {
    const length = Number(declared);
    if (!Number.isSafeInteger(length) || length < 0 || length > maximum) {
      await response.body?.cancel();
      throw new ToolGatewayError("agent_tool_result_unknown", true);
    }
  }
  if (response.body === null) {
    throw new ToolGatewayError("agent_tool_result_unknown", true);
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maximum) {
        await reader.cancel();
        throw new ToolGatewayError("agent_tool_result_unknown", true);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), total);
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(body);
    return JSON.parse(text) as unknown;
  } catch {
    throw new ToolGatewayError("agent_tool_result_unknown", true);
  }
}

export type CreateImageGateway = (
  toolCallId: string,
  ordinal: number,
  arguments_: CreateImageArguments,
  signal: AbortSignal | undefined,
) => Promise<CreateImageResult>;

export function createImageGateway(
  request: RuntimeRequest,
  policy: GatewayTransportPolicy = DEFAULT_GATEWAY_TRANSPORT_POLICY,
): CreateImageGateway {
  if (request.tool_gateway_url === null || request.tool_capability === null) {
    return async () => {
      throw new ToolGatewayError("agent_tool_not_allowed", false);
    };
  }
  const gatewayUrl = request.tool_gateway_url;
  const capability = request.tool_capability;
  return async (toolCallId, ordinal, arguments_, signal) => {
    const accepted = normalizedArguments(request, arguments_);
    const deadline = AbortSignal.timeout(policy.timeoutMs);
    const combinedSignal = signal
      ? AbortSignal.any([signal, deadline])
      : deadline;
    let response: Response;
    try {
      response = await fetch(gatewayUrl, {
        method: "POST",
        redirect: "error",
        headers: {
          authorization: `Bearer ${capability}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({
          pi_tool_call_id: toolCallId,
          ordinal,
          execution_epoch: request.execution_epoch,
          arguments: accepted,
        }),
        signal: combinedSignal,
      });
    } catch {
      throw new ToolGatewayError("agent_tool_result_unknown", true);
    }
    let payload: unknown;
    try {
      payload = await readBoundedJson(response, policy.maxResponseBytes);
    } catch (error) {
      if (response.status >= 400 && response.status < 500 && response.status !== 408) {
        throw new ToolGatewayError("agent_tool_failed", false);
      }
      if (error instanceof ToolGatewayError) throw error;
      throw new ToolGatewayError("agent_tool_result_unknown", true);
    }
    if (!response.ok) {
      const failure = gatewayFailure(payload, response.status);
      throw new ToolGatewayError(failure.code, failure.resultUnknown);
    }
    return parseGatewayResult(payload, {
      toolCallId,
      ordinal,
      accepted,
      requireIdentity:
        (request.version === 3 || request.version === 4) &&
        request.tool_receipt_version === 2,
    });
  };
}
