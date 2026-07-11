import type {
  ImageParams,
  RenderQualityChoice,
} from "../../lib/types";

export const DEFAULT_PARAMS: ImageParams = {
  aspect_ratio: "7:10",
  size_mode: "fixed",
  quality: "4k",
  render_quality: "high",
  count: 1,
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
  const outputCompression =
    typeof params.output_compression === "number" &&
    Number.isFinite(params.output_compression)
      ? Math.max(0, Math.min(100, Math.trunc(params.output_compression)))
      : undefined;
  return {
    ...params,
    count: clampImageCount(params.count),
    ...(outputCompression === undefined
      ? { output_compression: undefined }
      : { output_compression: outputCompression }),
  };
}

export function normalizeRenderQuality(
  value: ImageParams["render_quality"] | undefined,
): RenderQualityChoice {
  return value === "low" || value === "medium" || value === "high"
    ? value
    : "high";
}
