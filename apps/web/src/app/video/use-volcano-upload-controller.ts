"use client";

/* eslint complexity: "off" */

import {
  useCallback,
  useEffect,
  type RefObject,
} from "react";

import {
  VOLCANO_PROJECT_ASSET_LIMIT,
  validateVolcanoAssetFile,
  volcanoAssetNameFromFile,
  volcanoGroupLockKey,
  volcanoOperationBlocksMutation,
  volcanoOperationStartedAt,
  volcanoQuotaUsage,
  volcanoReservedQuotaCount,
  truncateVolcanoAssetName,
} from "./volcano-asset-domain";
import { clientId } from "./volcano-asset-manager-helpers";
import {
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
  startVolcanoUpload,
  verifyUntrackedVolcanoUpload,
  waitForVolcanoCreateAssetSlot,
} from "./volcano-upload-runner";

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
    (signal: AbortSignal) =>
      waitForVolcanoCreateAssetSlot(
        createAssetQueueRef,
        nextCreateAssetAtRef,
        signal,
      ),
    [createAssetQueueRef, nextCreateAssetAtRef],
  );

  const startUpload = useCallback(
    (initialItem: UploadItem) =>
      startVolcanoUpload(initialItem, {
        activeSessionRef,
        isSessionActive,
        uploadQueue: {
          uploadControllersRef,
          uploadQueuesRef: uploadQueue.uploadQueuesRef,
          updateUpload,
        },
        operationController: {
          enqueueOperation,
          retryOperation,
          retireOperation,
          getOperation,
        },
        assetData: {
          refreshAssets,
          refreshGroups,
          refreshProjectAssetTotal,
        },
        waitForCreateAssetSlot,
        setNotice,
      }),
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
    (item: UploadItem) =>
      verifyUntrackedVolcanoUpload(item, {
        activeSessionRef,
        isSessionActive,
        uploadQueue: {
          uploadControllersRef,
          uploadQueuesRef: uploadQueue.uploadQueuesRef,
          updateUpload,
        },
        operationController: {
          enqueueOperation,
          retryOperation,
          retireOperation,
          getOperation,
        },
        assetData: {
          refreshAssets,
          refreshGroups,
          refreshProjectAssetTotal,
        },
        waitForCreateAssetSlot,
        setNotice,
      }),
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
            operationStartedAt: volcanoOperationStartedAt(
              item.operationStartedAt,
            ),
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
