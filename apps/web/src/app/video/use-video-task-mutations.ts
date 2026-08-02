"use client";

import { useCallback, useRef } from "react";
import type { MutableRefObject } from "react";
import { useMutation } from "@tanstack/react-query";

import { toast } from "@/components/ui/primitives";
import {
  cancelVideoGeneration,
  createVideoGeneration,
  deleteVideo,
  retryVideoGeneration,
} from "@/lib/apiClient";
import type { VideoAction } from "@/lib/types";
import {
  releaseVideoCreateIdempotencyKey,
  resolveVideoCreateIdempotencyKey,
  type PendingVideoCreateKey,
} from "./video-create-idempotency";
import {
  isVideoRequestFenceCurrent,
  mergeVideoGenerationLists as mergeById,
  nextVideoRequestFence,
} from "@/lib/videoEventSnapshot";
import type { VideoRequestFence } from "@/lib/videoEventSnapshot";

import {
  referencePayloadForVideoAction,
  promptForVideoAction,
} from "./video-reference-domain";
import {
  inputImageForVideoAction,
} from "./video-page-domain";
import {
  parseSeed,
  toVideoResolution,
} from "./video-options-model";
import type { ReferenceDraft } from "./video-workbench-ui";
import type { useVideoGenerationFeed } from "./use-video-generation-feed";

type VideoFeedController = ReturnType<typeof useVideoGenerationFeed>;

type UseVideoTaskMutationsOptions = Pick<
  VideoFeedController,
  | "abortGenerationRefresh"
  | "disableVideoSettling"
  | "effectiveItems"
  | "enableVideoSettling"
  | "invalidateHistory"
  | "scheduleGenerationRefresh"
  | "setIsTaskPanelOpen"
  | "setItems"
  | "setSelectedVideoId"
  | "syncVideoSettling"
  | "terminalHistorySyncedRef"
> & {
  action: VideoAction;
  aspectRatio: string;
  durationS: number;
  generateAudio: boolean;
  inputImageId: string;
  model: string;
  prompt: string;
  referenceMedia: ReferenceDraft[];
  resolution: string;
  retryRequestFenceRef: MutableRefObject<VideoRequestFence>;
  seed: string;
};

