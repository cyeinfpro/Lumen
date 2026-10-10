import type { CanvasAssetDescriptor, CanvasOutput } from "./types";

type RecordValue = Record<string, unknown>;
const STATES = new Set(["pending", "preparing", "ready", "failed", "unavailable"]);

function record(value: unknown): RecordValue {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as RecordValue : {};
}
function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}
function number(value: unknown, positive = false): number | null {
  return typeof value === "number" && Number.isFinite(value) &&
    (positive ? value > 0 : value >= 0) ? value : null;
}
function locator(value: unknown): string | null {
  return typeof value === "string" && value.startsWith("/api/") &&
    !/[\\\u0000-\u0020]/.test(value) ? value : null;
}

// These response-only descriptors never become graph config or URL identities.
export function normalizeCanvasAssets(value: unknown): CanvasAssetDescriptor[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const result: CanvasAssetDescriptor[] = [];
  for (const item of value) {
    const raw = record(item);
    const id = text(raw.asset_id);
    if (raw.schema_version !== 1 || !id ||
      (raw.kind !== "image" && raw.kind !== "video") ||
      typeof raw.preparation_state !== "string" || !STATES.has(raw.preparation_state)) continue;
    const locators = record(raw.locators);
    result.push({
      schema_version: 1, asset_id: id, kind: raw.kind,
      source_sha256: text(raw.source_sha256) ?? "",
      mime: text(raw.mime) ?? "",
      size_bytes: number(raw.size_bytes),
      width: number(raw.width, true), height: number(raw.height, true),
      duration_ms: number(raw.duration_ms, true),
      preparation_state: raw.preparation_state as CanvasAssetDescriptor["preparation_state"],
      preparation_revision: Number.isSafeInteger(raw.preparation_revision)
        ? number(raw.preparation_revision) : null,
      updated_at: text(raw.updated_at),
      locators: { original: locator(locators.original),
        preview: locator(locators.preview), thumb: locator(locators.thumb) },
    });
  }
  return result;
}

export function assetKey(asset: Pick<CanvasAssetDescriptor, "kind" | "asset_id">): string {
  return JSON.stringify([asset.kind, asset.asset_id]);
}

export function mergeCanvasAssets(
  current: CanvasAssetDescriptor[] | undefined,
  incoming: CanvasAssetDescriptor[] | undefined,
): CanvasAssetDescriptor[] | undefined {
  if (!incoming) return current;
  const previous = new Map((current ?? []).map((asset) => [assetKey(asset), asset]));
  return incoming.map((asset) => {
    const old = previous.get(assetKey(asset));
    // A new content hash may legitimately restart preparation at revision 0.
    return old && old.source_sha256 === asset.source_sha256 &&
      old.preparation_revision !== null &&
      (asset.preparation_revision === null || old.preparation_revision > asset.preparation_revision)
      ? old : asset;
  });
}

export function projectCanvasAsset(
  output: CanvasOutput, assets: ReadonlyMap<string, CanvasAssetDescriptor>,
): CanvasOutput {
  const id = output.type === "image" ? output.image_id : output.video_id;
  const asset = id ? assets.get(assetKey({ kind: output.type, asset_id: id })) : undefined;
  if (!asset) return output; // Missing descriptor is not evidence of deletion or denial.
  return {
    ...output,
    width: asset.width, height: asset.height, duration_ms: asset.duration_ms,
    source_sha256: asset.source_sha256,
    preparation_state: asset.preparation_state,
    preparation_revision: asset.preparation_revision,
    url: asset.locators.original ?? output.url,
    thumbnail_url: asset.locators.thumb,
    // Video preview locators are binary routes, never image/poster sources.
    preview_url: output.type === "image" ? asset.locators.preview ?? output.preview_url : null,
    poster_url: output.type === "video" ? asset.locators.thumb : output.poster_url,
  };
}

export function hasPreparingCanvasAssets(assets: CanvasAssetDescriptor[] | undefined): boolean {
  return assets?.some((asset) => asset.preparation_state === "pending" ||
    asset.preparation_state === "preparing") ?? false;
}
