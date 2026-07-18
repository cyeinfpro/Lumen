import type {
  VideoAssetGroupOut,
  VideoAssetOperationAction,
  VideoAssetOperationOut,
  VideoAssetOperationResult,
  VideoAssetOut,
  VideoAssetStatus,
  VideoAssetType,
} from "@/lib/types";

import type {
  VolcanoManagedOperationPhase,
  VolcanoOperationRecoveryMode,
  VolcanoUploadRetryMode,
} from "./volcano-asset-domain";

export type VolcanoAssetSelection = {
  id: string;
  name: string;
  asset_type: "Image" | "Video";
  url?: string | null;
  preview_url?: string | null;
  status: string;
  group_id: string;
};

export type VolcanoAssetManagerProps = {
  open: boolean;
  model: string;
  remainingLimits: { image: number; video: number };
  existingAssetIds: Set<string> | string[];
  onClose: () => void;
  onUse: (assets: VolcanoAssetSelection[]) => void;
  onDeleted: (assetIds: string[]) => void;
};

export type AssetTypeFilter = "all" | VideoAssetType;
export type AssetStatusFilter = "all" | VideoAssetStatus;

export type UploadPhase =
  | "queued"
  | "uploading"
  | "optimizing"
  | "waiting_quota"
  | "processing"
  | "needs_refresh"
  | "ready"
  | "failed";

export type UploadItem = {
  id: string;
  model: string;
  groupId: string;
  file: File | null;
  fileName: string;
  fileSize: number;
  fileLastModified: number;
  assetType: VideoAssetType;
  name: string;
  phase: UploadPhase;
  imageId?: string;
  videoId?: string;
  assetId?: string;
  clientOperationId?: string;
  operationId?: string;
  operationStatus?: string;
  progressStage?: string;
  submissionStartedAt?: number;
  operationStartedAt?: number;
  operationRetryable?: boolean;
  retryAfterSeconds?: number | null;
  retryAvailableAt?: number;
  pollFailures?: number;
  retryMode: VolcanoUploadRetryMode;
  quotaReserved: boolean;
  quotaReservationTarget: number;
  error?: string;
};

export type GroupFormState = {
  mode: "create" | "rename";
  groupId?: string;
  name: string;
  description: string;
};

export type DeleteTarget =
  | { kind: "group"; group: VideoAssetGroupOut }
  | { kind: "asset"; asset: VideoAssetOut };

export type Notice = {
  tone: "error" | "status";
  text: string;
};

export type OperationItem = {
  id: string;
  model: string;
  action: VideoAssetOperationAction;
  remoteOperationId?: string;
  lockKey: string;
  title: string;
  pendingLabel: string;
  phase: VolcanoManagedOperationPhase;
  recovery: VolcanoOperationRecoveryMode;
  submissionStartedAt?: number;
  operationStartedAt?: number;
  progressStage?: string;
  retryAfterSeconds?: number | null;
  retryAvailableAt?: number;
  pollFailures: number;
  retryable: boolean;
  blocksChildren?: boolean;
  error?: string;
};

export type OperationRunner = {
  model: string;
  lockKey: string;
  sessionId: number;
  prepare?: (signal: AbortSignal) => Promise<void>;
  submit: (signal: AbortSignal) => Promise<VideoAssetOperationOut>;
  onProgress?: (
    operation: VideoAssetOperationOut,
    sessionId: number,
    operationStartedAt: number,
  ) => void;
  onSucceeded: (
    result: VideoAssetOperationResult,
    operation: VideoAssetOperationOut,
    sessionId: number,
  ) => Promise<void> | void;
  onFailed?: (operation: VideoAssetOperationOut, sessionId: number) => void;
  onSubmissionFailed?: (error: unknown, sessionId: number) => void;
  onUncertain?: (message: string, sessionId: number) => void;
  verifyUnknown?: (signal: AbortSignal, sessionId: number) => Promise<boolean>;
};

export type ActiveSession = {
  id: number;
  open: boolean;
  model: string;
};

export type AssetViewSnapshot = {
  capabilityReady: boolean;
  groupId: string | null;
  search: string;
  status: AssetStatusFilter;
  type: AssetTypeFilter;
  page: number;
};

export type UploadCreateRetryDecision =
  | "recreate"
  | "retire_and_recreate"
  | "preserve"
  | "blocked";

export const ASSET_PAGE_SIZE = 40;
export const GROUP_PAGE_SIZE = 100;
export const DELETE_SCAN_PAGE_SIZE = 100;
export const POLL_INTERVAL_MS = 4_000;
export const MAX_UPLOAD_CONCURRENCY = 2;
