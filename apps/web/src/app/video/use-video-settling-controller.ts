"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MutableRefObject } from "react";

import type { VideoGenerationOut } from "@/lib/types";

import type { GenerationRefreshRequest } from "./video-request-lifecycle";
import {
  createVideoSettlingCheckpoint,
  ensureVideoSettlingCheckpoint,
  isActiveVideo,
  isVideoMaterializationPending,
} from "./video-task-model";
import type { VideoSettlingCheckpoint } from "./video-task-model";

export type VideoSettlingController = {
  version: number;
  sync: (item: VideoGenerationOut) => void;
  isActive: (item: VideoGenerationOut, nowMs?: number) => boolean;
  canSchedule: (id: string) => boolean;
  enable: (id: string) => void;
  disable: (id: string) => void;
};

type VideoActivePollingTimerApi = {
  setTimeout: (callback: () => void, delayMs: number) => number;
  clearTimeout: (timer: number) => void;
  setInterval: (callback: () => void, delayMs: number) => number;
  clearInterval: (timer: number) => void;
};

export const VIDEO_ACTIVE_POLL_INITIAL_DELAY_MS = 800;
export const VIDEO_ACTIVE_POLL_INTERVAL_MS = 2500;

export function startVideoActivePolling(
  ids: readonly string[],
  scheduleGenerationRefresh: (id: string) => void,
  timerApi: VideoActivePollingTimerApi = window,
): () => void {
  if (ids.length === 0) return () => {};

  let alive = true;
  const poll = () => {
    if (!alive) return;
    for (const id of ids) scheduleGenerationRefresh(id);
  };
  const initialTimer = timerApi.setTimeout(
    poll,
    VIDEO_ACTIVE_POLL_INITIAL_DELAY_MS,
  );
  const interval = timerApi.setInterval(poll, VIDEO_ACTIVE_POLL_INTERVAL_MS);

  return () => {
    alive = false;
    timerApi.clearTimeout(initialTimer);
    timerApi.clearInterval(interval);
  };
}

export function useVideoSettlingController({
  effectiveItems,
  generationRefreshRequestsRef,
  scheduledRefreshTimersRef,
  pendingHistoryRefreshRef,
}: {
  effectiveItems: VideoGenerationOut[];
  generationRefreshRequestsRef: MutableRefObject<
    Map<string, GenerationRefreshRequest>
  >;
  scheduledRefreshTimersRef: MutableRefObject<Map<string, number>>;
  pendingHistoryRefreshRef: MutableRefObject<Set<string>>;
}): VideoSettlingController {
  const checkpointsRef = useRef<Map<string, VideoSettlingCheckpoint>>(
    new Map(),
  );
  const expiryTimersRef = useRef<Map<string, number>>(new Map());
  const disabledRef = useRef<Set<string>>(new Set());
  const [version, setVersion] = useState(0);

  const expire = useCallback(
    (id: string) => {
      const current = checkpointsRef.current.get(id);
      if (!current || current.phase === "expired") return;
      const expired = ensureVideoSettlingCheckpoint(current, Date.now());
      if (expired.phase !== "expired") return;
      checkpointsRef.current.set(id, expired);
      const expiryTimer = expiryTimersRef.current.get(id);
      if (expiryTimer != null) window.clearTimeout(expiryTimer);
      expiryTimersRef.current.delete(id);
      const scheduledTimer = scheduledRefreshTimersRef.current.get(id);
      if (scheduledTimer != null) window.clearTimeout(scheduledTimer);
      scheduledRefreshTimersRef.current.delete(id);
      generationRefreshRequestsRef.current.get(id)?.controller.abort();
      pendingHistoryRefreshRef.current.delete(id);
      setVersion((value) => value + 1);
    },
    [
      generationRefreshRequestsRef,
      pendingHistoryRefreshRef,
      scheduledRefreshTimersRef,
    ],
  );

  const clear = useCallback((id: string) => {
    const timer = expiryTimersRef.current.get(id);
    if (timer != null) window.clearTimeout(timer);
    expiryTimersRef.current.delete(id);
    if (checkpointsRef.current.delete(id)) {
      setVersion((value) => value + 1);
    }
  }, []);

  const ensure = useCallback(
    (id: string) => {
      const nowMs = Date.now();
      const current = checkpointsRef.current.get(id);
      const checkpoint = ensureVideoSettlingCheckpoint(
        current ?? createVideoSettlingCheckpoint(nowMs),
        nowMs,
      );
      const changed =
        current?.phase !== checkpoint.phase ||
        current?.startedAtMs !== checkpoint.startedAtMs ||
        current?.deadlineAtMs !== checkpoint.deadlineAtMs;
      checkpointsRef.current.set(id, checkpoint);
      if (
        checkpoint.phase === "settling" &&
        !expiryTimersRef.current.has(id)
      ) {
        expiryTimersRef.current.set(
          id,
          window.setTimeout(
            () => expire(id),
            Math.max(0, checkpoint.deadlineAtMs - nowMs),
          ),
        );
      }
      if (changed) setVersion((value) => value + 1);
    },
    [expire],
  );

  const sync = useCallback(
    (item: VideoGenerationOut) => {
      if (
        isVideoMaterializationPending(item) &&
        !disabledRef.current.has(item.id)
      ) {
        ensure(item.id);
      } else {
        clear(item.id);
      }
    },
    [clear, ensure],
  );

  useEffect(() => {
    for (const item of effectiveItems) sync(item);
  }, [effectiveItems, sync]);

  useEffect(
    () => () => {
      for (const timer of expiryTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      expiryTimersRef.current.clear();
      checkpointsRef.current.clear();
      disabledRef.current.clear();
    },
    [],
  );

  const isActive = useCallback(
    (item: VideoGenerationOut, nowMs?: number) =>
      !disabledRef.current.has(item.id) &&
      isActiveVideo(item, checkpointsRef.current.get(item.id), nowMs),
    [],
  );
  const canSchedule = useCallback(
    (id: string) =>
      !disabledRef.current.has(id) &&
      checkpointsRef.current.get(id)?.phase !== "expired",
    [],
  );
  const enable = useCallback((id: string) => {
    disabledRef.current.delete(id);
  }, []);
  const disable = useCallback(
    (id: string) => {
      disabledRef.current.add(id);
      clear(id);
    },
    [clear],
  );

  return useMemo(
    () => ({
      version,
      sync,
      isActive,
      canSchedule,
      enable,
      disable,
    }),
    [canSchedule, disable, enable, isActive, sync, version],
  );
}
