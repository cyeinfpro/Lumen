import { apiFetch } from "./http";
import type {
  VideoAssetCapabilitiesOut,
  VideoAssetCreateIn,
  VideoAssetGroupCreateIn,
  VideoAssetGroupListOut,
  VideoAssetGroupPatchIn,
  VideoAssetListOut,
  VideoAssetOperationOut,
  VideoAssetOut,
  VideoAssetPatchIn,
  VideoAssetQuotaUsageOut,
} from "../videoAssetTypes";

const VIDEO_ASSETS_BASE = "/video-assets";
export const DEFAULT_VIDEO_ASSET_QUOTAS = {
  max_assets: 50,
  max_asset_groups: 50,
  create_asset_qpm: 3,
  create_asset_window_seconds: 60,
} as const;

type VideoAssetCapabilitiesApiOut = Partial<VideoAssetCapabilitiesOut> & {
  available?: boolean;
  configured?: boolean;
  supported?: boolean;
  enabled?: boolean;
  code?: string | null;
  reason_code?: string | null;
  message?: string | null;
  missing?: string[];
};

function includesAny(value: string, candidates: string[]): boolean {
  return candidates.some((candidate) => value.includes(candidate));
}

function videoAssetProviderIsNotOfficial(
  payload: VideoAssetCapabilitiesApiOut,
  raw: string,
): boolean {
  return (
    includesAny(raw, [
      "not_volcano",
      "provider_not_official",
      "not_official",
      "unsupported_provider",
    ]) || Boolean(payload.provider_kind && payload.provider_kind !== "volcano")
  );
}

function videoAssetCredentialsAreMissing(
  raw: string,
  missing: Set<string>,
): boolean {
  return (
    includesAny(raw, ["credential", "access_key", "ak_sk"]) ||
    missing.has("access_key_id") ||
    missing.has("secret_access_key")
  );
}

function normalizeVideoAssetCapabilityReason(
  payload: VideoAssetCapabilitiesApiOut,
): VideoAssetCapabilitiesOut["reason"] {
  const raw = String(
    payload.reason ?? payload.reason_code ?? payload.code ?? "",
  )
    .trim()
    .toLowerCase();
  const missing = new Set(
    [...(payload.missing_fields ?? []), ...(payload.missing ?? [])].map(
      (item) => item.trim().toLowerCase(),
    ),
  );
  if (videoAssetProviderIsNotOfficial(payload, raw)) {
    return "not_volcano_official";
  }
  if (videoAssetCredentialsAreMissing(raw, missing)) {
    return "missing_credentials";
  }
  if (raw.includes("project") || missing.has("project_name")) {
    return "missing_project_name";
  }
  if (
    raw.includes("public") ||
    raw.includes("base_url") ||
    missing.has("public_base_url")
  ) {
    return "public_base_url_unavailable";
  }
  return "unavailable";
}

function normalizeVideoAssetQuotas(
  quotas: VideoAssetCapabilitiesApiOut["quotas"],
): VideoAssetCapabilitiesOut["quotas"] {
  return {
    max_assets: quotas?.max_assets ?? DEFAULT_VIDEO_ASSET_QUOTAS.max_assets,
    max_asset_groups:
      quotas?.max_asset_groups ?? DEFAULT_VIDEO_ASSET_QUOTAS.max_asset_groups,
    create_asset_qpm:
      quotas?.create_asset_qpm ?? DEFAULT_VIDEO_ASSET_QUOTAS.create_asset_qpm,
    create_asset_window_seconds:
      quotas?.create_asset_window_seconds ??
      DEFAULT_VIDEO_ASSET_QUOTAS.create_asset_window_seconds,
  };
}

function normalizeVideoAssetCapabilities(
  payload: VideoAssetCapabilitiesApiOut,
): VideoAssetCapabilitiesOut {
  const ready = Boolean(
    payload.ready ??
    payload.available ??
    payload.configured ??
    payload.supported ??
    payload.enabled,
  );
  return {
    ready,
    reason: ready ? "ready" : normalizeVideoAssetCapabilityReason(payload),
    detail: payload.detail ?? payload.message ?? null,
    provider_kind: payload.provider_kind ?? null,
    provider_name: payload.provider_name ?? null,
    project_name: payload.project_name ?? null,
    region: payload.region ?? null,
    public_base_url: payload.public_base_url ?? null,
    missing_fields: payload.missing_fields ?? payload.missing ?? [],
    quotas: normalizeVideoAssetQuotas(payload.quotas),
  };
}

