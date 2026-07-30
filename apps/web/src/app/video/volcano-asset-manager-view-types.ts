import type { KeyboardEventHandler, Ref } from "react";
import type { VideoAssetCapabilitiesOut, VideoAssetGroupOut, VideoAssetOut } from "@/lib/types";
import type {
  AssetStatusFilter,
  AssetTypeFilter,
  DeleteTarget,
  GroupFormState,
  Notice,
  OperationItem,
  UploadItem,
  VolcanoAssetSelection,
} from "./volcano-asset-manager-types";

export type CapabilityView = {
  value: VideoAssetCapabilitiesOut | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
};

export type QuotaView = {
  projectAssetTotal: number | null;
  projectGroupTotal: number | null;
  quotaLoading: boolean;
  quotaError: string | null;
};

export type GroupPanelView = {
  groups: VideoAssetGroupOut[];
  filteredGroups: VideoAssetGroupOut[];
  groupTotal: number | null;
  loading: boolean;
  error: string | null;
  search: string;
  selectedGroupId: string | null;
  form: GroupFormState | null;
  formError: string | null;
  createDisabledReason: string | null;
  uploads: UploadItem[];
  pendingOperationsByLock: ReadonlyMap<string, OperationItem>;
  onSearchChange: (value: string) => void;
  onOpenCreate: () => void;
  onFormChange: (form: GroupFormState) => void;
  onCancelForm: () => void;
  onSaveForm: () => void;
  onSelect: (groupId: string) => void;
  onRename: (group: VideoAssetGroupOut) => void;
  onDelete: (group: VideoAssetGroupOut) => void;
};

export type UploadPanelView = {
  operations: OperationItem[];
  uploads: UploadItem[];
  blockedUploadIds: ReadonlySet<string>;
  disabledReason: string | null;
  pendingAssetCreates: number;
  dragActive: boolean;
  notice: Notice | null;
  onRetryOperation: (operationId: string) => void;
  onDismissOperation: (operationId: string) => void;
  onDragActive: (active: boolean) => void;
  onFiles: (files: File[]) => void;
  onRename: (id: string, name: string) => void;
  onRemove: (id: string) => void;
  onRetry: (id: string) => void;
};

export type AssetPanelView = {
  selectedGroup?: VideoAssetGroupOut;
  selectedGroupId: string | null;
  totalCount: number;
  loadedAssetCount: number;
  searchInput: string;
  search: string;
  typeFilter: AssetTypeFilter;
  statusFilter: AssetStatusFilter;
  loading: boolean;
  error: string | null;
  visibleAssets: VideoAssetOut[];
  page: number;
  totalPages: number;
  selected: VolcanoAssetSelection[];
  existingIds: ReadonlySet<string>;
  remainingLimits: { image: number; video: number };
  selectedImageCount: number;
  selectedVideoCount: number;
  pendingOperationsByLock: ReadonlyMap<string, OperationItem>;
  selectedGroupDeleting: boolean;
  selectedGroupOperation?: OperationItem;
  renameAsset: { asset: VideoAssetOut; name: string } | null;
  onSearchInputChange: (value: string) => void;
  onTypeFilterChange: (value: AssetTypeFilter) => void;
  onStatusFilterChange: (value: AssetStatusFilter) => void;
  onRefresh: () => void;
  onRenameAssetChange: (name: string) => void;
  onCancelRename: () => void;
  onSaveRename: () => void;
  onToggle: (asset: VideoAssetOut) => void;
  onOpenRename: (asset: VideoAssetOut) => void;
  onDelete: (asset: VideoAssetOut) => void;
  onPreviousPage: () => void;
  onNextPage: () => void;
};

export type SelectionFooterView = {
  selected: VolcanoAssetSelection[];
  selectedImageCount: number;
  selectedVideoCount: number;
  remainingLimits: { image: number; video: number };
  onClear: () => void;
  onUse: () => void;
};

export type VolcanoAssetManagerViewProps = {
  open: boolean;
  titleId: string;
  descriptionId: string;
  uploadInputId: string;
  dialogRef: Ref<HTMLElement>;
  closeButtonRef: Ref<HTMLButtonElement>;
  onKeyDown: KeyboardEventHandler<HTMLElement>;
  onClose: () => void;
  capability: CapabilityView;
  quotas: QuotaView;
  groups: GroupPanelView;
  uploads: UploadPanelView;
  assets: AssetPanelView;
  selection: SelectionFooterView;
  deleteDialog: {
    target: DeleteTarget | null;
    onClose: () => void;
    onConfirm: () => void;
  };
};
