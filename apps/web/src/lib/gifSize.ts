import type { ImageParams } from "./types";

// Observed on flux.infpro.me/v1, 2026-11-05; not a universal provider guarantee.
// These pairs deliberately do not infer the provider's internal frame-grid budget.
export const GIF_SIZE_PRESETS = [
  { id: "512x512-2", size: "512x512", frames: 2 },
  { id: "512x512-4", size: "512x512", frames: 4 },
  { id: "512x512-9", size: "512x512", frames: 9 },
  { id: "512x512-16", size: "512x512", frames: 16 },
  { id: "960x960-9", size: "960x960", frames: 9 },
  { id: "768x432-16", size: "768x432", frames: 16 },
] as const;

export function gifPresetId(params: ImageParams): string {
  if (params.size_mode === "auto") return "";
  return GIF_SIZE_PRESETS.find((preset) =>
    preset.size === (params.fixed_size ?? "512x512") &&
    preset.frames === (params.frame_count ?? 9))?.id ?? "";
}

export function gifTimingError(params: ImageParams): string | null {
  if (params.output_format !== "gif") return null;
  const frames = params.frame_count ?? 9;
  const duration = params.frame_duration_ms ?? 200;
  if (!Number.isInteger(frames) || frames < 2 || frames > 16) return "动画帧数请选择 2–16 的整数；不会自动替换您的选择";
  if (!Number.isInteger(duration) || duration < 20 || duration > 2000 || duration % 10 !== 0) return "每帧时长请选择 20–2000 ms，且为 10 ms 的整数倍";
  return null;
}

export function gifTimingPreview(params: ImageParams): { fps: number; loopSeconds: number } | null {
  if (params.output_format !== "gif" || gifTimingError(params)) return null;
  const duration = params.frame_duration_ms ?? 200;
  return { fps: 1000 / duration, loopSeconds: (params.frame_count ?? 9) * duration / 1000 };
}

export function gifSizeError(params: ImageParams): string | null {
  if (params.output_format !== "gif" || params.size_mode === "auto") return null;
  const match = (params.fixed_size ?? "512x512").match(/^(\d+)x(\d+)$/);
  if (!match || !match.slice(1).every((value) => Number.isSafeInteger(Number(value)) && Number(value) > 0 && Number(value) % 16 === 0)) {
    return "GIF 宽高须为正整数，且均为 16 的倍数";
  }
  return null;
}

export function resolveGifSize(params: ImageParams): Pick<ImageParams, "size_mode" | "fixed_size"> {
  return params.size_mode === "auto"
    ? { size_mode: "auto", fixed_size: undefined }
    : { size_mode: "fixed", fixed_size: params.fixed_size ?? "512x512" };
}
