import { apiFetch } from "@/lib/api/http";
import type { VideoRequestFence } from "@/lib/videoEventSnapshot";
import type {
  VideoAction,
  VideoGenerationOut,
  VideoGenerationsOut,
  VideoOptionsOut,
  VideoUploadOut,
} from "@/lib/types";

import type { NormalizedVideoImageConstraints } from "./optionsModel";
import type { ReferenceDraft } from "./types";

const VIDEO_REFRESH_RETRY_BASE_MS = 1500;
const VIDEO_REFRESH_RETRY_MAX_MS = 15000;

export type GenerationRefreshRequest = {
  controller: AbortController;
  epoch: number;
};

export type DraftUploadRequest = {
  controller: AbortController;
  draftFence: VideoRequestFence;
  epoch: number;
  expectedAction: VideoAction;
  file: File;
  imageConstraints?: NormalizedVideoImageConstraints | null;
};

export type ReferenceUploadRequest = Omit<DraftUploadRequest, "file"> & {
  items: Array<{
    file: File;
    kind: "image" | "video";
  }>;
};

export type ReferenceUploadResult =
  | {
      kind: "image";
      image_id: string;
      display: string;
      previewUrl: string | null;
    }
  | {
      kind: "video";
      video_id: string;
      display: string;
      previewUrl: string | null;
    };

export type ReferenceUploadBatchResult = {
  uploaded: ReferenceUploadResult[];
  failed: Array<{
    filename: string;
    message: string;
  }>;
};

export function fetchVideoOptions(signal: AbortSignal): Promise<VideoOptionsOut> {
  return apiFetch<VideoOptionsOut>("/videos/options", { signal });
}

export function fetchVideoGenerations(
  opts: { cursor?: string | null; limit?: number },
  signal: AbortSignal,
): Promise<VideoGenerationsOut> {
  const query = new URLSearchParams();
  if (opts.cursor) query.set("cursor", opts.cursor);
  if (opts.limit != null) query.set("limit", String(opts.limit));
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return apiFetch<VideoGenerationsOut>(`/videos/generations${suffix}`, {
    signal,
  });
}

export function fetchVideoGeneration(
  id: string,
  signal: AbortSignal,
): Promise<VideoGenerationOut> {
  return apiFetch<VideoGenerationOut>(
    `/videos/generations/${encodeURIComponent(id)}`,
    { signal },
  );
}

export function uploadReferenceVideo(
  file: File,
  signal: AbortSignal,
): Promise<VideoUploadOut> {
  const body = new FormData();
  body.append("file", file);
  return apiFetch<VideoUploadOut>("/videos/upload", {
    method: "POST",
    signal,
    body,
  });
}

type VideoImageFileMetadata = {
  size: number;
  type: string;
  width?: number;
  height?: number;
};

function formatBytes(bytes: number): string {
  const megabytes = bytes / 1024 / 1024;
  return `${Number.isInteger(megabytes) ? megabytes : megabytes.toFixed(1)} MB`;
}

function imageMimeConstraintError(
  file: VideoImageFileMetadata,
  constraints: NormalizedVideoImageConstraints,
): string | null {
  if (constraints.mimeTypes.length === 0) return null;
  if (!file.type) return null;
  if (constraints.mimeTypes.includes(file.type)) return null;
  return `图片格式需为 ${constraints.mimeTypes.join("、")}`;
}

function imageSizeConstraintError(
  file: VideoImageFileMetadata,
  constraints: NormalizedVideoImageConstraints,
): string | null {
  if (!constraints.maxBytes) return null;
  if (file.size <= constraints.maxBytes) return null;
  return `单张图片需小于 ${formatBytes(constraints.maxBytes)}`;
}

function minimumDimensionConstraintError(
  width: number,
  height: number,
  constraints: NormalizedVideoImageConstraints,
): string | null {
  const widthValid =
    constraints.minWidthPx == null || width >= constraints.minWidthPx;
  const heightValid =
    constraints.minHeightPx == null || height >= constraints.minHeightPx;
  if (widthValid && heightValid) return null;
  const minimum = Math.max(
    constraints.minWidthPx ?? 0,
    constraints.minHeightPx ?? 0,
  );
  return `图片宽高均不能小于 ${minimum}px`;
}

function maximumDimensionConstraintError(
  width: number,
  height: number,
  constraints: NormalizedVideoImageConstraints,
): string | null {
  const widthValid =
    constraints.maxWidthPx == null || width <= constraints.maxWidthPx;
  const heightValid =
    constraints.maxHeightPx == null || height <= constraints.maxHeightPx;
  if (widthValid && heightValid) return null;
  const maximum = Math.min(
    constraints.maxWidthPx ?? Number.POSITIVE_INFINITY,
    constraints.maxHeightPx ?? Number.POSITIVE_INFINITY,
  );
  return `图片宽高均不能大于 ${maximum}px`;
}

