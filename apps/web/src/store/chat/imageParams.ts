import { normalizeImageModel, normalizeImageQuality } from "../../lib/imageModels.ts";
import type {
  ImageParams,
  RenderQualityChoice,
} from "../../lib/types";

export const DEFAULT_PARAMS: ImageParams = {
  model: "gpt-image-2.6-sunburst",
  aspect_ratio: "7:10",
  size_mode: "fixed",
  quality: "4k",
  render_quality: "max",
  count: 1,
  background: "opaque",
};

const IMAGE_COUNT_MIN = 1;
const IMAGE_COUNT_MAX = 10;

export function clampImageCount(count: number | undefined): number {
  if (typeof count !== "number" || !Number.isFinite(count)) {
    return IMAGE_COUNT_MIN;
  }
  return Math.max(
    IMAGE_COUNT_MIN,
    Math.min(IMAGE_COUNT_MAX, Math.trunc(count)),
  );
}

export function normalizeImageParams(params: ImageParams): ImageParams {
  const format = params.output_format;
  const specialFormat = format === "svg" || format === "gif";
  const complexity = normalizeComplexity(params.complexity);
  const outputCompression =
    typeof params.output_compression === "number" &&
    Number.isFinite(params.output_compression)
      ? Math.max(0, Math.min(100, Math.trunc(params.output_compression)))
      : undefined;
  const normalized: ImageParams = {
    ...params,
    model: normalizeImageModel(params.model),
    render_quality: normalizeImageQuality(params.render_quality, params.model),
    count: specialFormat ? 1 : clampImageCount(params.count),
    complexity,
    frame_count: format === "gif" ? boundedInteger(params.frame_count, 2, 16, 9) : undefined,
    frame_duration_ms: format === "gif" ? Math.round(boundedInteger(params.frame_duration_ms, 20, 2000, 200) / 10) * 10 : undefined,
    ...(specialFormat || outputCompression === undefined
      ? { output_compression: undefined }
      : { output_compression: outputCompression }),
  };
  if (normalized.complexity === undefined) delete normalized.complexity;
  if (normalized.frame_count === undefined) delete normalized.frame_count;
  if (normalized.frame_duration_ms === undefined) delete normalized.frame_duration_ms;
  return normalized;
}

function normalizeComplexity(value: ImageParams["complexity"]): ImageParams["complexity"] {
  return value === "low" || value === "medium" || value === "high" ? value : undefined;
}

function boundedInteger(value: number | undefined, minimum: number, maximum: number, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(minimum, Math.min(maximum, Math.trunc(value))) : fallback;
}

export function normalizeRenderQuality(
  value: ImageParams["render_quality"] | undefined,
): RenderQualityChoice {
  return value === "low" || value === "medium" || value === "high" || value === "xhigh" || value === "max"
    ? value
    : "high";
}