export function useVideoTaskMutations({
  abortGenerationRefresh,
  action,
  aspectRatio,
  disableVideoSettling,
  durationS,
  effectiveItems,
  enableVideoSettling,
  generateAudio,
  inputImageId,
  invalidateHistory,
  model,
  prompt,
  referenceMedia,
  resolution,
  retryRequestFenceRef,
  scheduleGenerationRefresh,
  seed,
  setIsTaskPanelOpen,
  setItems,
  setSelectedVideoId,
  syncVideoSettling,
  terminalHistorySyncedRef,
}: UseVideoTaskMutationsOptions) {
  // 幂等键按提交操作持有(useRef 限定在本 mutation 实例生命周期),并与 payload
  // 指纹绑定:同参数重提沿用(服务端回放,避免二次预扣),参数修改自动换新 key
  // (后端指纹不同,旧 key 会 409);成功或服务端明确拒绝后释放。见
  // video-create-idempotency.ts。并发提交互不串扰:决策函数纯且无模块级状态。
  const pendingCreateKeyRef = useRef<PendingVideoCreateKey>(null);
  const createMut = useMutation({
    mutationFn: () => {
      const body = {
        action,
        model,
        prompt: promptForVideoAction(action, prompt, referenceMedia),
        input_image_id: inputImageForVideoAction(action, inputImageId),
        reference_media: referencePayloadForVideoAction(action, referenceMedia),
        duration_s: durationS,
        resolution: toVideoResolution(resolution),
        aspect_ratio: aspectRatio,
        generate_audio: generateAudio,
        seed: parseSeed(seed),
        watermark: false,
      };
      const { key, pending } = resolveVideoCreateIdempotencyKey(
        pendingCreateKeyRef.current,
        body,
        () => crypto.randomUUID(),
      );
      pendingCreateKeyRef.current = pending;
      return createVideoGeneration(body, { idempotency_key: key });
    },
    onSuccess: (generation) => {
      pendingCreateKeyRef.current = null;
      terminalHistorySyncedRef.current.delete(generation.id);
      enableVideoSettling(generation.id);
      syncVideoSettling(generation);
      setItems((previous) => mergeById(previous, [generation]));
      setIsTaskPanelOpen(true);
      toast.success("任务已提交");
      scheduleGenerationRefresh(generation.id, { delayMs: 800 });
      void invalidateHistory();
    },
    onError: (error) => {
      // 服务端明确拒绝(4xx/5xx)释放 key;网络/超时等歧义失败保留供重提回放。
      pendingCreateKeyRef.current = releaseVideoCreateIdempotencyKey(
        pendingCreateKeyRef.current,
        error,
      );
      toast.error("提交失败", {
        description: error instanceof Error ? error.message : undefined,
      });
    },
  });

  const cancelMut = useMutation({
    mutationFn: cancelVideoGeneration,
    onSuccess: (generation, requestedId) => {
      if (generation.id !== requestedId) return;
      setItems((previous) => mergeById(previous, [generation]));
      const providerCannotCancel =
        generation.provider_kind === "dashscope" ||
        generation.provider_kind === "omni_flash" ||
        generation.provider_kind === "volcano_newapi";
      toast.success("已请求取消", {
        description: providerCannotCancel
          ? "该供应商可能无法中止已提交任务，若上游最终成功仍会按结果计费。"
          : undefined,
      });
      scheduleGenerationRefresh(generation.id, { forceHistorySync: true });
    },
    onError: (error) =>
      toast.error("取消失败", {
        description: error instanceof Error ? error.message : undefined,
      }),
  });

  const retryMut = useMutation({
    mutationFn: (request: VideoRequestFence) =>
      retryVideoGeneration(request.taskId),
    onSuccess: (generation, request) => {
      if (!isVideoRequestFenceCurrent(retryRequestFenceRef.current, request)) {
        return;
      }
      terminalHistorySyncedRef.current.delete(generation.id);
      enableVideoSettling(generation.id);
      syncVideoSettling(generation);
      setItems((previous) => mergeById(previous, [generation]));
      setIsTaskPanelOpen(true);
      const createdNewTask = generation.id !== request.taskId;
      toast.success(createdNewTask ? "已创建新的重试任务" : "已重新生成", {
        description: createdNewTask
          ? `正在跟踪新任务 ${generation.id.slice(0, 8)}`
          : undefined,
      });
      scheduleGenerationRefresh(generation.id, { delayMs: 800 });
      void invalidateHistory();
    },
    onError: (error, request) => {
      if (!isVideoRequestFenceCurrent(retryRequestFenceRef.current, request)) {
        return;
      }
      toast.error("重试失败", {
        description: error instanceof Error ? error.message : undefined,
      });
    },
  });

  const requestVideoRetry = useCallback(
    (generationId: string) => {
      const request = nextVideoRequestFence(
        retryRequestFenceRef.current,
        generationId,
      );
      retryRequestFenceRef.current = request;
      retryMut.mutate(request);
    },
    [retryMut, retryRequestFenceRef],
  );

  const deleteMut = useMutation({
    mutationFn: deleteVideo,
    onSuccess: async (_data, videoId) => {
      for (const item of effectiveItems) {
        if (item.video?.id === videoId) {
          disableVideoSettling(item.id);
          abortGenerationRefresh(item.id);
        }
      }
      setItems((previous) =>
        previous.map((item) =>
          item.video?.id === videoId ? { ...item, video: null } : item,
        ),
      );
      setSelectedVideoId((current) => (current === videoId ? "" : current));
      toast.success("视频已删除");
      await invalidateHistory();
    },
    onError: (error) =>
      toast.error("删除失败", {
        description: error instanceof Error ? error.message : undefined,
      }),
  });

  return {
    cancelMut,
    createMut,
    deleteMut,
    requestVideoRetry,
    retryMut,
  };
}
