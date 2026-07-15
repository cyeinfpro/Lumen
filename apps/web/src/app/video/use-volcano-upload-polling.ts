"use client";

/* eslint complexity: "off" */

import {
  useEffect,
  useMemo,
  type RefObject,
} from "react";

import {
  getVideoAsset,
  getVideoAssetOperation,
} from "@/lib/apiClient";
import type { VideoAssetOut } from "@/lib/types";

import {
  VOLCANO_OPERATION_POLL_TIMEOUT_MS,
  volcanoAssetErrorMessage,
  volcanoAssetStatusKind,
  volcanoOperationAssetResult,
  volcanoOperationStatusKind,
  volcanoOperationTimedOut,
} from "./volcano-asset-domain";
import type { ActiveSession } from "./volcano-asset-manager-types";
import { POLL_INTERVAL_MS } from "./volcano-asset-manager-types";
import type { VolcanoAssetDataController } from "./use-volcano-asset-data";
import type { VolcanoUploadQueueController } from "./use-volcano-upload-queue";
import { isAbortError } from "./video-request-lifecycle";

export function useVolcanoUploadPolling({
  open,
  model,
  assets,
  activeSessionRef,
  isSessionActive,
  uploadQueue,
  assetData,
}: {
  open: boolean;
  model: string;
  assets: VideoAssetOut[];
  activeSessionRef: RefObject<ActiveSession>;
  isSessionActive: (sessionId: number, expectedModel?: string) => boolean;
  uploadQueue: Pick<
    VolcanoUploadQueueController,
    "uploads" | "uploadsRef" | "pollAbortRef" | "updateUpload"
  >;
  assetData: Pick<
    VolcanoAssetDataController,
    "refreshAssets" | "refreshGroups" | "refreshProjectAssetTotal"
  >;
}): void {
  const { uploads, uploadsRef, pollAbortRef, updateUpload } = uploadQueue;
  const {
    refreshAssets,
    refreshGroups,
    refreshProjectAssetTotal,
  } = assetData;
  const processingUploadKey = useMemo(
    () =>
      uploads
        .filter(
          (item) =>
            item.phase === "processing" &&
            Boolean(
              item.assetId ||
                (item.operationId && !item.clientOperationId),
            ),
        )
        .map(
          (item) =>
            `${item.id}:${item.operationId ?? ""}:${item.assetId ?? ""}`,
        )
        .join("|"),
    [uploads],
  );
  const hasProcessingAssets = assets.some(
    (item) => volcanoAssetStatusKind(item.status) === "processing",
  );

  useEffect(() => {
    if (!open || (!hasProcessingAssets && !processingUploadKey)) {
      return;
    }
    const sessionId = activeSessionRef.current.id;
    const controller = new AbortController();
    pollAbortRef.current?.abort();
    pollAbortRef.current = controller;
    let timer: number | null = null;
    const poll = async (): Promise<void> => {
      if (
        controller.signal.aborted ||
        !isSessionActive(sessionId, model)
      ) {
        return;
      }
      if (hasProcessingAssets) {
        await refreshAssets(true, sessionId);
      }
      const pollItems = uploadsRef.current.filter(
        (item) =>
          item.phase === "processing" &&
          Boolean(
            item.assetId ||
              (item.operationId && !item.clientOperationId),
          ),
      );
      const results = await Promise.all(
        pollItems.map(async (item) => {
          try {
            if (
              item.operationId &&
              !item.assetId &&
              !item.clientOperationId
            ) {
              let operation = await getVideoAssetOperation(
                item.operationId,
                {
                  signal: controller.signal,
                },
              );
              if (
                volcanoOperationStatusKind(operation.status) ===
                  "succeeded" &&
                !operation.result
              ) {
                const asset = await getVideoAsset(
                  operation.id,
                  item.model,
                  {
                    signal: controller.signal,
                  },
                );
                operation = { ...operation, result: asset };
              }
              return { item, operation, error: null };
            }
            if (item.assetId) {
              const asset = await getVideoAsset(
                item.assetId,
                item.model,
                {
                  signal: controller.signal,
                },
              );
              return { item, asset, error: null };
            }
            return {
              item,
              error: new Error("缺少后台任务标识"),
            };
          } catch (error) {
            return { item, error };
          }
        }),
      );
      if (
        controller.signal.aborted ||
        !isSessionActive(sessionId, model)
      ) {
        return;
      }
      let shouldRefreshTotals = false;
      for (const result of results) {
        const { item } = result;
        if (result.error) {
          if (isAbortError(result.error)) continue;
          const failures = (item.pollFailures ?? 0) + 1;
          const exhausted = failures >= 3;
          updateUpload(
            item.id,
            {
              pollFailures: failures,
              phase: exhausted ? "needs_refresh" : "processing",
              retryMode: exhausted ? "refresh" : item.retryMode,
              error: exhausted
                ? `${volcanoAssetErrorMessage(
                    result.error,
                    "后台状态刷新失败",
                  )}。请点“检查状态”后继续，系统不会重复创建素材。`
                : `状态刷新暂时失败，将自动重试（${failures}/3）`,
            },
            sessionId,
            item.model,
          );
          continue;
        }
        if ("operation" in result && result.operation) {
          const operation = result.operation;
          const operationKind = volcanoOperationStatusKind(
            operation.status,
          );
          if (operationKind === "pending") {
            if (
              volcanoOperationTimedOut(
                item.operationStartedAt,
                Date.now(),
                VOLCANO_OPERATION_POLL_TIMEOUT_MS,
              )
            ) {
              updateUpload(
                item.id,
                {
                  phase: "needs_refresh",
                  retryMode: "refresh",
                  operationStatus: operation.status,
                  progressStage: operation.progress_stage,
                  operationRetryable: operation.retryable,
                  retryAfterSeconds:
                    operation.retry_after_seconds,
                  error:
                    "后台处理时间较长，已暂停自动轮询。点“检查状态”可继续确认，不会重新创建素材。",
                },
                sessionId,
                item.model,
              );
            } else {
              updateUpload(
                item.id,
                {
                  phase: "processing",
                  retryMode: "none",
                  operationStatus: operation.status,
                  progressStage: operation.progress_stage,
                  operationRetryable: operation.retryable,
                  retryAfterSeconds:
                    operation.retry_after_seconds,
                  pollFailures: 0,
                  error: undefined,
                },
                sessionId,
                item.model,
              );
            }
            continue;
          }
          if (operationKind === "failed") {
            const retryable =
              operation.retryable ||
              Boolean(operation.error?.retryable);
            updateUpload(
              item.id,
              {
                phase: "failed",
                operationStatus: operation.status,
                progressStage: operation.progress_stage,
                operationRetryable: retryable,
                retryAfterSeconds:
                  operation.retry_after_seconds ??
                  operation.error?.retry_after_seconds,
                retryAvailableAt:
                  Date.now() +
                  Math.max(
                    0,
                    operation.retry_after_seconds ??
                      operation.error?.retry_after_seconds ??
                      0,
                  ) *
                    1000,
                retryMode: retryable ? "operation" : "none",
                quotaReserved: false,
                pollFailures: 0,
                error: volcanoAssetErrorMessage(
                  operation.error,
                  "火山素材后台任务失败",
                ),
              },
              sessionId,
              item.model,
            );
            shouldRefreshTotals = true;
            continue;
          }
          const asset = volcanoOperationAssetResult(operation);
          if (operationKind !== "succeeded" || !asset) {
            updateUpload(
              item.id,
              {
                phase: "needs_refresh",
                retryMode: "refresh",
                operationStatus: operation.status,
                progressStage: operation.progress_stage,
                error:
                  "后台返回了未知任务状态，请检查状态后再继续。",
              },
              sessionId,
              item.model,
            );
            continue;
          }
          const assetKind = volcanoAssetStatusKind(asset.status);
          updateUpload(
            item.id,
            {
              assetId: asset.id,
              file: null,
              operationStatus: operation.status,
              progressStage: operation.progress_stage,
              operationRetryable: false,
              retryAfterSeconds: null,
              retryAvailableAt: undefined,
              pollFailures: 0,
              phase:
                assetKind === "active"
                  ? "ready"
                  : assetKind === "failed"
                    ? "failed"
                    : "processing",
              retryMode: "none",
              error:
                assetKind === "failed"
                  ? asset.error_message || "火山素材处理失败"
                  : undefined,
            },
            sessionId,
            item.model,
          );
          shouldRefreshTotals = true;
          continue;
        }
        if ("asset" in result && result.asset) {
          const asset = result.asset;
          const assetKind = volcanoAssetStatusKind(asset.status);
          if (
            assetKind === "processing" &&
            volcanoOperationTimedOut(item.operationStartedAt)
          ) {
            updateUpload(
              item.id,
              {
                phase: "needs_refresh",
                retryMode: "refresh",
                pollFailures: 0,
                error:
                  "火山处理时间较长，已暂停自动轮询。点“检查状态”可继续确认。",
              },
              sessionId,
              item.model,
            );
            continue;
          }
          updateUpload(
            item.id,
            {
              phase:
                assetKind === "active"
                  ? "ready"
                  : assetKind === "failed"
                    ? "failed"
                    : "processing",
              retryMode: "none",
              pollFailures: 0,
              error:
                assetKind === "failed"
                  ? asset.error_message || "火山素材处理失败"
                  : undefined,
            },
            sessionId,
            item.model,
          );
          if (assetKind !== "processing") {
            shouldRefreshTotals = true;
          }
        }
      }
      if (shouldRefreshTotals) {
        void refreshGroups(undefined, true, sessionId);
        void refreshAssets(true, sessionId);
        void refreshProjectAssetTotal(true, sessionId);
      }
      if (
        !controller.signal.aborted &&
        isSessionActive(sessionId, model)
      ) {
        timer = window.setTimeout(
          () => void poll(),
          POLL_INTERVAL_MS,
        );
      }
    };
    void poll();
    return () => {
      if (timer != null) window.clearTimeout(timer);
      controller.abort();
      if (pollAbortRef.current === controller) {
        pollAbortRef.current = null;
      }
    };
  }, [
    activeSessionRef,
    hasProcessingAssets,
    isSessionActive,
    model,
    open,
    pollAbortRef,
    processingUploadKey,
    refreshAssets,
    refreshGroups,
    refreshProjectAssetTotal,
    updateUpload,
    uploadsRef,
  ]);
}
