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
// 历史列表在窗口聚焦/可见性恢复时的陈旧阈值：数据未超过该时长则跳过全量 refetch。
export const VIDEO_HISTORY_STALE_MS = 20_000;

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
