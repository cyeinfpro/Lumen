"use client";

import type {
  KeyboardEventHandler,
  Ref,
} from "react";
import {
  AlertCircle,
  Check,
  Folder,
  FolderPlus,
  Image as ImageIcon,
  Loader2,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  X,
} from "lucide-react";

import {
  Button,
  ConfirmDialog,
  EmptyState,
  IconButton,
  Input,
} from "@/components/ui/primitives";
import type {
  VideoAssetCapabilitiesOut,
  VideoAssetGroupOut,
  VideoAssetOut,
} from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  VOLCANO_ASSET_NAME_MAX_LENGTH,
  VOLCANO_PROJECT_ASSET_LIMIT,
  VOLCANO_PROJECT_GROUP_LIMIT,
  volcanoAssetLockKey,
  volcanoGroupLockKey,
} from "./volcano-asset-domain";
import {
  AssetCard,
  AssetPagination,
  CapabilityUnavailable,
  GroupEditor,
  InlineMessage,
  LoadingPanel,
  MetaRow,
  OperationActivity,
  ProjectQuotaBadge,
  SegmentedFilter,
  UploadArea,
} from "./volcano-asset-manager-components";
import { uploadBlocksGroupMutation } from "./volcano-asset-manager-state";
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

type CapabilityView = {
  value: VideoAssetCapabilitiesOut | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
};

type QuotaView = {
  projectAssetTotal: number | null;
  projectGroupTotal: number | null;
  quotaLoading: boolean;
  quotaError: string | null;
};

