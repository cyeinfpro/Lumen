"use client";

/* eslint complexity: "off" */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import {
  createVideoAssetGroup,
  deleteVideoAsset,
  deleteVideoAssetGroup,
  listVideoAssetGroups,
  patchVideoAsset,
  patchVideoAssetGroup,
} from "@/lib/apiClient";
import type { VideoAssetOut } from "@/lib/types";

import {
  VOLCANO_PROJECT_ASSET_LIMIT,
  VOLCANO_PROJECT_GROUP_LIMIT,
  toggleVolcanoAssetSelection,
  volcanoAssetLockKey,
  volcanoAssetSelectionIssueMessage,
  volcanoDeletedAssetIds,
  volcanoGroupCreateLockKey,
  volcanoGroupLockKey,
  volcanoOperationAssetResult,
  volcanoOperationBlocksMutation,
  volcanoOperationDeleteResult,
  volcanoOperationGroupResult,
  volcanoQuotaUsage,
  volcanoUniqueNewGroupMatch,
} from "./volcano-asset-domain";
import {
  allGroupAssetIds,
  fullAssetSet,
  scanVideoAssets,
} from "./volcano-asset-manager-helpers";
import {
  assetSelection,
  uploadBlocksGroupMutation,
} from "./volcano-asset-manager-state";
import { useVolcanoAssetManagerController } from "./volcano-asset-manager-controller";
import { VolcanoAssetManagerPresentation } from "./volcano-asset-manager-presentation";
import type {
  DeleteTarget,
  GroupFormState,
  Notice,
  VolcanoAssetManagerProps,
  VolcanoAssetSelection,
} from "./volcano-asset-manager-types";
import {
  ASSET_PAGE_SIZE,
  GROUP_PAGE_SIZE,
} from "./volcano-asset-manager-types";
import { useVolcanoUploadController } from "./use-volcano-upload-controller";

export type {
  VolcanoAssetManagerProps,
  VolcanoAssetSelection,
} from "./volcano-asset-manager-types";

