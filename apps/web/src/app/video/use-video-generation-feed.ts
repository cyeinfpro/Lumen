"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { Dispatch, SetStateAction } from "react";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useSSE } from "@/features/realtime";
import {
  isTerminalVideoEvent,
  mergeVideoGenerationEvent,
  mergeVideoGenerationLists as mergeById,
  videoGenerationEventId,
} from "@/lib/videoEventSnapshot";
import type { VideoGenerationOut } from "@/lib/types";

import {
  fetchVideoGeneration,
  fetchVideoGenerations,
  fetchVideoOptions,
  generationRefreshRequestIsCurrent,
  isAbortError,
  recordGenerationRefreshFailure,
} from "./video-request-lifecycle";
import { filteredVideoHistoryItems } from "./video-page-derived-state";
import {
  hasVideo,
  isFailedHistoryVideo,
  isTerminalVideo,
  isVideoMaterializationPending,
  type VideoGenerationWithVideo,
  type VideoHistoryFilter,
} from "./video-task-model";
import { prewarmVideoItem } from "./video-task-ui";
import {
  startVideoActivePolling,
  useVideoSettlingController,
} from "./use-video-settling-controller";
import {
  createVideoFeedRuntime,
  disposeVideoFeedRuntime,
  isVideoFeedRuntimeCurrent,
  isVideoFeedScopeTokenCurrent,
  normalizeVideoFeedUserId,
  videoFeedChannels,
  videoFeedScopeToken,
} from "./video-feed-scope";
import type { VideoFeedRuntime } from "./video-feed-scope";
import {
  type ScopedVideoItems,
  type ScopedVideoSelection,
  useVideoFeedRuntimeReset,
} from "./use-video-feed-runtime-reset";
import { userScopedQueryKey, useUserQueryScope } from "@/lib/queries/userScope";

import {
  VIDEO_EVENTS,
  VIDEO_HISTORY_PAGE_SIZE,
  VIDEO_HISTORY_STALE_MS,
  VIDEO_REFRESH_MIN_INTERVAL_MS,
  type GenerationRefreshOptions,
  type GenerationRefreshScheduleOptions,
  type ScheduleGenerationRefresh,
  type ScopedGenerationRefreshRequest,
} from "./video-generation-feed-config";

export type {
  GenerationRefreshOptions,
  GenerationRefreshScheduleOptions,
  ScheduleGenerationRefresh,
} from "./video-generation-feed-config";

