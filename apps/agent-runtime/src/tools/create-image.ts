import { Type } from "typebox";
import { defineTool, type ToolDefinition } from "@earendil-works/pi-coding-agent";

import { AGENT_TOOL_CREATE_IMAGE, type RuntimeRequest } from "../contracts.js";
import type { CreateImageGateway } from "./gateway.js";

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

export function createImageTool(
  request: RuntimeRequest,
  gateway: CreateImageGateway,
  state: ToolRuntimeState,
): ToolDefinition {
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
        state.errors.set(toolCallId, {
          code: "agent_tool_result_unknown",
          resultUnknown: true,
        });
        throw new Error("A prior image submission is still unconfirmed");
      }
      if (
        state.calls >= request.limits.max_tool_calls ||
        state.imageCalls >= request.limits.max_image_tool_calls
      ) {
        state.limitReason = "tool_calls";
        throw new Error("The image tool limit has been reached");
      }
      if (state.acceptedImages + requestedCount > request.limits.max_images_per_run) {
        state.limitReason = "images";
        throw new Error("The image count limit has been reached");
      }
      state.calls += 1;
      state.imageCalls += 1;
      const allowedReferences = new Set(
        request.references.map((reference) => reference.reference_label),
      );
      if (references.some((label) => !allowedReferences.has(label))) {
        state.failedCalls += 1;
        state.lastErrorCode = "agent_reference_not_found";
        state.errors.set(toolCallId, {
          code: "agent_reference_not_found",
          resultUnknown: false,
        });
        throw new Error("A requested reference is not available in this run");
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
        state.errors.set(toolCallId, { code, resultUnknown });
        state.failedCalls += 1;
        state.lastErrorCode = code;
        if (resultUnknown) state.unknownResults += 1;
        throw error;
      }
      state.acceptedImages += result.generation_ids.length;
      state.successfulCalls += 1;
      return {
        content: [
          {
            type: "text",
            text: JSON.stringify({
              accepted: true,
              mode: result.mode,
              generation_ids: result.generation_ids,
              replayed: result.replayed,
              accepted_parameters: result.accepted,
              instruction: "The jobs run asynchronously in Lumen. Do not poll or resubmit them.",
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
