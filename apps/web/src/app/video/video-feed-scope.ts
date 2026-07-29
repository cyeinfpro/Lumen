export interface VideoFeedAbortableRequest {
  controller: AbortController;
}

export interface VideoFeedScopeToken {
  generation: number;
  userId: string | null;
}

export interface VideoFeedRuntime<
  TRequest extends VideoFeedAbortableRequest = VideoFeedAbortableRequest,
> {
  userId: string | null;
  generation: number;
  terminalHistorySynced: Set<string>;
  generationRefreshRequests: Map<string, TRequest>;
  generationRefreshEpochs: Map<string, number>;
  scheduledRefreshTimers: Map<string, number>;
  pendingHistoryRefreshes: Set<string>;
  lastRefreshAt: Map<string, number>;
  refreshBackoffUntil: Map<string, number>;
  refreshFailureCounts: Map<string, number>;
}

export interface VideoFeedRuntimeSnapshot {
  userId: string | null;
  generation: number;
  requests: number;
  timers: number;
  terminalHistorySynced: number;
  pendingHistoryRefreshes: number;
  refreshEpochs: number;
  refreshTimestamps: number;
  refreshBackoffs: number;
  refreshFailures: number;
}

export function normalizeVideoFeedUserId(
  userId: string | null | undefined,
): string | null {
  const normalized = userId?.trim() ?? "";
  return normalized || null;
}

export function createVideoFeedRuntime<
  TRequest extends VideoFeedAbortableRequest = VideoFeedAbortableRequest,
>(
  userId: string | null | undefined,
): VideoFeedRuntime<TRequest> {
  return {
    userId: normalizeVideoFeedUserId(userId),
    generation: 0,
    terminalHistorySynced: new Set(),
    generationRefreshRequests: new Map(),
    generationRefreshEpochs: new Map(),
    scheduledRefreshTimers: new Map(),
    pendingHistoryRefreshes: new Set(),
    lastRefreshAt: new Map(),
    refreshBackoffUntil: new Map(),
    refreshFailureCounts: new Map(),
  };
}

export function videoFeedScopeToken(
  runtime: VideoFeedRuntime,
): VideoFeedScopeToken {
  return {
    userId: runtime.userId,
    generation: runtime.generation,
  };
}

export function isVideoFeedRuntimeCurrent(
  runtime: VideoFeedRuntime,
  userId: string | null | undefined,
): boolean {
  return runtime.userId === normalizeVideoFeedUserId(userId);
}

export function isVideoFeedScopeTokenCurrent(
  runtime: VideoFeedRuntime,
  token: VideoFeedScopeToken,
): boolean {
  return (
    runtime.userId === token.userId &&
    runtime.generation === token.generation
  );
}

export function resetVideoFeedRuntime(
  runtime: VideoFeedRuntime,
  nextUserId: string | null | undefined,
  clearTimer: (timer: number) => void,
): boolean {
  const normalizedUserId = normalizeVideoFeedUserId(nextUserId);
  if (runtime.userId === normalizedUserId) return false;
  clearVideoFeedRuntime(runtime, clearTimer);
  runtime.userId = normalizedUserId;
  runtime.generation += 1;
  return true;
}

export function disposeVideoFeedRuntime(
  runtime: VideoFeedRuntime,
  clearTimer: (timer: number) => void,
): void {
  clearVideoFeedRuntime(runtime, clearTimer);
  runtime.generation += 1;
}

export function videoFeedChannels(
  runtime: VideoFeedRuntime,
  userId: string | null | undefined,
  activeItems: readonly { id: string }[],
): string[] {
  if (
    !normalizeVideoFeedUserId(userId) ||
    !isVideoFeedRuntimeCurrent(runtime, userId)
  ) {
    return [];
  }
  return [
    ...new Set(
      activeItems
        .map((item) => item.id.trim())
        .filter(Boolean)
        .map((id) => `task:${id}`),
    ),
  ];
}

export function videoFeedRuntimeSnapshot(
  runtime: VideoFeedRuntime,
): VideoFeedRuntimeSnapshot {
  return {
    userId: runtime.userId,
    generation: runtime.generation,
    requests: runtime.generationRefreshRequests.size,
    timers: runtime.scheduledRefreshTimers.size,
    terminalHistorySynced: runtime.terminalHistorySynced.size,
    pendingHistoryRefreshes: runtime.pendingHistoryRefreshes.size,
    refreshEpochs: runtime.generationRefreshEpochs.size,
    refreshTimestamps: runtime.lastRefreshAt.size,
    refreshBackoffs: runtime.refreshBackoffUntil.size,
    refreshFailures: runtime.refreshFailureCounts.size,
  };
}

function clearVideoFeedRuntime(
  runtime: VideoFeedRuntime,
  clearTimer: (timer: number) => void,
): void {
  for (const timer of runtime.scheduledRefreshTimers.values()) {
    clearTimer(timer);
  }
  runtime.scheduledRefreshTimers.clear();
  for (const request of runtime.generationRefreshRequests.values()) {
    request.controller.abort();
  }
  runtime.generationRefreshRequests.clear();
  runtime.terminalHistorySynced.clear();
  runtime.generationRefreshEpochs.clear();
  runtime.pendingHistoryRefreshes.clear();
  runtime.lastRefreshAt.clear();
  runtime.refreshBackoffUntil.clear();
  runtime.refreshFailureCounts.clear();
}