export function useVideoGenerationFeed() {
  const qc = useQueryClient();
  const userScope = useUserQueryScope();
  const userId = normalizeVideoFeedUserId(userScope.userId);
  const [runtime] = useState<
    VideoFeedRuntime<ScopedGenerationRefreshRequest>
  >(() => createVideoFeedRuntime<ScopedGenerationRefreshRequest>(userId));
  const terminalHistorySyncedRef = useRef(runtime.terminalHistorySynced);
  const generationRefreshRequestsRef = useRef(
    runtime.generationRefreshRequests,
  );
  const generationRefreshEpochRef = useRef(runtime.generationRefreshEpochs);
  const scheduledRefreshTimersRef = useRef(
    runtime.scheduledRefreshTimers,
  );
  const scheduleGenerationRefreshRef = useRef<ScheduleGenerationRefresh>(
    () => {},
  );
  const pendingHistoryRefreshRef = useRef(
    runtime.pendingHistoryRefreshes,
  );
  const lastRefreshAtRef = useRef(runtime.lastRefreshAt);
  const refreshBackoffUntilRef = useRef(runtime.refreshBackoffUntil);
  const refreshFailureCountRef = useRef(runtime.refreshFailureCounts);
  const [scopedItems, setScopedItems] = useState<ScopedVideoItems>({
    userId,
    value: [],
  });
  const [scopedSelection, setScopedSelection] =
    useState<ScopedVideoSelection>({
      userId,
      value: "",
    });
  const [historyFilter, setHistoryFilter] = useState<VideoHistoryFilter>("all");
  const [isTaskPanelOpen, setIsTaskPanelOpen] = useState(false);
  const scopeReady = isVideoFeedRuntimeCurrent(runtime, userId);
  const items = useMemo(
    () =>
      scopeReady && scopedItems.userId === userId ? scopedItems.value : [],
    [scopeReady, scopedItems, userId],
  );
  const selectedVideoId =
    scopeReady && scopedSelection.userId === userId
      ? scopedSelection.value
      : "";
  const setItems = useCallback<Dispatch<SetStateAction<VideoGenerationOut[]>>>(
    (action) => {
      if (!isVideoFeedRuntimeCurrent(runtime, userId)) return;
      setScopedItems((current) => {
        if (!isVideoFeedRuntimeCurrent(runtime, userId)) return current;
        const currentValue =
          current.userId === userId ? current.value : [];
        const value =
          typeof action === "function"
            ? action(currentValue)
            : action;
        return { userId, value };
      });
    },
    [runtime, setScopedItems, userId],
  );
  const setSelectedVideoId = useCallback<Dispatch<SetStateAction<string>>>(
    (action) => {
      if (!isVideoFeedRuntimeCurrent(runtime, userId)) return;
      setScopedSelection((current) => {
        if (!isVideoFeedRuntimeCurrent(runtime, userId)) return current;
        const currentValue =
          current.userId === userId ? current.value : "";
        const value =
          typeof action === "function"
            ? action(currentValue)
            : action;
        return { userId, value };
      });
    },
    [runtime, setScopedSelection, userId],
  );

  useBodyScrollLock(isTaskPanelOpen, {
    bodyOverscrollBehavior: "none",
    documentOverscrollBehavior: "none",
  });

  const optionsQueryKey = useMemo(
    () => userScopedQueryKey(userId, ["video", "options"] as const),
    [userId],
  );
  const historyQueryKey = useMemo(
    () => userScopedQueryKey(userId, ["video", "generations"] as const),
    [userId],
  );
  const optionsQ = useQuery({
    queryKey: optionsQueryKey,
    queryFn: ({ signal }) => fetchVideoOptions(signal),
    enabled: userScope.enabled && scopeReady,
    retry: false,
    staleTime: 60_000,
    gcTime: 5 * 60_000,
  });
  const historyQ = useInfiniteQuery({
    queryKey: historyQueryKey,
    queryFn: ({ pageParam, signal }) =>
      fetchVideoGenerations(
        {
          cursor: pageParam,
          limit: VIDEO_HISTORY_PAGE_SIZE,
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled: userScope.enabled && scopeReady,
    retry: false,
    staleTime: VIDEO_HISTORY_STALE_MS,
    gcTime: 5 * 60_000,
  });
  const historyItems = useMemo(
    () =>
      scopeReady
        ? historyQ.data?.pages.flatMap((page) => page.items) ?? []
        : [],
    [historyQ.data?.pages, scopeReady],
  );
  const effectiveItems = useMemo(
    () => mergeById(historyItems, items),
    [historyItems, items],
  );
  // 整理窗口超时后的恢复重查入口。通过 ref 间接调用 refreshGenerationSafe:
  // 该函数定义在本 hook 之后,且恢复重查需要绕过 canSchedule 门禁,
  // 否则过期任务永远无法再被重查。
  const settlingRecoveryRefreshRef = useRef<(id: string) => void>(() => {});
  const handleSettlingExpired = useCallback((id: string) => {
    settlingRecoveryRefreshRef.current(id);
  }, []);
  const {
    version: videoSettlingVersion,
    sync: syncVideoSettling,
    isActive: isVideoSettlingActive,
    canSchedule: canScheduleVideoRefresh,
    enable: enableVideoSettling,
    disable: disableVideoSettling,
  } = useVideoSettlingController({
    effectiveItems,
    generationRefreshRequestsRef,
    scheduledRefreshTimersRef,
    pendingHistoryRefreshRef,
    scopeKey: `${userId ?? "anonymous"}:${runtime.generation}`,
    onExpired: handleSettlingExpired,
  });

  const activeItems = useMemo(() => {
    void videoSettlingVersion;
    return effectiveItems
      .filter((item) => isVideoSettlingActive(item))
      .map((item) =>
        isVideoMaterializationPending(item)
          ? { ...item, progress_stage: "fetching" as const }
          : item,
      );
  }, [effectiveItems, isVideoSettlingActive, videoSettlingVersion]);
  const completedVideoItems = useMemo(
    () => effectiveItems.filter(hasVideo),
    [effectiveItems],
  );
  const playbackVideoItem = useMemo(
    () =>
      selectedVideoId
        ? completedVideoItems.find((item) => item.video.id === selectedVideoId)
        : undefined,
    [completedVideoItems, selectedVideoId],
  );
  const settledHistoryItems = useMemo(() => {
    void videoSettlingVersion;
    return effectiveItems.filter((item) => !isVideoSettlingActive(item));
  }, [effectiveItems, isVideoSettlingActive, videoSettlingVersion]);
  const succeededHistoryItems = useMemo(
    () => settledHistoryItems.filter((item) => item.status === "succeeded"),
    [settledHistoryItems],
  );
  const failedHistoryItems = useMemo(
    () => settledHistoryItems.filter(isFailedHistoryVideo),
    [settledHistoryItems],
  );
  const filteredHistoryItems = useMemo(
    () =>
      filteredVideoHistoryItems(
        historyFilter,
        settledHistoryItems,
        succeededHistoryItems,
        failedHistoryItems,
      ),
    [
      failedHistoryItems,
      historyFilter,
      settledHistoryItems,
      succeededHistoryItems,
    ],
  );
  const channels = useMemo(
    () => videoFeedChannels(runtime, userId, activeItems),
    [activeItems, runtime, userId],
  );
  const activeItemIdsKey = useMemo(
    () => activeItems.map((item) => item.id).join("|"),
    [activeItems],
  );

  useEffect(() => {
    prewarmVideoItem(playbackVideoItem);
  }, [playbackVideoItem]);

  const invalidateHistory = useCallback(
    () => qc.invalidateQueries({ queryKey: historyQueryKey }),
    [historyQueryKey, qc],
  );

  const refreshGeneration = useCallback(
    async (
      id: string,
      request: ScopedGenerationRefreshRequest,
      opts: GenerationRefreshOptions = {},
    ): Promise<boolean> => {
      const next = await fetchVideoGeneration(id, request.controller.signal);
      if (
        !isVideoFeedScopeTokenCurrent(runtime, request.scope) ||
        !generationRefreshRequestIsCurrent(
          request,
          generationRefreshRequestsRef.current.get(id),
          generationRefreshEpochRef.current.get(id),
        ) ||
        next.id !== id
      ) {
        return false;
      }
      syncVideoSettling(next);
      setItems((prev) => mergeById(prev, [next]));
      if (next.video) {
        prewarmVideoItem(next as VideoGenerationWithVideo);
      }

      const terminal = isTerminalVideo(next);
      if (!terminal) {
        terminalHistorySyncedRef.current.delete(id);
      }
      if (
        opts.forceHistorySync ||
        (terminal && !terminalHistorySyncedRef.current.has(id))
      ) {
        await invalidateHistory();
        if (!isVideoFeedScopeTokenCurrent(runtime, request.scope)) {
          return false;
        }
        if (terminal) terminalHistorySyncedRef.current.add(id);
      }
      return true;
    },
    [invalidateHistory, runtime, setItems, syncVideoSettling],
  );

  const refreshGenerationSafe = useCallback(
    async (id: string, opts: GenerationRefreshOptions = {}) => {
      if (!isVideoFeedRuntimeCurrent(runtime, userId)) return;
      if (opts.forceHistorySync) {
        pendingHistoryRefreshRef.current.add(id);
      }
      const existing = generationRefreshRequestsRef.current.get(id);
      if (existing && !opts.forceHistorySync) return;
      existing?.controller.abort();

      const forceHistorySync =
        opts.forceHistorySync || pendingHistoryRefreshRef.current.has(id);
      pendingHistoryRefreshRef.current.delete(id);
      const request: ScopedGenerationRefreshRequest = {
        controller: new AbortController(),
        epoch: (generationRefreshEpochRef.current.get(id) ?? 0) + 1,
        scope: videoFeedScopeToken(runtime),
      };
      generationRefreshEpochRef.current.set(id, request.epoch);
      generationRefreshRequestsRef.current.set(id, request);

      try {
        const committed = await refreshGeneration(id, request, {
          forceHistorySync,
        });
        if (!committed) return;
        refreshFailureCountRef.current.delete(id);
        refreshBackoffUntilRef.current.delete(id);
      } catch (err) {
        if (
          isAbortError(err) ||
          !generationRefreshRequestIsCurrent(
            request,
            generationRefreshRequestsRef.current.get(id),
            generationRefreshEpochRef.current.get(id),
          )
        ) {
          return;
        }
        recordGenerationRefreshFailure(
          id,
          err,
          refreshFailureCountRef.current,
          refreshBackoffUntilRef.current,
        );
        if (forceHistorySync) {
          pendingHistoryRefreshRef.current.add(id);
        }
        scheduleGenerationRefreshRef.current(id, { forceHistorySync });
      } finally {
        if (generationRefreshRequestsRef.current.get(id) === request) {
          generationRefreshRequestsRef.current.delete(id);
        }
      }
    },
    [refreshGeneration, runtime, userId],
  );

  useEffect(() => {
    // 整理窗口超时后的恢复重查:绕过 canSchedule 门禁直接重查任务状态,
    // 视频一旦落盘,syncVideoSettling 会结束整理窗口并展示视频。
    settlingRecoveryRefreshRef.current = (id: string) => {
      void refreshGenerationSafe(id, { forceHistorySync: true });
    };
    return () => {
      settlingRecoveryRefreshRef.current = () => {};
    };
  }, [refreshGenerationSafe]);

  const abortGenerationRefresh = useCallback(
    (id: string) => {
      if (!isVideoFeedRuntimeCurrent(runtime, userId)) return;
      const request = generationRefreshRequestsRef.current.get(id);
      request?.controller.abort();
      generationRefreshRequestsRef.current.delete(id);
      generationRefreshEpochRef.current.set(
        id,
        (generationRefreshEpochRef.current.get(id) ?? 0) + 1,
      );
      const timer = scheduledRefreshTimersRef.current.get(id);
      if (timer != null) window.clearTimeout(timer);
      scheduledRefreshTimersRef.current.delete(id);
      pendingHistoryRefreshRef.current.delete(id);
    },
    [runtime, userId],
  );

  const scheduleGenerationRefresh = useCallback(
    (id: string, opts: GenerationRefreshScheduleOptions = {}) => {
      if (!isVideoFeedRuntimeCurrent(runtime, userId)) return;
      if (!id || !canScheduleVideoRefresh(id)) return;
      if (opts.forceHistorySync) {
        pendingHistoryRefreshRef.current.add(id);
      }
      if (scheduledRefreshTimersRef.current.has(id)) return;

      const now = Date.now();
      const lastRefreshAt = lastRefreshAtRef.current.get(id) ?? 0;
      const minIntervalDelay = Math.max(
        0,
        VIDEO_REFRESH_MIN_INTERVAL_MS - (now - lastRefreshAt),
      );
      const backoffDelay = Math.max(
        0,
        (refreshBackoffUntilRef.current.get(id) ?? 0) - now,
      );
      const delayMs = Math.max(
        opts.delayMs ?? 0,
        minIntervalDelay,
        backoffDelay,
      );
      const scope = videoFeedScopeToken(runtime);

      const timer = window.setTimeout(() => {
        scheduledRefreshTimersRef.current.delete(id);
        if (!isVideoFeedScopeTokenCurrent(runtime, scope)) return;
        if (!canScheduleVideoRefresh(id)) return;
        lastRefreshAtRef.current.set(id, Date.now());
        const forceHistorySync = pendingHistoryRefreshRef.current.has(id);
        pendingHistoryRefreshRef.current.delete(id);
        void refreshGenerationSafe(id, { forceHistorySync });
      }, delayMs);
      scheduledRefreshTimersRef.current.set(id, timer);
    },
    [
      canScheduleVideoRefresh,
      refreshGenerationSafe,
      runtime,
      userId,
    ],
  );

  useEffect(() => {
    scheduleGenerationRefreshRef.current = scheduleGenerationRefresh;
    return () => {
      scheduleGenerationRefreshRef.current = () => {};
    };
  }, [scheduleGenerationRefresh]);

  const applyVideoEventSnapshot = useCallback(
    (data: unknown): { id: string; terminal: boolean } | null => {
      if (!isVideoFeedRuntimeCurrent(runtime, userId)) return null;
      const id = videoGenerationEventId(data);
      if (!id) return null;
      setItems((prev) =>
        prev.map((item) =>
          item.id === id ? mergeVideoGenerationEvent(item, data) : item,
        ),
      );
      return { id, terminal: isTerminalVideoEvent(data) };
    },
    [runtime, setItems, userId],
  );
  const handlers = useMemo(
    () =>
      Object.fromEntries(
        VIDEO_EVENTS.map((eventName) => [
          eventName,
          (data: unknown) => {
            const snapshot = applyVideoEventSnapshot(data);
            if (snapshot) {
              scheduleGenerationRefresh(snapshot.id, {
                forceHistorySync: snapshot.terminal,
              });
            }
          },
        ]),
      ),
    [applyVideoEventSnapshot, scheduleGenerationRefresh],
  );
  useSSE(channels, handlers);

  useLayoutEffect(
    () =>
      startVideoActivePolling(
        activeItemIdsKey.split("|").filter(Boolean),
        scheduleGenerationRefresh,
      ),
    [activeItemIdsKey, scheduleGenerationRefresh],
  );

  useEffect(() => {
    const refreshVisibleTasks = () => {
      if (document.visibilityState !== "visible") return;
      // 聚焦/可见性恢复只刷新陈旧数据：历史列表仍在 stale 窗口内则跳过全量
      // refetch，避免每次切回标签页都重复拉取整份列表（全局 refetchOnWindowFocus 为 false）。
      const historyUpdatedAt = qc.getQueryState(historyQueryKey)?.dataUpdatedAt;
      if (
        historyUpdatedAt === undefined ||
        Date.now() - historyUpdatedAt >= VIDEO_HISTORY_STALE_MS
      ) {
        void invalidateHistory();
      }
      const ids = activeItemIdsKey.split("|").filter(Boolean);
      for (const id of ids) scheduleGenerationRefresh(id);
    };

    window.addEventListener("focus", refreshVisibleTasks);
    document.addEventListener("visibilitychange", refreshVisibleTasks);
    return () => {
      window.removeEventListener("focus", refreshVisibleTasks);
      document.removeEventListener("visibilitychange", refreshVisibleTasks);
    };
  }, [
    activeItemIdsKey,
    historyQueryKey,
    invalidateHistory,
    qc,
    scheduleGenerationRefresh,
  ]);

  useEffect(
    () => () => {
      disposeVideoFeedRuntime(runtime, (timer) => window.clearTimeout(timer));
    },
    [runtime],
  );

  useVideoFeedRuntimeReset(
    runtime,
    userId,
    qc,
    setScopedItems,
    setScopedSelection,
    setIsTaskPanelOpen,
  );

  return {
    abortGenerationRefresh,
    activeItems,
    disableVideoSettling,
    effectiveItems,
    enableVideoSettling,
    failedHistoryItems,
    filteredHistoryItems,
    historyFilter,
    historyQ,
    invalidateHistory,
    isTaskPanelOpen,
    options: optionsQ.data,
    optionsQ,
    playbackVideoItem,
    scheduleGenerationRefresh,
    selectedVideoId,
    setHistoryFilter,
    setIsTaskPanelOpen,
    setItems,
    setSelectedVideoId,
    settledHistoryItems,
    succeededHistoryItems,
    syncVideoSettling,
    terminalHistorySyncedRef,
  };
}
