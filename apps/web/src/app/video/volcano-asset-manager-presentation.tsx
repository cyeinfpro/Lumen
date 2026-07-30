"use client";

import type { Dispatch, SetStateAction } from "react";

import type { VideoAssetGroupOut, VideoAssetOut } from "@/lib/types";

import type {
  DeleteTarget,
  GroupFormState,
  Notice,
  OperationItem,
  VolcanoAssetManagerProps,
  VolcanoAssetSelection,
} from "./volcano-asset-manager-types";
import {
  VolcanoAssetManagerView,
  type VolcanoAssetManagerViewProps,
} from "./volcano-asset-manager-view";
import type { VolcanoAssetDataController } from "./use-volcano-asset-data";
import type { VolcanoOperationController } from "./use-volcano-operation-controller";
import type { VolcanoUploadController } from "./use-volcano-upload-controller";
import type { VolcanoUploadQueueController } from "./use-volcano-upload-queue";

type RenameAssetState = {
  asset: VideoAssetOut;
  name: string;
};

type VolcanoAssetManagerPresentationProps = Pick<
  VolcanoAssetManagerViewProps,
  | "open"
  | "titleId"
  | "descriptionId"
  | "uploadInputId"
  | "dialogRef"
  | "closeButtonRef"
  | "onKeyDown"
  | "onClose"
> & {
  remainingLimits: VolcanoAssetManagerProps["remainingLimits"];
  onUse: VolcanoAssetManagerProps["onUse"];
  assetData: VolcanoAssetDataController;
  uploadQueue: VolcanoUploadQueueController;
  operationController: VolcanoOperationController;
  uploadController: VolcanoUploadController;
  pendingOperationsByLock: ReadonlyMap<string, OperationItem>;
  blockedUploadIds: ReadonlySet<string>;
  uploadDisabledReason: string | null;
  groupCreateDisabledReason: string | null;
  groupSearch: string;
  setGroupSearch: Dispatch<SetStateAction<string>>;
  filteredGroups: VideoAssetGroupOut[];
  groupForm: GroupFormState | null;
  setGroupForm: Dispatch<SetStateAction<GroupFormState | null>>;
  groupFormError: string | null;
  setGroupFormError: Dispatch<SetStateAction<string | null>>;
  dragActive: boolean;
  setDragActive: Dispatch<SetStateAction<boolean>>;
  notice: Notice | null;
  selectedGroup?: VideoAssetGroupOut;
  loadedAssets: VideoAssetOut[];
  totalAssetPages: number;
  selected: VolcanoAssetSelection[];
  setSelected: Dispatch<SetStateAction<VolcanoAssetSelection[]>>;
  existingIds: ReadonlySet<string>;
  selectedImageCount: number;
  selectedVideoCount: number;
  selectedGroupDeleting: boolean;
  selectedGroupOperation?: OperationItem;
  renameAsset: RenameAssetState | null;
  setRenameAsset: Dispatch<SetStateAction<RenameAssetState | null>>;
  deleteTarget: DeleteTarget | null;
  setDeleteTarget: Dispatch<SetStateAction<DeleteTarget | null>>;
  saveGroup: () => void;
  saveAssetName: () => void;
  toggleAsset: (asset: VideoAssetOut) => void;
  confirmDelete: () => void;
};

