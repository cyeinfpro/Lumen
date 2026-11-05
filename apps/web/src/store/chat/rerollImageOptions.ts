import type { Generation, ImageParams } from "../../lib/types";

// Reroll-only fields: fidelity upscale must not inherit GIF/SVG or annotations.
export function rerollImageOptions(generation: Generation): Partial<ImageParams> {
  const requested = generation.requested_params ?? {};
  const result: Partial<ImageParams> = {};
  const format = requested.output_format;
  if (format === "png" || format === "jpeg" || format === "webp" || format === "svg" || format === "gif") {
    result.output_format = format;
  }
  const complexity = requested.complexity;
  if (complexity === "low" || complexity === "medium" || complexity === "high") {
    result.complexity = complexity;
  }
  if (format === "gif") {
    for (const key of ["frame_count", "frame_duration_ms"] as const) {
      const value = requested[key];
      if (typeof value === "number" && Number.isInteger(value)) result[key] = value;
    }
  }
  return result;
}

export function rerollMarkupId(generation: Generation): string | undefined {
  const value = generation.requested_params?.markup_image_id;
  return typeof value === "string" && value.length > 0 ? value : undefined;
}
