import type { GenerationRefreshRequest } from "./video-request-lifecycle";
import type { VideoFeedScopeToken } from "./video-feed-scope";

export const VIDEO_EVENTS = [
  "video.queued",
  "video.submitted",
  "video.progress",
  "video.fetching",
  "video.succeeded",
  "video.failed",
  "video.canceled",
];
export const VIDEO_REFRESH_MIN_INTERVAL_MS = 900;
export const VIDEO_HISTORY_PAGE_SIZE = 12;

export type GenerationRefreshOptions = {
  forceHistorySync?: boolean;
};

export type GenerationRefreshScheduleOptions = GenerationRefreshOptions & {
  delayMs?: number;
};

export type ScheduleGenerationRefresh = (
  id: string,
  opts?: GenerationRefreshScheduleOptions,
) => void;

export type ScopedGenerationRefreshRequest = GenerationRefreshRequest & {
  scope: VideoFeedScopeToken;
};