export async function getVideoAssetCapabilities(
  model: string,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetCapabilitiesOut> {
  const query = new URLSearchParams({ model });
  const payload = await apiFetch<VideoAssetCapabilitiesApiOut>(
    `${VIDEO_ASSETS_BASE}/capabilities?${query.toString()}`,
    { signal: opts.signal },
  );
  return normalizeVideoAssetCapabilities(payload);
}

export function getVideoAssetUsage(
  model: string,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetQuotaUsageOut> {
  const query = new URLSearchParams({ model });
  return apiFetch<VideoAssetQuotaUsageOut>(
    `${VIDEO_ASSETS_BASE}/usage?${query.toString()}`,
    { signal: opts.signal },
  );
}

export interface ListVideoAssetGroupsOptions {
  model: string;
  name?: string;
  group_ids?: string[];
  page_number?: number;
  page_size?: number;
  sort_by?: string;
  sort_order?: "asc" | "desc";
  signal?: AbortSignal;
}

export function listVideoAssetGroups(
  opts: ListVideoAssetGroupsOptions,
): Promise<VideoAssetGroupListOut> {
  const query = new URLSearchParams({ model: opts.model });
  if (opts.name) query.set("name", opts.name);
  for (const groupId of opts.group_ids ?? []) {
    query.append("group_ids", groupId);
  }
  if (opts.page_number != null) {
    query.set("page_number", String(opts.page_number));
  }
  if (opts.page_size != null) query.set("page_size", String(opts.page_size));
  if (opts.sort_by) query.set("sort_by", opts.sort_by);
  if (opts.sort_order) query.set("sort_order", opts.sort_order);
  return apiFetch<VideoAssetGroupListOut>(
    `${VIDEO_ASSETS_BASE}/groups?${query.toString()}`,
    { signal: opts.signal },
  );
}

export function createVideoAssetGroup(
  model: string,
  body: VideoAssetGroupCreateIn,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  const query = new URLSearchParams({ model });
  return apiFetch<VideoAssetOperationOut>(
    `${VIDEO_ASSETS_BASE}/groups?${query.toString()}`,
    {
      method: "POST",
      body: JSON.stringify(body),
      signal: opts.signal,
    },
  );
}

export function patchVideoAssetGroup(
  groupId: string,
  model: string,
  body: VideoAssetGroupPatchIn,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  const query = new URLSearchParams({ model });
  return apiFetch<VideoAssetOperationOut>(
    `${VIDEO_ASSETS_BASE}/groups/${encodeURIComponent(groupId)}?${query.toString()}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
      signal: opts.signal,
    },
  );
}

export function deleteVideoAssetGroup(
  model: string,
  groupId: string,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  const query = new URLSearchParams({ model });
  return apiFetch<VideoAssetOperationOut>(
    `${VIDEO_ASSETS_BASE}/groups/${encodeURIComponent(groupId)}?${query.toString()}`,
    { method: "DELETE", signal: opts.signal },
  );
}

export interface ListVideoAssetsOptions {
  model: string;
  name?: string;
  group_ids?: string[];
  asset_types?: Array<"Image" | "Video">;
  statuses?: Array<"Active" | "Processing" | "Failed">;
  page_number?: number;
  page_size?: number;
  sort_by?: string;
  sort_order?: "asc" | "desc";
  signal?: AbortSignal;
}

export async function listVideoAssets(
  opts: ListVideoAssetsOptions,
): Promise<VideoAssetListOut> {
  const query = new URLSearchParams({ model: opts.model });
  if (opts.name) query.set("name", opts.name);
  for (const groupId of opts.group_ids ?? []) {
    query.append("group_ids", groupId);
  }
  for (const assetType of opts.asset_types ?? []) {
    query.append("asset_types", assetType);
  }
  for (const status of opts.statuses ?? []) {
    query.append("statuses", status);
  }
  if (opts.page_number != null) {
    query.set("page_number", String(opts.page_number));
  }
  if (opts.page_size != null) query.set("page_size", String(opts.page_size));
  if (opts.sort_by) query.set("sort_by", opts.sort_by);
  if (opts.sort_order) query.set("sort_order", opts.sort_order);
  return apiFetch<VideoAssetListOut>(
    `${VIDEO_ASSETS_BASE}/assets?${query.toString()}`,
    { signal: opts.signal },
  );
}

export function getVideoAsset(
  assetId: string,
  model: string,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOut> {
  const query = new URLSearchParams({ model });
  return apiFetch<VideoAssetOut>(
    `${VIDEO_ASSETS_BASE}/assets/${encodeURIComponent(assetId)}?${query.toString()}`,
    { signal: opts.signal },
  );
}

export function createVideoAsset(
  model: string,
  body: VideoAssetCreateIn,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  const query = new URLSearchParams({ model });
  return apiFetch<VideoAssetOperationOut>(
    `${VIDEO_ASSETS_BASE}/assets?${query.toString()}`,
    {
      method: "POST",
      body: JSON.stringify(body),
      signal: opts.signal,
    },
  );
}

export function patchVideoAsset(
  assetId: string,
  model: string,
  body: VideoAssetPatchIn,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  const query = new URLSearchParams({ model });
  return apiFetch<VideoAssetOperationOut>(
    `${VIDEO_ASSETS_BASE}/assets/${encodeURIComponent(assetId)}?${query.toString()}`,
    {
      method: "PATCH",
      body: JSON.stringify(body),
      signal: opts.signal,
    },
  );
}

export function deleteVideoAsset(
  assetId: string,
  model: string,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  const query = new URLSearchParams({ model });
  return apiFetch<VideoAssetOperationOut>(
    `${VIDEO_ASSETS_BASE}/assets/${encodeURIComponent(assetId)}?${query.toString()}`,
    { method: "DELETE", signal: opts.signal },
  );
}

export function getVideoAssetOperation(
  operationId: string,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  return apiFetch<VideoAssetOperationOut>(
    `${VIDEO_ASSETS_BASE}/operations/${encodeURIComponent(operationId)}`,
    { signal: opts.signal },
  );
}

export function retryVideoAssetOperation(
  operationId: string,
  opts: { signal?: AbortSignal } = {},
): Promise<VideoAssetOperationOut> {
  return apiFetch<VideoAssetOperationOut>(
    `${VIDEO_ASSETS_BASE}/operations/${encodeURIComponent(operationId)}/retry`,
    { method: "POST", signal: opts.signal },
  );
}
