import { Type } from "typebox";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";

import {
  AGENT_TOOL_CREATE_IMAGE,
  runtimeToolPolicy,
  type RuntimeRequest,
} from "../contracts.js";
import { ToolGatewayError, type CreateImageGateway } from "./gateway.js";

const AspectRatio = Type.Union(
  ["1:1", "16:9", "9:16", "21:9", "9:21", "10:7", "7:10", "4:5", "3:4", "4:3", "3:2", "2:3"].map(
    (value) => Type.Literal(value),
  ),
);

export interface ToolRuntimeState {
  readonly ordinals: Map<string, number>;
  readonly errors: Map<string, { code: string; resultUnknown: boolean }>;
  readonly modes: Map<string, "text_to_image" | "image_to_image">;
  nextOrdinal: number;
  calls: number;
  imageCalls: number;
  acceptedImages: number;
  successfulCalls: number;
  failedCalls: number;
  unknownResults: number;
  lastErrorCode: string | null;
  limitReason: string | null;
}

export function ordinalFor(state: ToolRuntimeState, toolCallId: string): number {
  const existing = state.ordinals.get(toolCallId);
  if (existing !== undefined) return existing;
  const ordinal = state.nextOrdinal;
  state.nextOrdinal += 1;
  state.ordinals.set(toolCallId, ordinal);
  return ordinal;
}

function rejectTool(
  state: ToolRuntimeState,
  toolCallId: string,
  code: string,
  resultUnknown = false,
): never {
  if (!state.errors.has(toolCallId)) {
    state.calls += 1;
    state.failedCalls += 1;
    state.lastErrorCode = code;
    state.errors.set(toolCallId, { code, resultUnknown });
    if (resultUnknown) state.unknownResults += 1;
  }
  throw new ToolGatewayError(code, resultUnknown);
}

export function createImageTool(
  request: RuntimeRequest,
  gateway: CreateImageGateway,
  state: ToolRuntimeState,
): ToolDefinition {
  const policy = runtimeToolPolicy(request);
  return defineTool({
    name: AGENT_TOOL_CREATE_IMAGE,
    label: "Create image",
    description:
      "Submit an asynchronous Lumen image generation request. Use only the supplied reference labels. The result returns generation IDs; do not poll or submit the same request again in this run.",
    executionMode: "sequential",
    parameters: Type.Object(
      {
        prompt: Type.String({ minLength: 1, maxLength: 10_000 }),
        reference_labels: Type.Optional(
          Type.Array(Type.String({ pattern: "^ref_(?:[1-9]|[1-5][0-9]|6[0-4])$" }), {
            maxItems: 16,
            uniqueItems: true,
          }),
        ),
        count: Type.Optional(Type.Integer({ minimum: 1, maximum: 4 })),
        aspect_ratio: Type.Optional(AspectRatio),
        quality: Type.Optional(
          Type.Union([Type.Literal("1k"), Type.Literal("2k"), Type.Literal("4k")]),
        ),
        render_quality: Type.Optional(
          Type.Union([
            Type.Literal("auto"),
            Type.Literal("low"),
            Type.Literal("medium"),
            Type.Literal("high"),
          ]),
        ),
        background: Type.Optional(
          Type.Union([
            Type.Literal("auto"),
            Type.Literal("opaque"),
            Type.Literal("transparent"),
          ]),
        ),
        output_format: Type.Optional(
          Type.Union([
            Type.Literal("png"),
            Type.Literal("jpeg"),
            Type.Literal("webp"),
          ]),
        ),
      },
      { additionalProperties: false },
    ),
    async execute(toolCallId, params, signal) {
      const ordinal = ordinalFor(state, toolCallId);
      const references = params.reference_labels ?? [];
      state.modes.set(
        toolCallId,
        references.length > 0 ? "image_to_image" : "text_to_image",
      );
      const requestedCount = params.count ?? request.image_defaults.count;
      if (state.unknownResults > 0) {
        state.limitReason = "tool_result_unknown";
        rejectTool(
          state,
          toolCallId,
          "agent_tool_result_unknown",
          true,
        );
      }
      if (
        state.imageCalls >= policy.max_image_tool_calls
      ) {
        state.limitReason = "tool_calls";
        rejectTool(
          state,
          toolCallId,
          "agent_tool_limit_reached",
        );
      }
      if (state.acceptedImages + requestedCount > policy.max_images_per_run) {
        state.limitReason = "images";
        rejectTool(
          state,
          toolCallId,
          "agent_image_limit_reached",
        );
      }
      state.calls += 1;
      state.imageCalls += 1;
      const allowedReferences = new Set(
        request.references.map((reference) => reference.reference_label),
      );
      if (references.some((label) => !allowedReferences.has(label))) {
        state.calls -= 1;
        rejectTool(
          state,
          toolCallId,
          "agent_reference_not_found",
        );
      }
      let result;
      try {
        result = await gateway(
          toolCallId,
          ordinal,
          params,
          signal,
        );
      } catch (error) {
        const candidate = error as { code?: unknown; resultUnknown?: unknown };
        const code =
          typeof candidate.code === "string" ? candidate.code : "agent_tool_failed";
        const resultUnknown = candidate.resultUnknown === true;
        if (!state.errors.has(toolCallId)) {
          state.errors.set(toolCallId, { code, resultUnknown });
          state.failedCalls += 1;
          state.lastErrorCode = code;
          if (resultUnknown) state.unknownResults += 1;
        }
        throw error;
      }
      state.acceptedImages += result.generation_ids.length;
      state.successfulCalls += 1;
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify({
              status: "accepted",
              asynchronous: true,
              mode: result.mode,
              generation_ids: result.generation_ids,
              replayed: result.replayed,
              instruction: "The jobs were accepted by Lumen. Do not poll or resubmit them in this run.",
            }),
          },
        ],
        details: {
          ordinal,
          mode: result.mode,
          generation_ids: result.generation_ids,
          replayed: result.replayed,
          accepted: result.accepted,
        },
      };
    },
  });
}
