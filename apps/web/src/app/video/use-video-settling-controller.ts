"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { MutableRefObject } from "react";

import type { VideoGenerationOut } from "@/lib/types";

import type { GenerationRefreshRequest } from "./video-request-lifecycle";
import {
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
// 整理窗口超时后恢复重查的节奏:快速轮询停止后,任务仍要继续跟进,
// 避免 succeeded 但 video 为空的任务永久卡在"整理中"。
export const VIDEO_SETTLING_RECOVERY_INTERVAL_MS = 30_000;

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
  scopeKey,
  onExpired,
}: {
  effectiveItems: VideoGenerationOut[];
  generationRefreshRequestsRef: MutableRefObject<
    Map<string, GenerationRefreshRequest>
  >;
  scheduledRefreshTimersRef: MutableRefObject<Map<string, number>>;
  pendingHistoryRefreshRef: MutableRefObject<Set<string>>;
  scopeKey: string;
  onExpired: (id: string) => void;
}): VideoSettlingController {
  const checkpointsRef = useRef<Map<string, VideoSettlingCheckpoint>>(
    new Map(),
  );
  const expiryTimersRef = useRef<Map<string, number>>(new Map());
  const recoveryIntervalsRef = useRef<Map<string, number>>(new Map());
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
      // 整理窗口超时:主动把任务交还给 feed 重查状态,并以较慢节奏持续
      // 恢复重查,直到视频落盘或任务不再处于素材化等待——succeeded 但
      // video 为空的任务不能永久停留在"整理中"。窗口被 ensure 重新开启
      // 期间该定时器只空转,到期后会再次触发 onExpired。
      onExpired(id);
      if (!recoveryIntervalsRef.current.has(id)) {
        recoveryIntervalsRef.current.set(
          id,
          window.setInterval(() => {
            const current = checkpointsRef.current.get(id);
            if (!current || current.phase !== "expired") return;
            onExpired(id);
          }, VIDEO_SETTLING_RECOVERY_INTERVAL_MS),
        );
      }
    },
    [
      generationRefreshRequestsRef,
      onExpired,
      pendingHistoryRefreshRef,
      scheduledRefreshTimersRef,
    ],
  );

  const clear = useCallback((id: string) => {
    const timer = expiryTimersRef.current.get(id);
    if (timer != null) window.clearTimeout(timer);
    expiryTimersRef.current.delete(id);
    const recoveryInterval = recoveryIntervalsRef.current.get(id);
    if (recoveryInterval != null) window.clearInterval(recoveryInterval);
    recoveryIntervalsRef.current.delete(id);
    if (checkpointsRef.current.delete(id)) {
      setVersion((value) => value + 1);
    }
  }, []);

  const ensure = useCallback(
    (id: string) => {
      const nowMs = Date.now();
      const current = checkpointsRef.current.get(id);
      // 已过期的 checkpoint 意味着上一个整理窗口结束时视频仍未落盘:
      // 重新开启窗口,让恢复重查(以及任何一次 sync)继续跟进任务,
      // 而不是把任务永久留在"整理中"。
      const checkpoint = ensureVideoSettlingCheckpoint(
        current?.phase === "expired" ? undefined : current,
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

  useLayoutEffect(() => {
    let active = true;
    for (const timer of expiryTimersRef.current.values()) {
      window.clearTimeout(timer);
    }
    for (const interval of recoveryIntervalsRef.current.values()) {
      window.clearInterval(interval);
    }
    expiryTimersRef.current.clear();
    recoveryIntervalsRef.current.clear();
    checkpointsRef.current.clear();
    disabledRef.current.clear();
    queueMicrotask(() => {
      if (active) setVersion((value) => value + 1);
    });
    return () => {
      active = false;
    };
  }, [scopeKey]);

  useEffect(() => {
    for (const item of effectiveItems) sync(item);
  }, [effectiveItems, sync]);

  useEffect(
    () => () => {
      for (const timer of expiryTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      for (const interval of recoveryIntervalsRef.current.values()) {
        window.clearInterval(interval);
      }
      expiryTimersRef.current.clear();
      recoveryIntervalsRef.current.clear();
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
