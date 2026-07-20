"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useSSE } from "@/lib/useSSE";
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
import type { GenerationRefreshRequest } from "./video-request-lifecycle";
import {
  filteredVideoHistoryItems,
} from "./video-page-derived-state";
import {
  hasVideo,
  isFailedHistoryVideo,
  isTerminalVideo,
  isVideoMaterializationPending,
} from "./video-task-model";
import type {
  VideoGenerationWithVideo,
  VideoHistoryFilter,
} from "./video-task-model";
import {
  prewarmVideoItem,
} from "./video-task-ui";
import {
  startVideoActivePolling,
  useVideoSettlingController,
} from "./use-video-settling-controller";

const VIDEO_EVENTS = [
  "video.queued",
  "video.submitted",
  "video.progress",
  "video.fetching",
  "video.succeeded",
  "video.failed",
  "video.canceled",
];
const VIDEO_REFRESH_MIN_INTERVAL_MS = 900;
const VIDEO_HISTORY_PAGE_SIZE = 12;

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

export function useVideoGenerationFeed() {
  const qc = useQueryClient();
  const terminalHistorySyncedRef = useRef<Set<string>>(new Set());
  const generationRefreshRequestsRef = useRef<
    Map<string, GenerationRefreshRequest>
  >(new Map());
  const generationRefreshEpochRef = useRef<Map<string, number>>(new Map());
  const scheduledRefreshTimersRef = useRef<Map<string, number>>(new Map());
  const scheduleGenerationRefreshRef = useRef<ScheduleGenerationRefresh>(
    () => {},
  );
  const pendingHistoryRefreshRef = useRef<Set<string>>(new Set());
  const lastRefreshAtRef = useRef<Map<string, number>>(new Map());
  const refreshBackoffUntilRef = useRef<Map<string, number>>(new Map());
  const refreshFailureCountRef = useRef<Map<string, number>>(new Map());
  const [items, setItems] = useState<VideoGenerationOut[]>([]);
  const [selectedVideoId, setSelectedVideoId] = useState("");
  const [historyFilter, setHistoryFilter] = useState<VideoHistoryFilter>("all");
  const [isTaskPanelOpen, setIsTaskPanelOpen] = useState(false);

  useBodyScrollLock(isTaskPanelOpen, {
    bodyOverscrollBehavior: "none",
    documentOverscrollBehavior: "none",
  });

  const optionsQ = useQuery({
    queryKey: ["video", "options"],
    queryFn: ({ signal }) => fetchVideoOptions(signal),
    retry: false,
    staleTime: 60_000,
    gcTime: 5 * 60_000,
  });
  const historyQ = useInfiniteQuery({
    queryKey: ["video", "generations"],
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
    retry: false,
    staleTime: 20_000,
    gcTime: 5 * 60_000,
  });
  const historyItems = useMemo(
    () => historyQ.data?.pages.flatMap((page) => page.items) ?? [],
    [historyQ.data?.pages],
  );
  const effectiveItems = useMemo(
    () => mergeById(historyItems, items),
    [historyItems, items],
  );
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
    () => activeItems.map((item) => `task:${item.id}`),
    [activeItems],
  );
  const activeItemIdsKey = useMemo(
    () => activeItems.map((item) => item.id).join("|"),
    [activeItems],
  );

  useEffect(() => {
    prewarmVideoItem(playbackVideoItem);
  }, [playbackVideoItem]);

  const invalidateHistory = useCallback(
    () => qc.invalidateQueries({ queryKey: ["video", "generations"] }),
    [qc],
  );

  const refreshGeneration = useCallback(
    async (
      id: string,
      request: GenerationRefreshRequest,
      opts: GenerationRefreshOptions = {},
    ): Promise<boolean> => {
      const next = await fetchVideoGeneration(id, request.controller.signal);
      if (
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
        if (terminal) terminalHistorySyncedRef.current.add(id);
      }
      return true;
    },
    [invalidateHistory, syncVideoSettling],
  );

  const refreshGenerationSafe = useCallback(
    async (id: string, opts: GenerationRefreshOptions = {}) => {
      if (opts.forceHistorySync) {
        pendingHistoryRefreshRef.current.add(id);
      }
      const existing = generationRefreshRequestsRef.current.get(id);
      if (existing && !opts.forceHistorySync) return;
      existing?.controller.abort();

      const forceHistorySync =
        opts.forceHistorySync || pendingHistoryRefreshRef.current.has(id);
      pendingHistoryRefreshRef.current.delete(id);
      const request: GenerationRefreshRequest = {
        controller: new AbortController(),
        epoch: (generationRefreshEpochRef.current.get(id) ?? 0) + 1,
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
    [refreshGeneration],
  );

  const abortGenerationRefresh = useCallback((id: string) => {
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
  }, []);

  const scheduleGenerationRefresh = useCallback(
    (id: string, opts: GenerationRefreshScheduleOptions = {}) => {
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

      const timer = window.setTimeout(() => {
        scheduledRefreshTimersRef.current.delete(id);
        if (!canScheduleVideoRefresh(id)) return;
        lastRefreshAtRef.current.set(id, Date.now());
        const forceHistorySync = pendingHistoryRefreshRef.current.has(id);
        pendingHistoryRefreshRef.current.delete(id);
        void refreshGenerationSafe(id, { forceHistorySync });
      }, delayMs);
      scheduledRefreshTimersRef.current.set(id, timer);
    },
    [canScheduleVideoRefresh, refreshGenerationSafe],
  );

  useEffect(() => {
    scheduleGenerationRefreshRef.current = scheduleGenerationRefresh;
    return () => {
      scheduleGenerationRefreshRef.current = () => {};
    };
  }, [scheduleGenerationRefresh]);

  const applyVideoEventSnapshot = useCallback(
    (data: unknown): { id: string; terminal: boolean } | null => {
      const id = videoGenerationEventId(data);
      if (!id) return null;
      setItems((prev) =>
        prev.map((item) =>
          item.id === id ? mergeVideoGenerationEvent(item, data) : item,
        ),
      );
      return { id, terminal: isTerminalVideoEvent(data) };
    },
    [],
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

  useEffect(
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
      void invalidateHistory();
      const ids = activeItemIdsKey.split("|").filter(Boolean);
      for (const id of ids) scheduleGenerationRefresh(id);
    };

    window.addEventListener("focus", refreshVisibleTasks);
    document.addEventListener("visibilitychange", refreshVisibleTasks);
    return () => {
      window.removeEventListener("focus", refreshVisibleTasks);
      document.removeEventListener("visibilitychange", refreshVisibleTasks);
    };
  }, [activeItemIdsKey, invalidateHistory, scheduleGenerationRefresh]);

  useEffect(
    () => () => {
      for (const timer of scheduledRefreshTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      scheduledRefreshTimersRef.current.clear();
      for (const request of generationRefreshRequestsRef.current.values()) {
        request.controller.abort();
      }
      generationRefreshRequestsRef.current.clear();
    },
    [],
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
