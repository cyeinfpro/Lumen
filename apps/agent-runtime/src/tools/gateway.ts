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
}

export class ToolGatewayError extends Error {
  constructor(
    readonly code: string,
    readonly resultUnknown: boolean,
  ) {
    super("Image request could not be submitted");
    this.name = "ToolGatewayError";
  }
}

const SAFE_GATEWAY_CODES = new Set([
  "agent_image_provider_unavailable",
  "agent_image_limit_reached",
  "agent_reference_not_allowed",
  "agent_reference_not_found",
  "agent_run_not_active",
  "agent_session_reference_limit_reached",
  "agent_stale_execution_epoch",
  "agent_tool_limit_reached",
  "agent_tool_not_allowed",
  "agent_tool_ordinal_conflict",
  "byok_disabled",
  "INSUFFICIENT_BALANCE",
  "NO_ACTIVE_API_KEY",
]);

function gatewayCode(value: unknown): string {
  if (value === null || typeof value !== "object") return "agent_tool_failed";
  const root = value as { detail?: unknown; error?: unknown };
  const detail = root.detail;
  const container =
    detail !== null && typeof detail === "object"
      ? (detail as { error?: unknown })
      : root;
  const error = container.error;
  if (error === null || typeof error !== "object") return "agent_tool_failed";
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" && SAFE_GATEWAY_CODES.has(code)
    ? code
    : "agent_tool_failed";
}

function parseGatewayResult(value: unknown): CreateImageResult {
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
    generationIds.some((item) => typeof item !== "string" || item.length < 1 || item.length > 96) ||
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
    labels.some((item) => typeof item !== "string") ||
    !Number.isInteger(normalized.count) ||
    typeof normalized.aspect_ratio !== "string" ||
    !new Set(["1k", "2k", "4k"]).has(String(normalized.quality)) ||
    !new Set(["auto", "low", "medium", "high"]).has(String(normalized.render_quality)) ||
    !new Set(["auto", "opaque", "transparent"]).has(String(normalized.background)) ||
    !new Set(["png", "jpeg", "webp"]).has(String(normalized.output_format))
  ) {
    throw new ToolGatewayError("agent_tool_result_unknown", true);
  }
  return {
    generation_ids: generationIds as string[],
    mode,
    replayed,
    accepted: normalized as unknown as Required<CreateImageArguments>,
  };
}

export type CreateImageGateway = (
  toolCallId: string,
  ordinal: number,
  arguments_: CreateImageArguments,
  signal: AbortSignal | undefined,
) => Promise<CreateImageResult>;

export function createImageGateway(request: RuntimeRequest): CreateImageGateway {
  if (request.tool_gateway_url === null || request.tool_capability === null) {
    return async () => {
      throw new ToolGatewayError("agent_tool_not_allowed", false);
    };
  }
  const gatewayUrl = request.tool_gateway_url;
  const capability = request.tool_capability;
  return async (toolCallId, ordinal, arguments_, signal) => {
    const timeoutSignal = AbortSignal.timeout(request.limits.tool_timeout_seconds * 1000);
    const combinedSignal = signal
      ? AbortSignal.any([signal, timeoutSignal])
      : timeoutSignal;
    let response: Response;
    try {
      response = await fetch(gatewayUrl, {
        method: "POST",
        headers: {
          authorization: `Bearer ${capability}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({
          pi_tool_call_id: toolCallId,
          ordinal,
          execution_epoch: request.execution_epoch,
          arguments: arguments_,
        }),
        signal: combinedSignal,
      });
    } catch {
      throw new ToolGatewayError("agent_tool_result_unknown", true);
    }
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new ToolGatewayError("agent_tool_result_unknown", true);
    }
    if (!response.ok) {
      throw new ToolGatewayError(
        gatewayCode(payload),
        response.status === 408 || response.status >= 500,
      );
    }
    return parseGatewayResult(payload);
  };
}