function aspectRatioConstraintError(
  width: number,
  height: number,
  constraints: NormalizedVideoImageConstraints,
): string | null {
  const aspectRatio = width / height;
  const minimumValid =
    constraints.minAspectRatio == null ||
    aspectRatio >= constraints.minAspectRatio;
  const maximumValid =
    constraints.maxAspectRatio == null ||
    aspectRatio <= constraints.maxAspectRatio;
  if (minimumValid && maximumValid) return null;
  const minimum = constraints.minAspectRatio ?? 0;
  const maximum =
    constraints.maxAspectRatio ?? Number.POSITIVE_INFINITY;
  return `图片宽高比需在 ${minimum} 到 ${maximum} 之间`;
}

export function videoImageConstraintError(
  file: VideoImageFileMetadata,
  constraints: NormalizedVideoImageConstraints | null | undefined,
): string | null {
  if (!constraints) return null;
  const metadataError =
    imageMimeConstraintError(file, constraints) ??
    imageSizeConstraintError(file, constraints);
  if (metadataError) return metadataError;
  if (file.width == null || file.height == null) return null;
  return (
    minimumDimensionConstraintError(file.width, file.height, constraints) ??
    maximumDimensionConstraintError(file.width, file.height, constraints) ??
    aspectRatioConstraintError(file.width, file.height, constraints)
  );
}

async function imageDimensions(
  file: File,
): Promise<{ width: number; height: number }> {
  if (typeof createImageBitmap === "function") {
    const bitmap = await createImageBitmap(file);
    try {
      return { width: bitmap.width, height: bitmap.height };
    } finally {
      bitmap.close();
    }
  }
  if (
    typeof Image === "undefined" ||
    typeof URL === "undefined" ||
    typeof URL.createObjectURL !== "function"
  ) {
    throw new Error("当前浏览器无法读取图片尺寸");
  }
  const objectUrl = URL.createObjectURL(file);
  try {
    return await new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () =>
        resolve({
          width: image.naturalWidth,
          height: image.naturalHeight,
        });
      image.onerror = () => reject(new Error("无法读取图片尺寸"));
      image.src = objectUrl;
    });
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

export async function validateVideoImageFile(
  file: File,
  constraints: NormalizedVideoImageConstraints | null | undefined,
): Promise<void> {
  const metadataError = videoImageConstraintError(file, constraints);
  if (metadataError) throw new Error(metadataError);
  if (
    !constraints ||
    (!constraints.minWidthPx &&
      !constraints.maxWidthPx &&
      !constraints.minHeightPx &&
      !constraints.maxHeightPx &&
      !constraints.minAspectRatio &&
      !constraints.maxAspectRatio)
  ) {
    return;
  }
  let dimensions: { width: number; height: number };
  try {
    dimensions = await imageDimensions(file);
  } catch {
    return;
  }
  const dimensionError = videoImageConstraintError(
    {
      size: file.size,
      type: file.type,
      ...dimensions,
    },
    constraints,
  );
  if (dimensionError) throw new Error(dimensionError);
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export function generationRefreshRequestIsCurrent(
  request: GenerationRefreshRequest,
  current: GenerationRefreshRequest | undefined,
  currentEpoch: number | undefined,
): boolean {
  return (
    current === request &&
    currentEpoch === request.epoch &&
    !request.controller.signal.aborted
  );
}

export function recordGenerationRefreshFailure(
  id: string,
  error: unknown,
  failureCounts: Map<string, number>,
  backoffUntil: Map<string, number>,
): void {
  const failures = (failureCounts.get(id) ?? 0) + 1;
  failureCounts.set(id, failures);
  const backoffMs = Math.min(
    VIDEO_REFRESH_RETRY_MAX_MS,
    VIDEO_REFRESH_RETRY_BASE_MS * 2 ** Math.min(failures - 1, 4),
  );
  backoffUntil.set(id, Date.now() + backoffMs);
  try {
    console.warn("[video] generation refresh failed", {
      id,
      failures,
      retryInMs: backoffMs,
      err: error,
    });
  } catch {
    // Console access is not guaranteed in every embedded browser runtime.
  }
}

function referenceObjectUrls(items: ReferenceDraft[]): Set<string> {
  return new Set(
    items
      .map((item) => item.previewUrl?.trim() ?? "")
      .filter((url) => url.startsWith("blob:")),
  );
}

export function revokeReferenceObjectUrl(
  value: string | null | undefined,
): void {
  const url = value?.trim() ?? "";
  if (
    !url.startsWith("blob:") ||
    typeof URL === "undefined" ||
    typeof URL.revokeObjectURL !== "function"
  ) {
    return;
  }
  URL.revokeObjectURL(url);
}

export function revokeUnusedReferenceObjectUrls(
  previous: ReferenceDraft[],
  next: ReferenceDraft[],
): void {
  if (
    typeof URL === "undefined" ||
    typeof URL.revokeObjectURL !== "function"
  ) {
    return;
  }
  const nextUrls = referenceObjectUrls(next);
  for (const url of referenceObjectUrls(previous)) {
    if (!nextUrls.has(url)) revokeReferenceObjectUrl(url);
  }
}
