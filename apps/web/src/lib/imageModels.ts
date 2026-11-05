import type { ImageModel, RenderQualityChoice } from "./types";

export const DEFAULT_IMAGE_MODEL: ImageModel = "gpt-image-2.6-flare";
export const IMAGE_MODEL_OPTIONS: ReadonlyArray<{ value: ImageModel; label: string }> = [
  { value: "gpt-image-2.6-flare", label: "GPT Image 2.6 Flare" },
  { value: "gpt-image-2.6-sunburst", label: "GPT Image 2.6 Sunburst" },
];

const QUALITY_OPTIONS: ReadonlyArray<{ value: RenderQualityChoice; label: string }> = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "Xhigh" },
  { value: "max", label: "Max" },
];

export function normalizeImageModel(value: unknown): ImageModel {
  // Upgrade saved drafts and rerolls, never the original history record.
  if (value === "gpt-image-2.5-sunburst") return "gpt-image-2.6-sunburst";
  if (value === "gpt-image-2.5-flare" || value === "gpt-image-2") return DEFAULT_IMAGE_MODEL;
  return IMAGE_MODEL_OPTIONS.find((option) => option.value === value)?.value ?? DEFAULT_IMAGE_MODEL;
}

export function imageQualityOptions(_model: unknown) {
  void _model;
  return QUALITY_OPTIONS;
}

export function normalizeImageQuality(value: unknown, model: unknown): RenderQualityChoice {
  return imageQualityOptions(model).find((option) => option.value === value)?.value ?? "high";
}

export function imageParamsForReroll(generation: {
  requested_params?: Record<string, unknown> | null;
  request_params?: Record<string, unknown> | null;
  effective_params?: Record<string, unknown> | null;
}) {
  const params = generation.requested_params ?? generation.request_params ?? generation.effective_params ?? {};
  const model = normalizeImageModel(params.model ?? params.image_model);
  return { model, render_quality: normalizeImageQuality(params.render_quality, model) };
}
