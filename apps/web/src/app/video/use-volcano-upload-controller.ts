"use client";

/* eslint complexity: "off" */

import {
  useCallback,
  useEffect,
  type RefObject,
} from "react";

import {
  createVideoAsset,
  uploadImage,
} from "@/lib/apiClient";

import {
  VOLCANO_PROJECT_ASSET_LIMIT,
  validateVolcanoAssetFile,
  volcanoAssetErrorMessage,
  volcanoAssetLockKey,
  volcanoAssetNameFromFile,
  volcanoAssetStatusKind,
  volcanoGroupLockKey,
  volcanoOperationAssetResult,
  volcanoOperationBlocksMutation,
  volcanoOperationIsRetryable,
  volcanoOperationTimedOut,
  volcanoQuotaUsage,
  volcanoReservedQuotaCount,
  truncateVolcanoAssetName,
} from "./volcano-asset-domain";
import {
  abortableDelay,
  clientId,
  CREATE_ASSET_MIN_INTERVAL_MS,
  scanVideoAssets,
} from "./volcano-asset-manager-helpers";
import {
  possibleSubmittedAssets,
  prepareUploadForCreateRetry,
  uploadCanBeRemoved,
  uploadCreateRetryDecision,
  uploadNameIsEditable,
} from "./volcano-asset-manager-state";
import type {
  ActiveSession,
  Notice,
  OperationItem,
  UploadItem,
} from "./volcano-asset-manager-types";
import { MAX_UPLOAD_CONCURRENCY } from "./volcano-asset-manager-types";
import type { VolcanoAssetDataController } from "./use-volcano-asset-data";
import type { VolcanoOperationController } from "./use-volcano-operation-controller";
import type { VolcanoUploadQueueController } from "./use-volcano-upload-queue";
import { useVolcanoUploadPolling } from "./use-volcano-upload-polling";
import {
  isAbortError,
  uploadReferenceVideo,
} from "./video-request-lifecycle";

export type VolcanoUploadController = {
  pendingAssetCreates: number;
  enqueueFiles: (files: File[]) => void;
  removeUpload: (id: string) => void;
  renameUpload: (id: string, name: string) => void;
  retryUpload: (id: string) => void;
};