export function VolcanoAssetManager({
  open,
  model,
  remainingLimits,
  existingAssetIds,
  onClose,
  onUse,
  onDeleted,
}: VolcanoAssetManagerProps) {
  const titleId = useId();
  const descriptionId = useId();
  const uploadInputId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [groupSearch, setGroupSearch] = useState("");
  const [groupForm, setGroupForm] = useState<GroupFormState | null>(null);
  const [groupFormError, setGroupFormError] = useState<string | null>(null);
  const [selected, setSelected] = useState<VolcanoAssetSelection[]>([]);
  const [renameAsset, setRenameAsset] = useState<{
    asset: VideoAssetOut;
    name: string;
  } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  useBodyScrollLock(open);
  const onDialogKeyDown = useModalLayer({
    open,
    rootRef: dialogRef,
    onClose,
    initialFocusRef: closeButtonRef,
  });

  const resetSessionUi = useCallback(() => {
    setSelected([]);
    setNotice(null);
    setDeleteTarget(null);
    setGroupForm(null);
    setGroupFormError(null);
    setRenameAsset(null);
    setGroupSearch("");
    setDragActive(false);
  }, []);
  const {
    activeSessionRef,
    isSessionActive,
    uploadQueue,
    operationController,
    assetData,
  } = useVolcanoAssetManagerController({
    open,
    model,
    resetSessionUi,
    setNotice,
  });
  const {
    assetPage: requestedAssetPage,
    assetSearch: requestedAssetSearch,
    capability,
    refreshAssets: refreshRequestedAssets,
    selectedGroupId: requestedGroupId,
    setAssetPage,
    statusFilter: requestedStatusFilter,
    typeFilter: requestedTypeFilter,
  } = assetData;

  useEffect(() => {
    if (!open || !capability?.ready || !requestedGroupId) {
      return;
    }
    const sessionId = activeSessionRef.current.id;
    const timer = window.setTimeout(
      () => void refreshRequestedAssets(false, sessionId),
      0,
    );
    return () => window.clearTimeout(timer);
  }, [
    activeSessionRef,
    capability?.ready,
    open,
    refreshRequestedAssets,
    requestedAssetPage,
    requestedAssetSearch,
    requestedGroupId,
    requestedStatusFilter,
    requestedTypeFilter,
  ]);

  const pendingOperationsByLock = useMemo(
    () =>
      new Map(
        operationController.operations
          .filter(volcanoOperationBlocksMutation)
          .map((operation) => [operation.lockKey, operation]),
      ),
    [operationController.operations],
  );
  const uploadController = useVolcanoUploadController({
    open,
    model,
    selectedGroupId: assetData.selectedGroupId,
    projectAssetTotal: assetData.projectAssetTotal,
    pendingOperationsByLock,
    activeSessionRef,
    isSessionActive,
    uploadQueue,
    operationController,
    assetData,
    setNotice,
  });

  const existingIds = useMemo(
    () => new Set(Array.from(existingAssetIds)),
    [existingAssetIds],
  );
  const notifyDeletedReferences = useCallback(
    (assetIds: Iterable<string>) => {
      const deletedIds = Array.from(
        new Set(
          Array.from(assetIds)
            .map((assetId) => assetId.trim())
            .filter(Boolean),
        ),
      );
      if (deletedIds.length > 0) onDeleted(deletedIds);
    },
    [onDeleted],
  );
  const selectedGroup = assetData.groups.find(
    (group) => group.id === assetData.selectedGroupId,
  );
  const selectedImageCount = selected.filter(
    (item) => item.asset_type === "Image",
  ).length;
  const selectedVideoCount = selected.filter(
    (item) => item.asset_type === "Video",
  ).length;
  const groupQuota =
    assetData.projectGroupTotal == null
      ? null
      : volcanoQuotaUsage(
          assetData.projectGroupTotal,
          VOLCANO_PROJECT_GROUP_LIMIT,
        );
  const projectAssetQuota =
    assetData.projectAssetTotal == null
      ? null
      : volcanoQuotaUsage(
          assetData.projectAssetTotal,
          VOLCANO_PROJECT_ASSET_LIMIT,
        );
  const effectiveAssetQuota =
    assetData.projectAssetTotal == null
      ? null
      : volcanoQuotaUsage(
          assetData.projectAssetTotal + uploadController.pendingAssetCreates,
          VOLCANO_PROJECT_ASSET_LIMIT,
        );
  const blockedUploadIds = useMemo(() => {
    const blockedOperationIds = new Set(
      operationController.operations
        .filter(volcanoOperationBlocksMutation)
        .map((operation) => operation.id),
    );
    return new Set(
      uploadQueue.uploads
        .filter(
          (item) =>
            item.clientOperationId &&
            blockedOperationIds.has(item.clientOperationId),
        )
        .map((item) => item.id),
    );
  }, [operationController.operations, uploadQueue.uploads]);
  const selectedGroupOperation = assetData.selectedGroupId
    ? pendingOperationsByLock.get(
        volcanoGroupLockKey(assetData.selectedGroupId),
      )
    : undefined;
  const selectedGroupDeleting = Boolean(selectedGroupOperation?.blocksChildren);
  const uploadDisabledReason = selectedGroupOperation
    ? selectedGroupDeleting
      ? "该素材组正在删除，暂不能上传"
      : "该素材组有后台操作进行中，暂不能上传"
    : assetData.projectAssetTotal == null
      ? "正在读取当前 Project 的素材总配额"
      : projectAssetQuota?.reached
        ? `当前 Project 已有 ${projectAssetQuota.used} 个素材，已达到 ${projectAssetQuota.limit} 个上限`
        : effectiveAssetQuota?.reached
          ? `剩余 ${projectAssetQuota?.remaining ?? 0} 个名额已由上传队列占用`
          : null;
  const groupCreateDisabledReason =
    assetData.projectGroupTotal == null
      ? "正在读取当前 Project 的素材组总配额"
      : groupQuota?.reached
        ? `当前 Project 已有 ${groupQuota.used} 个素材组，已达到 ${groupQuota.limit} 个上限`
        : pendingOperationsByLock.has(volcanoGroupCreateLockKey())
          ? "已有素材组创建任务进行中"
          : null;
  const filteredGroups = assetData.groups.filter((group) => {
    const query = groupSearch.trim().toLowerCase();
    return (
      !query ||
      group.name.toLowerCase().includes(query) ||
      group.description.toLowerCase().includes(query)
    );
  });
  const loadedAssets = fullAssetSet(
    assetData.assets,
    assetData.selectedGroupId,
    assetData.assetsGroupId,
  );
  const totalAssetPages = Math.max(
    1,
    Math.ceil(assetData.assetTotal / ASSET_PAGE_SIZE),
  );

  useEffect(() => {
    if (requestedAssetPage > totalAssetPages) {
      setAssetPage(totalAssetPages);
    }
  }, [requestedAssetPage, setAssetPage, totalAssetPages]);

  const saveGroup = () => {
    if (!groupForm) return;
    const form = groupForm;
    const name = form.name.trim();
    const description = form.description.trim();
    const createGroupBaselineIds = new Set<string>();
    if (!name) {
      setGroupFormError("名称不能为空");
      return;
    }
    if (form.mode === "create" && !groupQuota) {
      setGroupFormError("素材组总配额读取中");
      return;
    }
    if (form.mode === "create" && groupQuota?.reached) {
      setGroupFormError(
        `素材组总配额最多 ${groupQuota.limit} 个，当前已达到上限`,
      );
      return;
    }
    if (
      form.mode === "rename" &&
      uploadQueue.uploadsRef.current.some(
        (item) =>
          item.groupId === form.groupId && uploadBlocksGroupMutation(item),
      )
    ) {
      setGroupFormError("该素材组仍有上传或后台创建任务，请等待完成后再编辑");
      return;
    }
    const lockKey =
      form.mode === "create"
        ? volcanoGroupCreateLockKey()
        : volcanoGroupLockKey(form.groupId || "");
    setGroupFormError(null);
    const operationId = operationController.enqueueOperation(
      {
        action: form.mode === "create" ? "create_group" : "update_group",
        lockKey,
        title:
          form.mode === "create"
            ? `新建素材组「${name}」`
            : `更新素材组「${name}」`,
        pendingLabel:
          form.mode === "create" ? "正在创建素材组" : "正在更新素材组",
      },
      {
        prepare:
          form.mode === "create"
            ? async (signal) => {
                const result = await listVideoAssetGroups({
                  model,
                  page_number: 1,
                  page_size: GROUP_PAGE_SIZE,
                  signal,
                });
                createGroupBaselineIds.clear();
                for (const group of result.items) {
                  if (
                    group.name.trim() === name &&
                    group.description.trim() === description
                  ) {
                    createGroupBaselineIds.add(group.id);
                  }
                }
              }
            : undefined,
        submit: (signal) =>
          form.mode === "create"
            ? createVideoAssetGroup(model, { name, description }, { signal })
            : patchVideoAssetGroup(
                form.groupId || "",
                model,
                { name, description },
                { signal },
              ),
        onSucceeded: async (_result, operation, sessionId) => {
          const group = volcanoOperationGroupResult(operation);
          if (group) {
            assetData.setGroups((current) => {
              const exists = current.some((item) => item.id === group.id);
              return exists
                ? current.map((item) => (item.id === group.id ? group : item))
                : [group, ...current];
            });
            assetData.setSelectedGroupId(group.id);
          }
          await Promise.all([
            assetData.refreshGroups(group?.id ?? form.groupId, true, sessionId),
            assetData.refreshProjectAssetTotal(true, sessionId),
          ]);
        },
        verifyUnknown: async (signal, sessionId) => {
          const result = await listVideoAssetGroups({
            model,
            page_number: 1,
            page_size: GROUP_PAGE_SIZE,
            signal,
          });
          if (!isSessionActive(sessionId, model)) return false;
          assetData.setGroups(result.items);
          assetData.setGroupTotal(
            Math.max(result.total_count, result.items.length),
          );
          const matched =
            form.mode === "create"
              ? volcanoUniqueNewGroupMatch(
                  result.items,
                  createGroupBaselineIds,
                  { name, description },
                )
              : result.items.find(
                  (item) =>
                    item.id === form.groupId &&
                    item.name.trim() === name &&
                    item.description.trim() === description,
                );
          if (matched) assetData.setSelectedGroupId(matched.id);
          await assetData.refreshProjectAssetTotal(true, sessionId);
          return Boolean(matched);
        },
      },
    );
    if (operationId) {
      setGroupForm(null);
    } else {
      setGroupFormError("该素材组已有操作进行中");
    }
  };

  const saveAssetName = () => {
    if (!renameAsset) return;
    const target = renameAsset;
    const name = target.name.trim();
    if (!name) {
      setNotice({ tone: "error", text: "素材名称不能为空" });
      return;
    }
    const lockKey = volcanoAssetLockKey(target.asset.group_id, target.asset.id);
    if (operationController.operationHasConflict(lockKey)) {
      setNotice({
        tone: "error",
        text: "该素材或所属素材组已有后台操作进行中",
      });
      return;
    }
    const operationId = operationController.enqueueOperation(
      {
        action: "update_asset",
        lockKey,
        title: `重命名素材「${target.asset.name || "未命名素材"}」`,
        pendingLabel: "正在重命名素材",
      },
      {
        submit: (signal) =>
          patchVideoAsset(target.asset.id, model, { name }, { signal }),
        onSucceeded: async (_result, operation, sessionId) => {
          const asset = volcanoOperationAssetResult(operation);
          if (asset) {
            assetData.setAssets((current) =>
              current.map((item) => (item.id === asset.id ? asset : item)),
            );
            setSelected((current) =>
              current.map((item) =>
                item.id === asset.id ? assetSelection(asset) : item,
              ),
            );
          }
          await assetData.refreshAssets(true, sessionId);
        },
        verifyUnknown: async (signal, sessionId) => {
          const result = await scanVideoAssets({
            model,
            groupIds: [target.asset.group_id],
            assetTypes: [target.asset.asset_type],
            signal,
          });
          if (!isSessionActive(sessionId, model)) return false;
          const matched = result.items.find(
            (item) => item.id === target.asset.id && item.name === name,
          );
          if (matched) {
            assetData.setAssets((current) =>
              current.map((item) => (item.id === matched.id ? matched : item)),
            );
            setSelected((current) =>
              current.map((item) =>
                item.id === matched.id ? assetSelection(matched) : item,
              ),
            );
          }
          await assetData.refreshAssets(true, sessionId);
          return Boolean(matched);
        },
      },
    );
    if (operationId) {
      setRenameAsset(null);
      setNotice({
        tone: "status",
        text: "素材重命名任务已加入后台操作",
      });
    } else {
      setNotice({
        tone: "error",
        text: "该素材已有操作进行中",
      });
    }
  };

  const confirmDelete = () => {
    if (!deleteTarget) return;
    const target = deleteTarget;
    if (
      target.kind === "group" &&
      uploadQueue.uploadsRef.current.some(
        (item) =>
          item.groupId === target.group.id && uploadBlocksGroupMutation(item),
      )
    ) {
      setNotice({
        tone: "error",
        text: "该素材组仍有上传或后台创建任务，请先等待结果或移除未提交文件",
      });
      setDeleteTarget(null);
      return;
    }
    setDeleteTarget(null);
    let deletedGroupAssetIds: string[] = [];
    const operationId =
      target.kind === "asset"
        ? operationController.enqueueOperation(
            {
              action: "delete_asset",
              lockKey: volcanoAssetLockKey(
                target.asset.group_id,
                target.asset.id,
              ),
              title: `删除素材「${target.asset.name || "未命名素材"}」`,
              pendingLabel: "正在删除素材",
            },
            {
              submit: (signal) =>
                deleteVideoAsset(target.asset.id, model, { signal }),
              onSucceeded: async (_result, operation, sessionId) => {
                const deletedIds = volcanoDeletedAssetIds(
                  volcanoOperationDeleteResult(operation),
                  [target.asset.id],
                );
                assetData.setAssets((current) =>
                  current.filter((item) => !deletedIds.includes(item.id)),
                );
                setSelected((current) =>
                  current.filter((item) => !deletedIds.includes(item.id)),
                );
                notifyDeletedReferences(deletedIds);
                await Promise.all([
                  assetData.refreshAssets(true, sessionId),
                  assetData.refreshProjectAssetTotal(true, sessionId),
                ]);
                void assetData.refreshGroups(undefined, true, sessionId);
              },
              verifyUnknown: async (signal, sessionId) => {
                const result = await scanVideoAssets({
                  model,
                  groupIds: [target.asset.group_id],
                  assetTypes: [target.asset.asset_type],
                  signal,
                });
                if (!isSessionActive(sessionId, model)) return false;
                const deleted = !result.items.some(
                  (item) => item.id === target.asset.id,
                );
                if (deleted) {
                  assetData.setAssets((current) =>
                    current.filter((item) => item.id !== target.asset.id),
                  );
                  setSelected((current) =>
                    current.filter((item) => item.id !== target.asset.id),
                  );
                  notifyDeletedReferences([target.asset.id]);
                  void assetData.refreshProjectAssetTotal(true, sessionId);
                  void assetData.refreshGroups(undefined, true, sessionId);
                }
                await assetData.refreshAssets(true, sessionId);
                return deleted;
              },
            },
          )
        : operationController.enqueueOperation(
            {
              action: "delete_group",
              lockKey: volcanoGroupLockKey(target.group.id),
              title: `删除素材组「${target.group.name}」`,
              pendingLabel: "正在删除素材组",
              blocksChildren: true,
            },
            {
              prepare: async (signal) => {
                deletedGroupAssetIds = await allGroupAssetIds(
                  model,
                  target.group.id,
                  signal,
                );
              },
              submit: (signal) =>
                deleteVideoAssetGroup(model, target.group.id, {
                  signal,
                }),
              onSucceeded: async (_result, operation, sessionId) => {
                const deletedIds = volcanoDeletedAssetIds(
                  volcanoOperationDeleteResult(operation),
                  deletedGroupAssetIds,
                );
                notifyDeletedReferences(deletedIds);
                assetData.setGroups((current) =>
                  current.filter((item) => item.id !== target.group.id),
                );
                assetData.setSelectedGroupId((current) =>
                  current === target.group.id ? null : current,
                );
                setSelected((current) =>
                  current.filter((item) => item.group_id !== target.group.id),
                );
                assetData.setAssets((current) =>
                  current.filter((item) => item.group_id !== target.group.id),
                );
                for (const item of uploadQueue.uploadsRef.current.filter(
                  (upload) => upload.groupId === target.group.id,
                )) {
                  uploadQueue.uploadControllersRef.current
                    .get(item.id)
                    ?.abort();
                  uploadQueue.uploadControllersRef.current.delete(item.id);
                  uploadQueue.uploadNamesRef.current.delete(item.id);
                }
                uploadQueue.commitUploadQueue(model, (current) =>
                  current.filter((item) => item.groupId !== target.group.id),
                );
                await Promise.all([
                  assetData.refreshGroups(undefined, true, sessionId),
                  assetData.refreshProjectAssetTotal(true, sessionId),
                ]);
              },
              verifyUnknown: async (signal, sessionId) => {
                const result = await listVideoAssetGroups({
                  model,
                  group_ids: [target.group.id],
                  page_number: 1,
                  page_size: 1,
                  signal,
                });
                if (!isSessionActive(sessionId, model)) return false;
                const deleted = result.items.length === 0;
                if (deleted) {
                  notifyDeletedReferences(deletedGroupAssetIds);
                  assetData.setGroups((current) =>
                    current.filter((item) => item.id !== target.group.id),
                  );
                  assetData.setSelectedGroupId((current) =>
                    current === target.group.id ? null : current,
                  );
                  setSelected((current) =>
                    current.filter((item) => item.group_id !== target.group.id),
                  );
                  uploadQueue.commitUploadQueue(model, (current) =>
                    current.filter((item) => item.groupId !== target.group.id),
                  );
                  void assetData.refreshProjectAssetTotal(true, sessionId);
                }
                return deleted;
              },
            },
          );
    if (!operationId) {
      setNotice({
        tone: "error",
        text: "该对象已有后台操作进行中",
      });
    }
  };

  const toggleAsset = (asset: VideoAssetOut) => {
    const result = toggleVolcanoAssetSelection({
      current: selected,
      candidate: assetSelection(asset),
      existingAssetIds: existingIds,
      remainingLimits,
    });
    setSelected(result.items);
    if (result.issue) {
      setNotice({
        tone: "error",
        text: volcanoAssetSelectionIssueMessage(result.issue),
      });
    }
  };

  return (
    <VolcanoAssetManagerPresentation
      open={open}
      titleId={titleId}
      descriptionId={descriptionId}
      uploadInputId={uploadInputId}
      dialogRef={dialogRef}
      closeButtonRef={closeButtonRef}
      onKeyDown={onDialogKeyDown}
      onClose={onClose}
      remainingLimits={remainingLimits}
      onUse={onUse}
      assetData={assetData}
      uploadQueue={uploadQueue}
      operationController={operationController}
      uploadController={uploadController}
      pendingOperationsByLock={pendingOperationsByLock}
      blockedUploadIds={blockedUploadIds}
      uploadDisabledReason={uploadDisabledReason}
      groupCreateDisabledReason={groupCreateDisabledReason}
      groupSearch={groupSearch}
      setGroupSearch={setGroupSearch}
      filteredGroups={filteredGroups}
      groupForm={groupForm}
      setGroupForm={setGroupForm}
      groupFormError={groupFormError}
      setGroupFormError={setGroupFormError}
      dragActive={dragActive}
      setDragActive={setDragActive}
      notice={notice}
      selectedGroup={selectedGroup}
      loadedAssets={loadedAssets}
      totalAssetPages={totalAssetPages}
      selected={selected}
      setSelected={setSelected}
      existingIds={existingIds}
      selectedImageCount={selectedImageCount}
      selectedVideoCount={selectedVideoCount}
      selectedGroupDeleting={selectedGroupDeleting}
      selectedGroupOperation={selectedGroupOperation}
      renameAsset={renameAsset}
      setRenameAsset={setRenameAsset}
      deleteTarget={deleteTarget}
      setDeleteTarget={setDeleteTarget}
      saveGroup={saveGroup}
      saveAssetName={saveAssetName}
      toggleAsset={toggleAsset}
      confirmDelete={confirmDelete}
    />
  );
}
