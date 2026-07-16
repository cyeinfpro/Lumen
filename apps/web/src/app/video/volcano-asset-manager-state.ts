import type { VideoAssetOut } from "@/lib/types";

import type {
  AssetStatusFilter,
  AssetTypeFilter,
  AssetViewSnapshot,
  OperationItem,
  UploadCreateRetryDecision,
  UploadItem,
  VolcanoAssetSelection,
} from "./volcano-asset-manager-types";

export function assetSelection(
  asset: VideoAssetOut,
): VolcanoAssetSelection {
  return {
    id: asset.id,
    name: asset.name || "未命名素材",
    asset_type: asset.asset_type,
    url: asset.url ?? null,
    preview_url: asset.preview_url ?? null,
    status: asset.status,
    group_id: asset.group_id,
  };
}

export function uploadBlocksGroupMutation(item: UploadItem): boolean {
  return (
    item.phase === "queued" ||
    item.phase === "uploading" ||
    item.phase === "optimizing" ||
    item.phase === "waiting_quota" ||
    item.phase === "processing" ||
    item.phase === "needs_refresh"
  );
}

export function uploadNameIsEditable(item: UploadItem): boolean {
  return item.phase === "queued" || item.phase === "failed";
}

export function uploadCanBeRemoved(item: UploadItem): boolean {
  return (
    item.phase === "queued" || item.phase === "ready" || item.phase === "failed"
  );
}

export function assetViewMatches(
  current: AssetViewSnapshot,
  requested: AssetViewSnapshot,
): boolean {
  return (
    current.capabilityReady === requested.capabilityReady &&
    current.groupId === requested.groupId &&
    current.search === requested.search &&
    current.status === requested.status &&
    current.type === requested.type &&
    current.page === requested.page
  );
}

export function pauseUploadQueue(items: UploadItem[]): UploadItem[] {
  let changed = false;
  const paused = items.map((item) => {
    if (item.phase === "uploading" || item.phase === "waiting_quota") {
      changed = true;
      return {
        ...item,
        phase: "queued" as const,
        error: undefined,
      };
    }
    if (item.phase === "optimizing") {
      changed = true;
      return {
        ...item,
        phase: "needs_refresh" as const,
        retryMode: "refresh" as const,
        error:
          "弹窗关闭或模型切换时提交尚未确认。重新打开后请先检查状态，系统不会自动重复创建。",
      };
    }
    return item;
  });
  return changed ? paused : items;
}

export function possibleSubmittedAssets(
  items: VideoAssetOut[],
  upload: UploadItem,
): VideoAssetOut[] {
  const startedAt = upload.submissionStartedAt ?? 0;
  return items.filter((asset) => {
    if (
      asset.group_id !== upload.groupId ||
      asset.name.trim() !== upload.name.trim()
    ) {
      return false;
    }
    const timestamp = Date.parse(asset.create_time || asset.update_time || "");
    return (
      startedAt <= 0 ||
      (Number.isFinite(timestamp) && timestamp >= startedAt - 120_000)
    );
  });
}

export function mergeUniqueAssetPage(
  current: VideoAssetOut[],
  pageItems: VideoAssetOut[],
  limit: number,
): { items: VideoAssetOut[]; added: number } {
  const seenIds = new Set(current.map((item) => item.id));
  const additions: VideoAssetOut[] = [];
  for (const item of pageItems) {
    if (!item.id || seenIds.has(item.id)) continue;
    seenIds.add(item.id);
    additions.push(item);
    if (current.length + additions.length >= limit) break;
  }
  return {
    items: additions.length > 0 ? [...current, ...additions] : current,
    added: additions.length,
  };
}

export function assetListRequest(
  view: AssetViewSnapshot,
  pageSize: number,
): {
  name?: string;
  statuses?: Exclude<AssetStatusFilter, "all">[];
  asset_types?: Exclude<AssetTypeFilter, "all">[];
  page_number: number;
  page_size: number;
} {
  return {
    name: view.search || undefined,
    statuses: view.status === "all" ? undefined : [view.status],
    asset_types: view.type === "all" ? undefined : [view.type],
    page_number: view.page,
    page_size: pageSize,
  };
}

export function uploadCreateRetryDecision(
  item: Pick<UploadItem, "clientOperationId" | "retryMode">,
  operation?: Pick<OperationItem, "phase" | "recovery">,
): UploadCreateRetryDecision {
  if (item.retryMode !== "create") return "preserve";
  if (!item.clientOperationId) return "recreate";
  if (!operation) return "retire_and_recreate";
  if (operation.phase === "failed" && operation.recovery === "none") {
    return "retire_and_recreate";
  }
  return "blocked";
}

export function prepareUploadForCreateRetry(item: UploadItem): UploadItem {
  return {
    ...item,
    phase: "failed",
    clientOperationId: undefined,
    operationId: undefined,
    operationStatus: undefined,
    progressStage: undefined,
    submissionStartedAt: undefined,
    operationStartedAt: undefined,
    operationRetryable: false,
    retryAfterSeconds: null,
    retryAvailableAt: undefined,
    pollFailures: 0,
    retryMode: "create",
    quotaReserved: false,
    error: undefined,
  };
}