export function useVolcanoUploadController({
  open,
  model,
  selectedGroupId,
  projectAssetTotal,
  pendingOperationsByLock,
  activeSessionRef,
  isSessionActive,
  uploadQueue,
  operationController,
  assetData,
  setNotice,
}: {
  open: boolean;
  model: string;
  selectedGroupId: string | null;
  projectAssetTotal: number | null;
  pendingOperationsByLock: ReadonlyMap<string, OperationItem>;
  activeSessionRef: RefObject<ActiveSession>;
  isSessionActive: (sessionId: number, expectedModel?: string) => boolean;
  uploadQueue: VolcanoUploadQueueController;
  operationController: VolcanoOperationController;
  assetData: Pick<
    VolcanoAssetDataController,
    | "assets"
    | "refreshAssets"
    | "refreshGroups"
    | "refreshProjectAssetTotal"
  >;
  setNotice: (notice: Notice) => void;
}): VolcanoUploadController {
  const {
    uploads,
    uploadNamesRef,
    uploadControllersRef,
    createAssetQueueRef,
    nextCreateAssetAtRef,
    commitUploadQueue,
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
  const pendingAssetCreates = volcanoReservedQuotaCount(uploads);

  const waitForCreateAssetSlot = useCallback(
    function schedule(signal: AbortSignal): Promise<void> {
      const scheduled = createAssetQueueRef.current.then(async () => {
        const waitMs = Math.max(
          0,
          nextCreateAssetAtRef.current - Date.now(),
        );
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
    },
    [createAssetQueueRef, nextCreateAssetAtRef],
  );

  const startUpload = useCallback(
    async (initialItem: UploadItem) => {
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
                error:
                  "旧创建任务仍在确认结果，不能重新提交 CreateAsset。",
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
              error:
                "旧创建任务仍在恢复或确认，不能重新提交 CreateAsset。",
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
        const queuedItem = (uploadQueue.uploadQueuesRef.current.get(
          item.model,
        ) ?? []).find((candidate) => candidate.id === item.id);
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
            onProgress: (operation, operationSessionId) => {
              updateUpload(
                item.id,
                {
                  clientOperationId: clientOperationId ?? undefined,
                  operationId: operation.id,
                  operationStatus: operation.status,
                  progressStage: operation.progress_stage,
                  operationStartedAt:
                    queuedItem?.operationStartedAt ?? Date.now(),
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
              if (
                isSessionActive(operationSessionId, item.model)
              ) {
                await Promise.all([
                  refreshGroups(
                    undefined,
                    true,
                    operationSessionId,
                  ),
                  refreshAssets(true, operationSessionId),
                  refreshProjectAssetTotal(
                    true,
                    operationSessionId,
                  ),
                ]);
              }
            },
            onFailed: (operation, operationSessionId) => {
              const retryable =
                volcanoOperationIsRetryable(operation);
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
              const candidates = possibleSubmittedAssets(
                result.items,
                {
                  ...item,
                  name: assetName,
                },
              );
              if (candidates.length !== 1) {
                if (
                  isSessionActive(operationSessionId, item.model)
                ) {
                  await Promise.all([
                    refreshGroups(
                      undefined,
                      true,
                      operationSessionId,
                    ),
                    refreshAssets(true, operationSessionId),
                    refreshProjectAssetTotal(
                      true,
                      operationSessionId,
                    ),
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
              if (
                isSessionActive(operationSessionId, item.model)
              ) {
                await Promise.all([
                  refreshGroups(
                    undefined,
                    true,
                    operationSessionId,
                  ),
                  refreshAssets(true, operationSessionId),
                  refreshProjectAssetTotal(
                    true,
                    operationSessionId,
                  ),
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
              error:
                "该素材组已有冲突操作，等待完成后可重试上传",
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
        const message = volcanoAssetErrorMessage(
          error,
          "素材上传失败",
        );
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
        if (
          uploadControllersRef.current.get(item.id) === controller
        ) {
          uploadControllersRef.current.delete(item.id);
        }
      }
    },
    [
      activeSessionRef,
      enqueueOperation,
      getOperation,
      isSessionActive,
      refreshAssets,
      refreshGroups,
      refreshProjectAssetTotal,
      retireOperation,
      retryOperation,
      setNotice,
      updateUpload,
      uploadControllersRef,
      uploadQueue.uploadQueuesRef,
      waitForCreateAssetSlot,
    ],
  );

  useEffect(() => {
    if (!open) return;
    const slots =
      MAX_UPLOAD_CONCURRENCY - uploadControllersRef.current.size;
    if (slots <= 0) return;
    uploads
      .filter((item) => item.phase === "queued")
      .slice(0, slots)
      .forEach((item) => void startUpload(item));
  }, [open, startUpload, uploadControllersRef, uploads]);

  useVolcanoUploadPolling({
    open,
    model,
    assets: assetData.assets,
    activeSessionRef,
    isSessionActive,
    uploadQueue,
    assetData,
  });

  const enqueueFiles = useCallback(
    (files: File[]) => {
      if (!selectedGroupId) {
        setNotice({
          tone: "error",
          text: "先选择或新建 AIGC 素材组",
        });
        return;
      }
      if (
        pendingOperationsByLock.has(
          volcanoGroupLockKey(selectedGroupId),
        )
      ) {
        setNotice({
          tone: "error",
          text: "该素材组有后台操作进行中，暂不能加入新上传",
        });
        return;
      }
      if (projectAssetTotal == null) {
        setNotice({
          tone: "error",
          text: "素材总配额读取中，暂不能加入新上传",
        });
        return;
      }
      const candidates: UploadItem[] = [];
      const errors: string[] = [];
      const existingUploadKeys = new Set(
        uploads.map(
          (item) =>
            `${item.groupId}\u0000${item.fileName}\u0000${item.fileSize}\u0000${item.fileLastModified}`,
        ),
      );
      for (const file of files) {
        const uploadKey = `${selectedGroupId}\u0000${file.name}\u0000${file.size}\u0000${file.lastModified}`;
        if (existingUploadKeys.has(uploadKey)) {
          errors.push(`${file.name}：已在当前上传列表中`);
          continue;
        }
        const validation = validateVolcanoAssetFile(file);
        if (!validation.ok) {
          errors.push(`${file.name}：${validation.error}`);
          continue;
        }
        candidates.push({
          id: clientId(),
          model,
          groupId: selectedGroupId,
          file,
          fileName: file.name,
          fileSize: file.size,
          fileLastModified: file.lastModified,
          assetType: validation.assetType,
          name: volcanoAssetNameFromFile(file.name),
          phase: "queued",
          retryMode: "create",
          quotaReserved: true,
          quotaReservationTarget: 0,
        });
        existingUploadKeys.add(uploadKey);
      }
      const availableSlots = Math.max(
        0,
        VOLCANO_PROJECT_ASSET_LIMIT -
          projectAssetTotal -
          pendingAssetCreates,
      );
      const accepted = candidates
        .slice(0, availableSlots)
        .map((item, index) => ({
          ...item,
          quotaReservationTarget:
            projectAssetTotal + pendingAssetCreates + index + 1,
        }));
      const quotaRejected = candidates.length - accepted.length;
      if (quotaRejected > 0) {
        errors.push(
          `素材总配额最多 ${VOLCANO_PROJECT_ASSET_LIMIT} 个，当前已用 ${projectAssetTotal} 个、队列预留 ${pendingAssetCreates} 个，另有 ${quotaRejected} 个文件未加入`,
        );
      }
      if (accepted.length > 0) {
        for (const item of accepted) {
          uploadNamesRef.current.set(item.id, item.name);
        }
        commitUploadQueue(model, (current) => [
          ...current,
          ...accepted,
        ]);
        setNotice({
          tone: "status",
          text: `已加入 ${accepted.length} 个文件，上传后自动优化为火山规格`,
        });
      }
      if (errors.length > 0) {
        setNotice({
          tone: "error",
          text:
            errors.length === 1
              ? errors[0]
              : `${errors[0]}；另有 ${errors.length - 1} 个文件未加入`,
        });
      }
    },
    [
      commitUploadQueue,
      model,
      pendingAssetCreates,
      pendingOperationsByLock,
      projectAssetTotal,
      selectedGroupId,
      setNotice,
      uploadNamesRef,
      uploads,
    ],
  );

  const removeUpload = useCallback(
    (id: string) => {
      const item = uploads.find((candidate) => candidate.id === id);
      if (!item) return;
      const managedOperation = item.clientOperationId
        ? getOperation(item.clientOperationId, item.model)
        : undefined;
      if (
        managedOperation &&
        volcanoOperationBlocksMutation(managedOperation)
      ) {
        setNotice({
          tone: "error",
          text:
            "该素材关联的云端操作尚未结束，恢复或确认结果后才能移除记录",
        });
        return;
      }
      if (!uploadCanBeRemoved(item)) {
        setNotice({
          tone: "error",
          text:
            "该素材仍在上传、排队、后台处理或结果确认中，暂不能移除记录",
        });
        return;
      }
      uploadControllersRef.current.get(id)?.abort();
      uploadControllersRef.current.delete(id);
      uploadNamesRef.current.delete(id);
      commitUploadQueue(item.model, (current) =>
        current.filter((candidate) => candidate.id !== id),
      );
    },
    [
      commitUploadQueue,
      getOperation,
      setNotice,
      uploadControllersRef,
      uploadNamesRef,
      uploads,
    ],
  );

  const renameUpload = useCallback(
    (id: string, name: string) => {
      const item = uploads.find((candidate) => candidate.id === id);
      if (!item) return;
      const managedOperation = item.clientOperationId
        ? getOperation(item.clientOperationId, item.model)
        : undefined;
      if (
        !uploadNameIsEditable(item) ||
        (managedOperation &&
          volcanoOperationBlocksMutation(managedOperation))
      ) {
        setNotice({
          tone: "error",
          text: "该素材已进入上传或云端操作阶段，名称已锁定",
        });
        return;
      }
      const nextName = truncateVolcanoAssetName(name);
      uploadNamesRef.current.set(id, nextName);
      updateUpload(id, { name: nextName }, undefined, item.model);
    },
    [
      getOperation,
      setNotice,
      updateUpload,
      uploadNamesRef,
      uploads,
    ],
  );

  const verifyUntrackedUpload = useCallback(
    async (item: UploadItem) => {
      if (uploadControllersRef.current.has(item.id)) return;
      const sessionId = activeSessionRef.current.id;
      if (!isSessionActive(sessionId, item.model)) return;
      const controller = new AbortController();
      uploadControllersRef.current.set(item.id, controller);
      updateUpload(
        item.id,
        {
          error:
            "正在检查云端素材，期间不会重新提交 CreateAsset。",
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
        const candidates = possibleSubmittedAssets(
          result.items,
          item,
        );
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
              phase: verificationExpired
                ? "failed"
                : "needs_refresh",
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
            operationStartedAt:
              item.operationStartedAt ??
              item.submissionStartedAt ??
              Date.now(),
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
        if (isAbortError(error) || controller.signal.aborted) {
          return;
        }
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
        if (
          uploadControllersRef.current.get(item.id) === controller
        ) {
          uploadControllersRef.current.delete(item.id);
        }
      }
    },
    [
      activeSessionRef,
      isSessionActive,
      refreshAssets,
      refreshGroups,
      refreshProjectAssetTotal,
      setNotice,
      updateUpload,
      uploadControllersRef,
    ],
  );

  const retryUpload = useCallback(
    (id: string) => {
      const original = uploads.find(
        (candidate) => candidate.id === id,
      );
      if (!original) return;
      if (
        pendingOperationsByLock.has(
          volcanoGroupLockKey(original.groupId),
        )
      ) {
        setNotice({
          tone: "error",
          text: "该素材组有后台操作进行中，暂不能重试",
        });
        return;
      }

      const managedOperation = original.clientOperationId
        ? getOperation(original.clientOperationId, original.model)
        : undefined;
      const createDecision = uploadCreateRetryDecision(
        original,
        managedOperation,
      );
      let item = original;
      if (createDecision === "retire_and_recreate") {
        if (
          original.clientOperationId &&
          !retireOperation(original.clientOperationId)
        ) {
          setNotice({
            tone: "error",
            text:
              "旧创建任务仍在确认结果，不能重新提交 CreateAsset。",
          });
          return;
        }
        item = prepareUploadForCreateRetry(original);
      } else if (createDecision === "blocked") {
        setNotice({
          tone: "error",
          text:
            "旧创建任务仍在恢复或确认，不能重新提交 CreateAsset。",
        });
        return;
      }

      if (
        item.clientOperationId &&
        managedOperation &&
        (managedOperation.recovery === "resume" ||
          managedOperation.recovery === "refresh") &&
        (item.retryMode === "operation" ||
          item.retryMode === "refresh")
      ) {
        retryOperation(item.clientOperationId);
        return;
      }
      if (item.retryMode === "none") {
        setNotice({
          tone: "error",
          text:
            "该失败不能直接重试，请刷新素材库或移除后重新选择文件",
        });
        return;
      }
      if (item.retryMode === "refresh") {
        if (!item.operationId && !item.assetId) {
          void verifyUntrackedUpload(item);
          return;
        }
        updateUpload(
          id,
          {
            phase: "processing",
            operationStartedAt: Date.now(),
            pollFailures: 0,
            error: undefined,
          },
          undefined,
          item.model,
        );
        return;
      }
      if (
        item.retryAvailableAt != null &&
        item.retryAvailableAt > Date.now()
      ) {
        const seconds = Math.max(
          1,
          Math.ceil(
            (item.retryAvailableAt - Date.now()) / 1000,
          ),
        );
        setNotice({
          tone: "error",
          text: `火山限流仍在冷却，请 ${seconds} 秒后重试`,
        });
        return;
      }
      if (!item.name.trim()) {
        setNotice({
          tone: "error",
          text: "素材名称不能为空",
        });
        return;
      }
      if (projectAssetTotal == null) {
        setNotice({
          tone: "error",
          text: "素材总配额读取中，暂不能重试",
        });
        return;
      }
      const otherPending = volcanoReservedQuotaCount(
        uploads.filter((candidate) => candidate.id !== id),
      );
      const quota = volcanoQuotaUsage(
        projectAssetTotal + otherPending,
        VOLCANO_PROJECT_ASSET_LIMIT,
      );
      if (quota.reached) {
        setNotice({
          tone: "error",
          text:
            "当前 Project 素材总配额已满，删除云端素材后再重试",
        });
        return;
      }
      updateUpload(
        id,
        {
          ...item,
          phase: "queued",
          assetId: undefined,
          operationStartedAt:
            item.retryMode === "operation"
              ? item.operationStartedAt
              : undefined,
          operationRetryable: false,
          retryAfterSeconds: null,
          retryAvailableAt: undefined,
          quotaReserved: true,
          quotaReservationTarget:
            projectAssetTotal + otherPending + 1,
          error: undefined,
        },
        undefined,
        item.model,
      );
    },
    [
      getOperation,
      pendingOperationsByLock,
      projectAssetTotal,
      retireOperation,
      retryOperation,
      setNotice,
      updateUpload,
      uploads,
      verifyUntrackedUpload,
    ],
  );

  return {
    pendingAssetCreates,
    enqueueFiles,
    removeUpload,
    renameUpload,
    retryUpload,
  };
}
