"use client";

import { useLayoutEffect } from "react";
import type { Dispatch, SetStateAction } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { userScopedQueryKey } from "@/lib/queries/userScope";
import type { VideoGenerationOut } from "@/lib/types";
import {
  isVideoFeedRuntimeCurrent,
  resetVideoFeedRuntime,
} from "./video-feed-scope";
import type { VideoFeedRuntime } from "./video-feed-scope";
import type { ScopedGenerationRefreshRequest } from "./video-generation-feed-config";

export type ScopedVideoItems = {
  userId: string | null;
  value: VideoGenerationOut[];
};

export type ScopedVideoSelection = {
  userId: string | null;
  value: string;
};

/**
 * 账号切换时重置 feed 运行时:清掉旧账号的定时器/刷新请求,并清空本组件
 * 持有的 items/selection/panel 状态。setter 均为 React setState(set* 稳定),
 * 依赖数组无需包含。
 */
export function useVideoFeedRuntimeReset(
  runtime: VideoFeedRuntime<ScopedGenerationRefreshRequest>,
  userId: string | null,
  qc: ReturnType<typeof useQueryClient>,
  setScopedItems: Dispatch<SetStateAction<ScopedVideoItems>>,
  setScopedSelection: Dispatch<SetStateAction<ScopedVideoSelection>>,
  setIsTaskPanelOpen: Dispatch<SetStateAction<boolean>>,
) {
  useLayoutEffect(() => {
    const previousUserId = runtime.userId;
    const changed = resetVideoFeedRuntime(
      runtime,
      userId,
      (timer) => window.clearTimeout(timer),
    );
    if (!changed) return;
    if (previousUserId) {
      void qc.cancelQueries({
        queryKey: userScopedQueryKey(
          previousUserId,
          ["video"] as const,
        ),
      });
    }
    let active = true;
    queueMicrotask(() => {
      if (!active || !isVideoFeedRuntimeCurrent(runtime, userId)) return;
      setScopedItems({ userId, value: [] });
      setScopedSelection({ userId, value: "" });
      setIsTaskPanelOpen(false);
    });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- useState setter 稳定,无需随依赖重建
  }, [qc, runtime, userId]);
}