export function VolcanoAssetManagerPresentation({
  open,
  titleId,
  descriptionId,
  uploadInputId,
  dialogRef,
  closeButtonRef,
  onKeyDown,
  onClose,
  remainingLimits,
  onUse,
  assetData,
  uploadQueue,
  operationController,
  uploadController,
  pendingOperationsByLock,
  blockedUploadIds,
  uploadDisabledReason,
  groupCreateDisabledReason,
  groupSearch,
  setGroupSearch,
  filteredGroups,
  groupForm,
  setGroupForm,
  groupFormError,
  setGroupFormError,
  dragActive,
  setDragActive,
  notice,
  selectedGroup,
  loadedAssets,
  totalAssetPages,
  selected,
  setSelected,
  existingIds,
  selectedImageCount,
  selectedVideoCount,
  selectedGroupDeleting,
  selectedGroupOperation,
  renameAsset,
  setRenameAsset,
  deleteTarget,
  setDeleteTarget,
  saveGroup,
  saveAssetName,
  toggleAsset,
  confirmDelete,
}: VolcanoAssetManagerPresentationProps) {
  return (
    <VolcanoAssetManagerView
      open={open}
      titleId={titleId}
      descriptionId={descriptionId}
      uploadInputId={uploadInputId}
      dialogRef={dialogRef}
      closeButtonRef={closeButtonRef}
      onKeyDown={onKeyDown}
      onClose={onClose}
      capability={{
        value: assetData.capability,
        loading: assetData.capabilityLoading,
        error: assetData.capabilityError,
        onRetry: () => void assetData.loadCapability(),
      }}
      quotas={{
        projectAssetTotal: assetData.projectAssetTotal,
        projectGroupTotal: assetData.projectGroupTotal,
        quotaLoading: assetData.quotaLoading,
        quotaError: assetData.quotaError,
      }}
      groups={{
        groups: assetData.groups,
        filteredGroups,
        groupTotal: assetData.groupTotal,
        loading: assetData.groupsLoading,
        error: assetData.groupsError,
        search: groupSearch,
        selectedGroupId: assetData.selectedGroupId,
        form: groupForm,
        formError: groupFormError,
        createDisabledReason: groupCreateDisabledReason,
        uploads: uploadQueue.uploads,
        pendingOperationsByLock,
        onSearchChange: setGroupSearch,
        onOpenCreate: () => {
          setGroupForm({
            mode: "create",
            name: "",
            description: "",
          });
          setGroupFormError(null);
        },
        onFormChange: setGroupForm,
        onCancelForm: () => setGroupForm(null),
        onSaveForm: saveGroup,
        onSelect: assetData.selectGroup,
        onRename: (group) => {
          setGroupForm({
            mode: "rename",
            groupId: group.id,
            name: group.name,
            description: group.description,
          });
          setGroupFormError(null);
        },
        onDelete: (group) => setDeleteTarget({ kind: "group", group }),
      }}
      uploads={{
        operations: operationController.operations,
        uploads: uploadQueue.uploads,
        blockedUploadIds,
        disabledReason: uploadDisabledReason,
        pendingAssetCreates: uploadController.pendingAssetCreates,
        dragActive,
        notice,
        onRetryOperation: operationController.retryOperation,
        onDismissOperation: operationController.dismissOperation,
        onDragActive: setDragActive,
        onFiles: uploadController.enqueueFiles,
        onRename: uploadController.renameUpload,
        onRemove: uploadController.removeUpload,
        onRetry: uploadController.retryUpload,
      }}
      assets={{
        selectedGroup,
        selectedGroupId: assetData.selectedGroupId,
        totalCount: assetData.assetTotal,
        loadedAssetCount: loadedAssets.length,
        searchInput: assetData.assetSearchInput,
        search: assetData.assetSearch,
        typeFilter: assetData.typeFilter,
        statusFilter: assetData.statusFilter,
        loading: assetData.assetsLoading,
        error: assetData.assetsError,
        visibleAssets: loadedAssets,
        page: Math.min(assetData.assetPage, totalAssetPages),
        totalPages: totalAssetPages,
        selected,
        existingIds,
        remainingLimits,
        selectedImageCount,
        selectedVideoCount,
        pendingOperationsByLock,
        selectedGroupDeleting,
        selectedGroupOperation,
        renameAsset,
        onSearchInputChange: assetData.setAssetSearchInput,
        onTypeFilterChange: assetData.changeTypeFilter,
        onStatusFilterChange: assetData.changeStatusFilter,
        onRefresh: () => {
          void assetData.refreshAssets(false);
          void assetData.refreshProjectAssetTotal(true);
          void assetData.refreshGroups(undefined, true);
        },
        onRenameAssetChange: (name) =>
          setRenameAsset((current) =>
            current ? { ...current, name } : current,
          ),
        onCancelRename: () => setRenameAsset(null),
        onSaveRename: saveAssetName,
        onToggle: toggleAsset,
        onOpenRename: (asset) =>
          setRenameAsset({
            asset,
            name: asset.name || "",
          }),
        onDelete: (asset) => setDeleteTarget({ kind: "asset", asset }),
        onPreviousPage: () =>
          assetData.setAssetPage((current) => Math.max(1, current - 1)),
        onNextPage: () =>
          assetData.setAssetPage((current) =>
            Math.min(totalAssetPages, current + 1),
          ),
      }}
      selection={{
        selected,
        selectedImageCount,
        selectedVideoCount,
        remainingLimits,
        onClear: () => setSelected([]),
        onUse: () => {
          onUse(selected);
          onClose();
        },
      }}
      deleteDialog={{
        target: deleteTarget,
        onClose: () => setDeleteTarget(null),
        onConfirm: confirmDelete,
      }}
    />
  );
}
