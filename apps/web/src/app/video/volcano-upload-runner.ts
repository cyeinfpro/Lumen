/* eslint complexity: "off" */

import type { RefObject } from "react";
import {
  createVideoAsset,
  uploadImage,
} from "@/lib/apiClient";
import {
  volcanoAssetErrorMessage,
  volcanoAssetLockKey,
  volcanoAssetStatusKind,
  volcanoOperationAssetResult,
  volcanoOperationIsRetryable,
  volcanoOperationStartedAt,
  volcanoOperationTimedOut,
} from "./volcano-asset-domain";
import {
  abortableDelay,
  CREATE_ASSET_MIN_INTERVAL_MS,
  scanVideoAssets,
} from "./volcano-asset-manager-helpers";
import {
  possibleSubmittedAssets,
  prepareUploadForCreateRetry,
  uploadCreateRetryDecision,
} from "./volcano-asset-manager-state";
import type {
  ActiveSession,
  Notice,
  UploadItem,
} from "./volcano-asset-manager-types";
import type { VolcanoAssetDataController } from "./use-volcano-asset-data";
import type { VolcanoOperationController } from "./use-volcano-operation-controller";
import type { VolcanoUploadQueueController } from "./use-volcano-upload-queue";
import {
  isAbortError,
  uploadReferenceVideo,
} from "./video-request-lifecycle";

interface VolcanoUploadRuntimeDependencies {
  activeSessionRef: RefObject<ActiveSession>;
  isSessionActive: (sessionId: number, expectedModel?: string) => boolean;
  uploadQueue: Pick<
    VolcanoUploadQueueController,
    "uploadControllersRef" | "uploadQueuesRef" | "updateUpload"
  >;
  operationController: Pick<
    VolcanoOperationController,
    "enqueueOperation" | "retryOperation" | "retireOperation" | "getOperation"
  >;
  assetData: Pick<
    VolcanoAssetDataController,
    "refreshAssets" | "refreshGroups" | "refreshProjectAssetTotal"
  >;
  waitForCreateAssetSlot: (signal: AbortSignal) => Promise<void>;
  setNotice: (notice: Notice) => void;
}

export function waitForVolcanoCreateAssetSlot(
  createAssetQueueRef: RefObject<Promise<void>>,
  nextCreateAssetAtRef: RefObject<number>,
  signal: AbortSignal,
): Promise<void> {
  const scheduled = createAssetQueueRef.current.then(async () => {
    const waitMs = Math.max(0, nextCreateAssetAtRef.current - Date.now());
    await abortableDelay(waitMs, signal);
    if (signal.aborted) {
      throw new DOMException("Aborted", "AbortError");
    }
    nextCreateAssetAtRef.current =
      Date.now() + CREATE_ASSET_MIN_INTERVAL_MS;
  });
  createAssetQueueRef.current = scheduled.then(
    () => undefined,
    () => undefined,
  );
  return scheduled;
}

