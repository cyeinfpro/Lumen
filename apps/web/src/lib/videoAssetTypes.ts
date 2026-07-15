import type { VideoProviderKind } from "./types";

export type VideoAssetType = "Image" | "Video";
export type VideoAssetStatus = "Active" | "Processing" | "Failed";

export type VideoAssetCapabilityReason =
  | "ready"
  | "not_volcano_official"
  | "missing_credentials"
  | "missing_project_name"
  | "public_base_url_unavailable"
  | "unavailable";

export interface VideoAssetQuotaLimitsOut {
  max_assets: number;
  max_asset_groups: number;
  create_asset_qpm: number;
  create_asset_window_seconds: number;
}

export interface VideoAssetQuotaUsageOut {
  assets_used: number;
  asset_groups_used: number;
}

export interface VideoAssetCapabilitiesOut {
  ready: boolean;
  reason: VideoAssetCapabilityReason;
  detail?: string | null;
  provider_kind?: VideoProviderKind | string | null;
  provider_name?: string | null;
  project_name?: string | null;
  region?: string | null;
  public_base_url?: string | null;
  missing_fields?: string[];
  quotas: VideoAssetQuotaLimitsOut;
}

export interface VideoAssetGroupOut {
  id: string;
  name: string;
  title: string;
  description: string;
  group_type: "AIGC" | string;
  project_name: string;
  create_time?: string | null;
  update_time?: string | null;
}

export interface VideoAssetGroupListOut {
  items: VideoAssetGroupOut[];
  total_count: number;
  page_number: number;
  page_size: number;
}

export interface VideoAssetGroupCreateIn {
  name: string;
  description?: string;
}

export interface VideoAssetGroupPatchIn {
  name?: string;
  description?: string;
}

export interface VideoAssetOut {
  id: string;
  group_id: string;
  name: string;
  asset_type: VideoAssetType;
  status: VideoAssetStatus | string;
  url?: string | null;
  project_name: string;
  create_time?: string | null;
  update_time?: string | null;
  error_code?: string | null;
  error_message?: string | null;
}

export interface VideoAssetOperationErrorOut {
  code: string;
  message: string;
  retryable: boolean;
  retry_after_seconds?: number | null;
}

export type VideoAssetOperationAction =
  | "create_group"
  | "update_group"
  | "delete_group"
  | "create_asset"
  | "update_asset"
  | "delete_asset";

export interface VideoAssetDeleteResultOut {
  id: string;
  deleted: boolean;
  resource_type?: "group" | "asset" | null;
  group_id?: string | null;
  asset_id?: string | null;
  deleted_asset_ids?: string[];
  cascade_assets?: boolean;
  already_deleted?: boolean;
}

export type VideoAssetOperationResult =
  VideoAssetGroupOut | VideoAssetOut | VideoAssetDeleteResultOut;

export interface VideoAssetOperationOut {
  id: string;
  action: VideoAssetOperationAction | string;
  status: string;
  progress_stage: string;
  attempt: number;
  delivery_generation: number;
  retryable: boolean;
  retry_after_seconds?: number | null;
  result?: VideoAssetOperationResult | null;
  error?: VideoAssetOperationErrorOut | null;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
}

export interface VideoAssetListOut {
  items: VideoAssetOut[];
  total_count: number;
  page_number: number;
  page_size: number;
}

export interface VideoAssetCreateIn {
  group_id: string;
  name: string;
  image_id?: string;
  video_id?: string;
}

export interface VideoAssetPatchIn {
  name: string;
}
