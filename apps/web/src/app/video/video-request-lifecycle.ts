import { apiFetch } from "@/lib/api/http";
import type { VideoRequestFence } from "@/lib/videoEventSnapshot";
import type {
  VideoAction,
  VideoGenerationOut,
  VideoGenerationsOut,
  VideoOptionsOut,
  VideoUploadOut,
} from "@/lib/types";

import type { ReferenceDraft } from "./video-workbench-ui";

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
};

export type ReferenceUploadRequest = DraftUploadRequest & {
  kind: "image" | "video";
  limit: number;
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