export async function startVolcanoUpload(
  initialItem: UploadItem,
  {
    activeSessionRef,
    isSessionActive,
    uploadQueue,
    operationController,
    assetData,
    waitForCreateAssetSlot,
    setNotice,
  }: VolcanoUploadRuntimeDependencies,
): Promise<void> {
  const {
    uploadControllersRef,
    uploadQueuesRef,
    updateUpload,
  } = uploadQueue;
  const {
    enqueueOperation,
    retryOperation,
    retireOperation,
    getOperation,
  } = operationController;
  const {
    refreshAssets,
    refreshGroups,
    refreshProjectAssetTotal,
  } = assetData;

  if (uploadControllersRef.current.has(initialItem.id)) return;
  const sessionId = activeSessionRef.current.id;
  if (!isSessionActive(sessionId, initialItem.model)) return;

  let item = initialItem;
  if (item.clientOperationId && item.retryMode === "create") {
    const managedOperation = getOperation(
      item.clientOperationId,
      item.model,
    );
    const decision = uploadCreateRetryDecision(item, managedOperation);
    if (decision === "retire_and_recreate") {
      if (!retireOperation(item.clientOperationId)) {
        updateUpload(
          item.id,
          {
            phase: "failed",
            error: "旧创建任务仍在确认结果，不能重新提交 CreateAsset。",
          },
          sessionId,
          item.model,
        );
        return;
      }
      item = prepareUploadForCreateRetry(item);
      updateUpload(item.id, item, sessionId, item.model);
    } else if (decision === "blocked") {
      updateUpload(
        item.id,
        {
          phase: "failed",
          error: "旧创建任务仍在恢复或确认，不能重新提交 CreateAsset。",
        },
        sessionId,
        item.model,
      );
      return;
    }
  }

  if (item.clientOperationId) {
    updateUpload(
      item.id,
      {
        phase: "waiting_quota",
        retryMode: "none",
        error: undefined,
      },
      sessionId,
      item.model,
    );
    retryOperation(item.clientOperationId);
    return;
  }
  if (item.operationId) {
    updateUpload(
      item.id,
      {
        phase: "needs_refresh",
        retryMode: "refresh",
        error:
          "已存在后台任务标识，请先检查状态。系统不会自动重复创建素材。",
      },
      sessionId,
      item.model,
    );
    return;
  }

  const controller = new AbortController();
  uploadControllersRef.current.set(item.id, controller);
  updateUpload(
    item.id,
    {
      phase: "uploading",
      error: undefined,
      pollFailures: 0,
    },
    sessionId,
    item.model,
  );
  try {
    let imageId = item.imageId;
    let videoId = item.videoId;
    if (!imageId && !videoId) {
      if (!item.file) {
        throw new Error("原始文件已释放，且没有可复用的上传 ID");
      }
      if (item.assetType === "Image") {
        const image = await uploadImage(item.file, {
          signal: controller.signal,
          purpose: "volcano_asset",
        });
        imageId = image.id;
        updateUpload(
          item.id,
          { file: null, imageId },
          sessionId,
          item.model,
        );
      } else {
        const video = await uploadReferenceVideo(
          item.file,
          controller.signal,
        );
        videoId = video.id;
        updateUpload(
          item.id,
          { file: null, videoId },
          sessionId,
          item.model,
        );
      }
      if (!isSessionActive(sessionId, item.model)) {
        updateUpload(
          item.id,
          { phase: "queued" },
          sessionId,
          item.model,
        );
        return;
      }
    }

    updateUpload(
      item.id,
      { phase: "waiting_quota" },
      sessionId,
      item.model,
    );
    const queuedItem = (uploadQueuesRef.current.get(item.model) ?? []).find(
      (candidate) => candidate.id === item.id,
    );
    const assetName = (queuedItem?.name ?? item.name).trim();
    if (!assetName) {
      throw new Error("素材名称不能为空");
    }
    let clientOperationId: string | null = null;
    clientOperationId = enqueueOperation(
      {
        action: "create_asset",
        lockKey: volcanoAssetLockKey(
          item.groupId,
          `upload:${item.id}`,
        ),
        title: `创建素材「${assetName}」`,
        pendingLabel: "正在创建并优化素材",
      },
      {
        prepare: waitForCreateAssetSlot,
        submit: (signal) => {
          updateUpload(
            item.id,
            {
              phase: "optimizing",
              submissionStartedAt: Date.now(),
            },
            sessionId,
            item.model,
          );
          return createVideoAsset(
            item.model,
            {
              group_id: item.groupId,
              name: assetName,
              ...(imageId
                ? { image_id: imageId }
                : { video_id: videoId }),
            },
            { signal },
          );
        },
        onProgress: (
          operation,
          operationSessionId,
          operationStartedAt,
        ) => {
          updateUpload(
            item.id,
            {
              clientOperationId: clientOperationId ?? undefined,
              operationId: operation.id,
              operationStatus: operation.status,
              progressStage: operation.progress_stage,
              operationStartedAt,
              operationRetryable:
                volcanoOperationIsRetryable(operation),
              retryAfterSeconds: operation.retry_after_seconds,
              retryAvailableAt: undefined,
              phase: "processing",
              retryMode: "none",
              quotaReserved: true,
              error: undefined,
            },
            operationSessionId,
            item.model,
          );
        },
        onSucceeded: async (
          _result,
          operation,
          operationSessionId,
        ) => {
          const asset = volcanoOperationAssetResult(operation);
          if (!asset) {
            updateUpload(
              item.id,
              {
                phase: "needs_refresh",
                retryMode: "refresh",
                error:
                  "创建任务已完成，但返回结果不是素材。请刷新素材库确认。",
              },
              operationSessionId,
              item.model,
            );
            return;
          }
          const assetKind = volcanoAssetStatusKind(asset.status);
          updateUpload(
            item.id,
            {
              assetId: asset.id,
              file: null,
              operationId: operation.id,
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
            operationSessionId,
            item.model,
          );
          if (isSessionActive(operationSessionId, item.model)) {
            await Promise.all([
              refreshGroups(undefined, true, operationSessionId),
              refreshAssets(true, operationSessionId),
              refreshProjectAssetTotal(true, operationSessionId),
            ]);
          }
        },
        onFailed: (operation, operationSessionId) => {
          const retryable = volcanoOperationIsRetryable(operation);
          const retryAfterSeconds =
            operation.retry_after_seconds ??
            operation.error?.retry_after_seconds ??
            null;
          updateUpload(
            item.id,
            {
              operationId: operation.id,
              operationStatus: operation.status,
              progressStage: operation.progress_stage,
              operationRetryable: retryable,
              retryAfterSeconds,
              retryAvailableAt:
                retryable && retryAfterSeconds
                  ? Date.now() + retryAfterSeconds * 1000
                  : undefined,
              phase: "failed",
              retryMode: retryable ? "operation" : "none",
              quotaReserved: false,
              error: volcanoAssetErrorMessage(
                operation.error,
                "火山素材后台任务失败",
              ),
            },
            operationSessionId,
            item.model,
          );
        },
        onSubmissionFailed: (error, operationSessionId) => {
          updateUpload(
            item.id,
            {
              phase: "failed",
              retryMode: "create",
              quotaReserved: false,
              error: volcanoAssetErrorMessage(
                error,
                "素材创建提交失败",
              ),
            },
            operationSessionId,
            item.model,
          );
        },
        onUncertain: (message, operationSessionId) => {
          updateUpload(
            item.id,
            {
              phase: "needs_refresh",
              retryMode: "refresh",
              quotaReserved: true,
              error: `${message} 系统不会自动重复创建素材。`,
            },
            operationSessionId,
            item.model,
          );
        },
        verifyUnknown: async (signal, operationSessionId) => {
          const result = await scanVideoAssets({
            model: item.model,
            groupIds: [item.groupId],
            name: assetName,
            assetTypes: [item.assetType],
            signal,
          });
          const candidates = possibleSubmittedAssets(result.items, {
            ...item,
            name: assetName,
          });
          if (candidates.length !== 1) {
            if (isSessionActive(operationSessionId, item.model)) {
              await Promise.all([
                refreshGroups(undefined, true, operationSessionId),
                refreshAssets(true, operationSessionId),
                refreshProjectAssetTotal(true, operationSessionId),
              ]);
            }
            return false;
          }
          const asset = candidates[0];
          const assetKind = volcanoAssetStatusKind(asset.status);
          updateUpload(
            item.id,
            {
              assetId: asset.id,
              file: null,
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
            operationSessionId,
            item.model,
          );
          if (isSessionActive(operationSessionId, item.model)) {
            await Promise.all([
              refreshGroups(undefined, true, operationSessionId),
              refreshAssets(true, operationSessionId),
              refreshProjectAssetTotal(true, operationSessionId),
            ]);
          }
          return true;
        },
      },
    );
    if (!clientOperationId) {
      updateUpload(
        item.id,
        {
          phase: "failed",
          retryMode: "create",
          error: "该素材组已有冲突操作，等待完成后可重试上传",
        },
        sessionId,
        item.model,
      );
      return;
    }
    updateUpload(
      item.id,
      {
        clientOperationId,
        phase: "waiting_quota",
        retryMode: "none",
      },
      sessionId,
      item.model,
    );
  } catch (error) {
    if (isAbortError(error) || controller.signal.aborted) {
      updateUpload(
        item.id,
        {
          phase: "queued",
          error: undefined,
        },
        sessionId,
        item.model,
      );
      return;
    }
    const message = volcanoAssetErrorMessage(error, "素材上传失败");
    updateUpload(
      item.id,
      {
        phase: "failed",
        retryMode: "create",
        quotaReserved: false,
        error: message,
      },
      sessionId,
      item.model,
    );
    if (isSessionActive(sessionId, item.model)) {
      setNotice({ tone: "error", text: message });
    }
  } finally {
    if (uploadControllersRef.current.get(item.id) === controller) {
      uploadControllersRef.current.delete(item.id);
    }
  }
}

export async function verifyUntrackedVolcanoUpload(
  item: UploadItem,
  {
    activeSessionRef,
    isSessionActive,
    uploadQueue,
    assetData,
    setNotice,
  }: VolcanoUploadRuntimeDependencies,
): Promise<void> {
  const { uploadControllersRef, updateUpload } = uploadQueue;
  const {
    refreshAssets,
    refreshGroups,
    refreshProjectAssetTotal,
  } = assetData;

  if (uploadControllersRef.current.has(item.id)) return;
  const sessionId = activeSessionRef.current.id;
  if (!isSessionActive(sessionId, item.model)) return;
  const controller = new AbortController();
  uploadControllersRef.current.set(item.id, controller);
  updateUpload(
    item.id,
    {
      error: "正在检查云端素材，期间不会重新提交 CreateAsset。",
    },
    sessionId,
    item.model,
  );
  try {
    const result = await scanVideoAssets({
      model: item.model,
      groupIds: [item.groupId],
      name: item.name.trim() || undefined,
      assetTypes: [item.assetType],
      signal: controller.signal,
    });
    if (controller.signal.aborted) return;
    const candidates = possibleSubmittedAssets(result.items, item);
    if (candidates.length !== 1) {
      const verificationExpired =
        candidates.length === 0 &&
        volcanoOperationTimedOut(item.submissionStartedAt);
      const message = verificationExpired
        ? "超过 20 分钟仍未发现对应云端素材。系统已停止自动恢复；请先刷新素材列表确认，再移除记录并重新选择文件。"
        : candidates.length === 0
          ? "暂未在云端发现可确认的同名素材，后台任务可能仍在排队。请稍后再次检查；系统不会自动重复创建。"
          : "发现多个可能匹配的同名素材，无法安全自动绑定。请在素材列表中确认并删除重复项。";
      updateUpload(
        item.id,
        {
          phase: verificationExpired ? "failed" : "needs_refresh",
          retryMode: verificationExpired ? "none" : "refresh",
          quotaReserved: verificationExpired
            ? false
            : item.quotaReserved,
          error: message,
        },
        sessionId,
        item.model,
      );
      if (isSessionActive(sessionId, item.model)) {
        setNotice({ tone: "error", text: message });
      }
      return;
    }
    const asset = candidates[0];
    const kind = volcanoAssetStatusKind(asset.status);
    updateUpload(
      item.id,
      {
        assetId: asset.id,
        file: null,
        phase:
          kind === "active"
            ? "ready"
            : kind === "failed"
              ? "failed"
              : "processing",
        retryMode: "none",
        operationStartedAt: volcanoOperationStartedAt(
          item.operationStartedAt ?? item.submissionStartedAt,
        ),
        pollFailures: 0,
        error:
          kind === "failed"
            ? asset.error_message || "火山素材处理失败"
            : undefined,
      },
      sessionId,
      item.model,
    );
    if (isSessionActive(sessionId, item.model)) {
      void refreshAssets(true, sessionId);
      void refreshGroups(undefined, true, sessionId);
      void refreshProjectAssetTotal(true, sessionId);
      setNotice({
        tone: "status",
        text: "已找到对应云端素材并恢复状态跟踪",
      });
    }
  } catch (error) {
    if (isAbortError(error) || controller.signal.aborted) return;
    const message = `${volcanoAssetErrorMessage(
      error,
      "云端状态检查失败",
    )}。系统不会自动重复创建素材。`;
    updateUpload(
      item.id,
      {
        phase: "needs_refresh",
        retryMode: "refresh",
        error: message,
      },
      sessionId,
      item.model,
    );
    if (isSessionActive(sessionId, item.model)) {
      setNotice({ tone: "error", text: message });
    }
  } finally {
    if (uploadControllersRef.current.get(item.id) === controller) {
      uploadControllersRef.current.delete(item.id);
    }
  }
}