type GroupPanelView = {
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

type UploadPanelView = {
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

type AssetPanelView = {
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

type SelectionFooterView = {
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

function ManagerHeader({
  titleId,
  descriptionId,
  closeButtonRef,
  onClose,
  quotas,
}: Pick<
  VolcanoAssetManagerViewProps,
  "titleId" | "descriptionId" | "closeButtonRef" | "onClose" | "quotas"
>) {
  return (
    <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3 sm:px-5 sm:py-4">
      <div className="min-w-0">
        <p className="type-page-kicker">SEEDANCE · AIGC ASSET GROUP</p>
        <h2 id={titleId} className="type-page-title-sm mt-1">
          火山虚拟素材库
        </h2>
        <p
          id={descriptionId}
          className="type-page-subtitle mt-1 max-w-3xl text-pretty"
        >
          当前火山 Project 共享素材库，仅管理官方 AIGC
          虚拟人像图片和视频。普通非人像素材继续使用视频页“上传参考”。
        </p>
        <div className="mt-3 flex flex-wrap gap-2">
          <ProjectQuotaBadge
            label="素材总配额"
            used={quotas.projectAssetTotal}
            limit={VOLCANO_PROJECT_ASSET_LIMIT}
            loading={quotas.quotaLoading}
          />
          <ProjectQuotaBadge
            label="素材组总配额"
            used={quotas.projectGroupTotal}
            limit={VOLCANO_PROJECT_GROUP_LIMIT}
            loading={quotas.quotaLoading}
          />
        </div>
        {quotas.quotaError ? (
          <p role="alert" className="type-caption mt-2 text-danger">
            {quotas.quotaError}
          </p>
        ) : null}
      </div>
      <IconButton
        ref={closeButtonRef}
        aria-label="关闭火山虚拟素材库"
        tooltip="关闭"
        variant="ghost"
        size="md"
        className="h-11 w-11"
        onClick={onClose}
      >
        <X className="h-4 w-4" />
      </IconButton>
    </header>
  );
}

function GroupFixedScope({
  capability,
}: {
  capability: VideoAssetCapabilitiesOut | null;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/70 p-3">
      <p className="type-overline text-[var(--fg-2)]">固定范围</p>
      <dl className="mt-2 grid gap-1.5 type-caption text-[var(--fg-1)]">
        <MetaRow label="GroupType" value="AIGC" />
        <MetaRow
          label="ProjectName"
          value={capability?.project_name || "未配置"}
        />
        <MetaRow label="Region" value={capability?.region || "未配置"} />
      </dl>
    </div>
  );
}

function GroupSearchControls({ groups }: { groups: GroupPanelView }) {
  return (
    <>
      <div className="mt-3 flex items-center gap-2">
        <Input
          aria-label="搜索素材组"
          placeholder="搜索素材组"
          value={groups.search}
          onChange={(event) => groups.onSearchChange(event.target.value)}
          leftIcon={<Search className="h-4 w-4" />}
          wrapperClassName="min-w-0 flex-1"
        />
        <IconButton
          aria-label="新建 AIGC 素材组"
          tooltip={groups.createDisabledReason || "新建素材组"}
          variant="secondary"
          size="md"
          disabled={groups.createDisabledReason !== null}
          onClick={groups.onOpenCreate}
        >
          <FolderPlus className="h-4 w-4" />
        </IconButton>
      </div>
      {groups.createDisabledReason ? (
        <p role="status" className="type-caption mt-2 text-warning">
          {groups.createDisabledReason}
        </p>
      ) : null}
    </>
  );
}

function GroupRow({
  group,
  groups,
}: {
  group: VideoAssetGroupOut;
  groups: GroupPanelView;
}) {
  const active = group.id === groups.selectedGroupId;
  const pendingOperation = groups.pendingOperationsByLock.get(
    volcanoGroupLockKey(group.id),
  );
  const groupHasActiveUploads = groups.uploads.some(
    (item) =>
      item.groupId === group.id && uploadBlocksGroupMutation(item),
  );
  return (
    <div
      className={cn(
        "flex min-h-11 items-center rounded-[var(--radius-card)] border",
        active
          ? "border-accent-border bg-accent-soft"
          : "border-transparent hover:border-[var(--border)] hover:bg-[var(--bg-1)]",
      )}
    >
      <button
        type="button"
        aria-current={active ? "true" : undefined}
        className="flex min-h-11 min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        onClick={() => groups.onSelect(group.id)}
      >
        <Folder
          className={cn(
            "h-4 w-4 shrink-0",
            active ? "text-accent" : "text-[var(--fg-2)]",
          )}
        />
        <span className="min-w-0">
          <span className="type-body-sm block truncate text-[var(--fg-0)]">
            {group.name}
          </span>
          <span className="type-caption block truncate text-[var(--fg-2)]">
            {pendingOperation
              ? pendingOperation.pendingLabel
              : group.description || "无描述"}
          </span>
        </span>
      </button>
      {active ? (
        <div className="flex shrink-0 pr-1">
          <IconButton
            aria-label={`重命名素材组 ${group.name}`}
            tooltip="重命名"
            variant="ghost"
            size="sm"
            disabled={Boolean(pendingOperation)}
            onClick={() => groups.onRename(group)}
          >
            <Pencil className="h-3.5 w-3.5" />
          </IconButton>
          <IconButton
            aria-label={`删除云端素材组 ${group.name}`}
            tooltip={
              groupHasActiveUploads
                ? "组内仍有上传或后台创建任务"
                : "删除云端素材组"
            }
            variant="ghost"
            size="sm"
            disabled={Boolean(pendingOperation) || groupHasActiveUploads}
            onClick={() => groups.onDelete(group)}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      ) : null}
    </div>
  );
}

function GroupList({ groups }: { groups: GroupPanelView }) {
  if (groups.loading) {
    return (
      <div className="mt-4 flex items-center justify-center gap-2 type-body-sm text-[var(--fg-2)]">
        <Loader2 className="h-4 w-4 animate-spin" />
        加载素材组
      </div>
    );
  }
  if (groups.filteredGroups.length === 0) {
    return (
      <p className="px-3 py-6 text-center type-body-sm text-[var(--fg-2)]">
        {groups.groups.length === 0 ? "暂无素材组" : "无结果"}
      </p>
    );
  }
  return (
    <div className="mt-3 space-y-1">
      {groups.filteredGroups.map((group) => (
        <GroupRow key={group.id} group={group} groups={groups} />
      ))}
    </div>
  );
}

function GroupSidebar({
  capability,
  groups,
}: {
  capability: VideoAssetCapabilitiesOut | null;
  groups: GroupPanelView;
}) {
  return (
    <aside className="border-b border-[var(--border)] bg-[var(--bg-0)]/55 p-3 lg:min-h-0 lg:overflow-y-auto lg:border-b-0 lg:border-r lg:p-4">
      <GroupFixedScope capability={capability} />
      <GroupSearchControls groups={groups} />
      {groups.form ? (
        <GroupEditor
          form={groups.form}
          projectName={capability?.project_name || "未配置"}
          error={groups.formError}
          onChange={groups.onFormChange}
          onCancel={groups.onCancelForm}
          onSave={groups.onSaveForm}
        />
      ) : null}
      {groups.error ? (
        <div role="alert" aria-live="assertive">
          <InlineMessage tone="error">{groups.error}</InlineMessage>
        </div>
      ) : null}
      <GroupList groups={groups} />
    </aside>
  );
}

function AssetToolbar({ assets }: { assets: AssetPanelView }) {
  return (
    <div className="shrink-0 border-b border-[var(--border)] px-3 py-3 sm:px-4">
      <div className="flex flex-col gap-3 xl:flex-row xl:items-center">
        <div className="min-w-0 flex-1">
          <p className="type-card-title truncate">
            {assets.selectedGroup?.name || "选择素材组"}
          </p>
          <p className="type-caption mt-0.5 text-[var(--fg-2)]">
            {assets.selectedGroup
              ? `${assets.totalCount} 个云端素材`
              : "新建或选择 AIGC 素材组后上传"}
          </p>
        </div>
        <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center">
          <Input
            aria-label="搜索火山虚拟素材"
            placeholder="服务端搜索名称"
            value={assets.searchInput}
            onChange={(event) =>
              assets.onSearchInputChange(event.target.value)
            }
            leftIcon={<Search className="h-4 w-4" />}
            wrapperClassName="min-w-0 sm:w-56"
            disabled={!assets.selectedGroupId}
          />
          <SegmentedFilter
            value={assets.typeFilter}
            options={[
              { value: "all", label: "全部" },
              { value: "Image", label: "图片" },
              { value: "Video", label: "视频" },
            ]}
            onChange={(value) =>
              assets.onTypeFilterChange(value as AssetTypeFilter)
            }
          />
          <select
            aria-label="筛选素材状态"
            value={assets.statusFilter}
            onChange={(event) =>
              assets.onStatusFilterChange(
                event.target.value as AssetStatusFilter,
              )
            }
            className="min-h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 type-body-sm text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] sm:min-h-9"
          >
            <option value="all">全部状态</option>
            <option value="Active">可用</option>
            <option value="Processing">处理中</option>
            <option value="Failed">失败</option>
          </select>
          <IconButton
            aria-label="刷新火山虚拟素材"
            tooltip="刷新"
            variant="secondary"
            size="md"
            loading={assets.loading}
            disabled={!assets.selectedGroupId}
            onClick={assets.onRefresh}
          >
            <RefreshCw className="h-4 w-4" />
          </IconButton>
        </div>
      </div>
    </div>
  );
}

function RenameAssetEditor({ assets }: { assets: AssetPanelView }) {
  if (!assets.renameAsset) return null;
  return (
    <div className="mt-3 flex flex-col gap-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-3 sm:flex-row sm:items-end">
      <Input
        label="素材名称"
        value={assets.renameAsset.name}
        maxLength={VOLCANO_ASSET_NAME_MAX_LENGTH}
        onChange={(event) =>
          assets.onRenameAssetChange(event.target.value)
        }
        wrapperClassName="min-w-0 flex-1"
      />
      <Button variant="ghost" size="sm" onClick={assets.onCancelRename}>
        取消
      </Button>
      <Button variant="primary" size="sm" onClick={assets.onSaveRename}>
        保存
      </Button>
    </div>
  );
}

function ActivityAndUpload({
  uploadInputId,
  uploads,
  assets,
}: {
  uploadInputId: string;
  uploads: UploadPanelView;
  assets: AssetPanelView;
}) {
  return (
    <>
      <OperationActivity
        operations={uploads.operations}
        onRetry={uploads.onRetryOperation}
        onDismiss={uploads.onDismissOperation}
      />
      {assets.selectedGroupId ? (
        <UploadArea
          inputId={uploadInputId}
          dragActive={uploads.dragActive}
          uploads={uploads.uploads}
          blockedUploadIds={uploads.blockedUploadIds}
          disabledReason={uploads.disabledReason}
          pendingAssetCreates={uploads.pendingAssetCreates}
          onDragActive={uploads.onDragActive}
          onFiles={uploads.onFiles}
          onRename={uploads.onRename}
          onRemove={uploads.onRemove}
          onRetry={uploads.onRetry}
        />
      ) : null}
      {uploads.notice ? (
        <InlineMessage tone={uploads.notice.tone} className="mt-3">
          {uploads.notice.text}
        </InlineMessage>
      ) : null}
      {assets.error ? (
        <div role="alert" aria-live="assertive">
          <InlineMessage tone="error" className="mt-3">
            {assets.error}
          </InlineMessage>
        </div>
      ) : null}
      <RenameAssetEditor assets={assets} />
    </>
  );
}

function AssetGridItem({
  asset,
  assets,
}: {
  asset: VideoAssetOut;
  assets: AssetPanelView;
}) {
  const pendingOperation =
    assets.pendingOperationsByLock.get(
      volcanoAssetLockKey(asset.group_id, asset.id),
    ) ??
    (assets.selectedGroupDeleting
      ? assets.selectedGroupOperation
      : undefined);
  const atLimit =
    asset.asset_type === "Image"
      ? assets.selectedImageCount >= Math.max(0, assets.remainingLimits.image)
      : assets.selectedVideoCount >= Math.max(0, assets.remainingLimits.video);
  return (
    <AssetCard
      asset={asset}
      selected={assets.selected.some((item) => item.id === asset.id)}
      existing={assets.existingIds.has(asset.id)}
      pendingOperation={pendingOperation}
      atLimit={atLimit}
      onToggle={() => assets.onToggle(asset)}
      onRename={() => assets.onOpenRename(asset)}
      onDelete={() => assets.onDelete(asset)}
    />
  );
}

function AssetResults({ assets }: { assets: AssetPanelView }) {
  if (!assets.selectedGroupId) {
    return (
      <EmptyState
        icon={<FolderPlus className="h-5 w-5" />}
        title="先选择素材组"
        description="火山虚拟素材必须存入 GroupType=AIGC 的官方素材组。"
      />
    );
  }
  if (assets.loading && assets.loadedAssetCount === 0) {
    return <LoadingPanel label="加载火山虚拟素材" compact />;
  }
  if (assets.visibleAssets.length === 0) {
    const filtered = Boolean(assets.search) || assets.typeFilter !== "all";
    return (
      <EmptyState
        icon={<ImageIcon className="h-5 w-5" />}
        title={filtered ? "无结果" : "暂无虚拟素材"}
        description="上传后自动优化为火山规格；完成处理后即可选入视频草稿。"
      />
    );
  }
  return (
    <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
      {assets.visibleAssets.map((asset) => (
        <AssetGridItem key={asset.id} asset={asset} assets={assets} />
      ))}
    </div>
  );
}

function AssetPaginationView({ assets }: { assets: AssetPanelView }) {
  if (!assets.selectedGroupId || assets.loading || assets.totalPages <= 1) {
    return null;
  }
  return (
    <AssetPagination
      page={assets.page}
      totalPages={assets.totalPages}
      totalCount={assets.totalCount}
      onPrevious={assets.onPreviousPage}
      onNext={assets.onNextPage}
    />
  );
}

function AssetWorkspace({
  uploadInputId,
  uploads,
  assets,
}: {
  uploadInputId: string;
  uploads: UploadPanelView;
  assets: AssetPanelView;
}) {
  return (
    <main className="flex min-h-0 flex-col bg-[var(--bg-1)]">
      <AssetToolbar assets={assets} />
      <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto p-3 sm:p-4">
        <ActivityAndUpload
          uploadInputId={uploadInputId}
          uploads={uploads}
          assets={assets}
        />
        <AssetResults assets={assets} />
        <AssetPaginationView assets={assets} />
      </div>
    </main>
  );
}

function ManagerWorkspace({
  capability,
  groups,
  uploadInputId,
  uploads,
  assets,
}: {
  capability: VideoAssetCapabilitiesOut | null;
  groups: GroupPanelView;
  uploadInputId: string;
  uploads: UploadPanelView;
  assets: AssetPanelView;
}) {
  return (
    <div className="grid min-h-full lg:h-full lg:grid-cols-[300px_minmax(0,1fr)]">
      <GroupSidebar capability={capability} groups={groups} />
      <AssetWorkspace
        uploadInputId={uploadInputId}
        uploads={uploads}
        assets={assets}
      />
    </div>
  );
}

function CapabilityGate({
  capability,
  groups,
  uploadInputId,
  uploads,
  assets,
}: Pick<
  VolcanoAssetManagerViewProps,
  "capability" | "groups" | "uploadInputId" | "uploads" | "assets"
>) {
  if (capability.loading) {
    return <LoadingPanel label="检查火山资产能力" />;
  }
  if (capability.error) {
    return (
      <EmptyState
        role="alert"
        icon={<AlertCircle className="h-5 w-5" />}
        title="能力检查失败"
        description={capability.error}
        action={
          <Button
            variant="outline"
            size="sm"
            leftIcon={<RefreshCw className="h-4 w-4" />}
            onClick={capability.onRetry}
          >
            重试
          </Button>
        }
      />
    );
  }
  if (capability.value && !capability.value.ready) {
    return <CapabilityUnavailable capability={capability.value} />;
  }
  return (
    <ManagerWorkspace
      capability={capability.value}
      groups={groups}
      uploadInputId={uploadInputId}
      uploads={uploads}
      assets={assets}
    />
  );
}

function ManagerFooter({
  selection,
  onClose,
}: Pick<VolcanoAssetManagerViewProps, "selection" | "onClose">) {
  const hasSelection = selection.selected.length > 0;
  return (
    <footer className="mobile-dialog-footer flex shrink-0 flex-col gap-2 border-t border-[var(--border)] bg-[var(--bg-1)]/92 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-5">
      <div className="min-w-0 type-body-sm text-[var(--fg-1)]">
        <span className="font-medium text-[var(--fg-0)]">
          已选 {selection.selected.length} 个
        </span>
        <span className="ml-2 text-[var(--fg-2)]">
          图片 {selection.selectedImageCount}/
          {Math.max(0, selection.remainingLimits.image)}
          {" · "}
          视频 {selection.selectedVideoCount}/
          {Math.max(0, selection.remainingLimits.video)}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 min-[430px]:grid-cols-3 sm:flex">
        <Button
          variant="ghost"
          size="sm"
          disabled={!hasSelection}
          onClick={selection.onClear}
        >
          取消本次选择
        </Button>
        <Button variant="secondary" size="sm" onClick={onClose}>
          关闭
        </Button>
        <Button
          variant="primary"
          size="sm"
          disabled={!hasSelection}
          leftIcon={<Check className="h-4 w-4" />}
          className="col-span-2 min-[430px]:col-span-1"
          onClick={selection.onUse}
        >
          使用 {selection.selected.length} 个素材
        </Button>
      </div>
    </footer>
  );
}

function DeleteDialogView({
  deleteDialog,
}: Pick<VolcanoAssetManagerViewProps, "deleteDialog">) {
  let title = "删除云端素材";
  let confirmText = "删除云端素材";
  let description = null;
  if (deleteDialog.target?.kind === "group") {
    title = "删除云端素材组";
    confirmText = "级联删除";
    description = (
      <>
        删除“{deleteDialog.target.group.name}”会
        <strong>级联删除组内全部云端素材</strong>
        ，且无法恢复。这不是取消本次选择。
      </>
    );
  }
  if (deleteDialog.target?.kind === "asset") {
    description = (
      <>
        将从火山虚拟素材库永久删除“
        {deleteDialog.target.asset.name || "未命名素材"}”。
        取消本次选择不会删除云端素材。
      </>
    );
  }
  return (
    <ConfirmDialog
      open={deleteDialog.target !== null}
      onOpenChange={(next) => {
        if (!next) deleteDialog.onClose();
      }}
      title={title}
      description={description}
      confirmText={confirmText}
      tone="danger"
      confirming={false}
      onConfirm={deleteDialog.onConfirm}
    />
  );
}

export function VolcanoAssetManagerView({
  open,
  titleId,
  descriptionId,
  uploadInputId,
  dialogRef,
  closeButtonRef,
  onKeyDown,
  onClose,
  capability,
  quotas,
  groups,
  uploads,
  assets,
  selection,
  deleteDialog,
}: VolcanoAssetManagerViewProps) {
  if (!open) return null;
  return (
    <>
      <div
        className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-[var(--surface-scrim)] sm:items-center"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) onClose();
        }}
      >
        <section
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          aria-describedby={descriptionId}
          tabIndex={-1}
          onKeyDown={onKeyDown}
          className="mobile-dialog-panel surface-dialog flex h-[var(--mobile-dialog-max-height)] w-full max-w-[1480px] flex-col overflow-hidden rounded-t-[var(--radius-sheet)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] sm:h-[min(92dvh,940px)] sm:rounded-[var(--radius-dialog)] sm:border-b"
        >
          <ManagerHeader
            titleId={titleId}
            descriptionId={descriptionId}
            closeButtonRef={closeButtonRef}
            onClose={onClose}
            quotas={quotas}
          />
          <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto lg:overflow-hidden">
            <CapabilityGate
              capability={capability}
              groups={groups}
              uploadInputId={uploadInputId}
              uploads={uploads}
              assets={assets}
            />
          </div>
          <ManagerFooter selection={selection} onClose={onClose} />
        </section>
      </div>
      <DeleteDialogView deleteDialog={deleteDialog} />
    </>
  );
}
