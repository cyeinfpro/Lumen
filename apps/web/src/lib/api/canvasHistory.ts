import { apiFetch } from "./http";
import { normalizeExecution } from "./canvases";
import { idempotentPostRequest, withSemanticPostIdempotency } from "./semanticIdempotency";
import { normalizeCanvasAssets } from "../canvas/assets";
import { CANVAS_HISTORY_PAGE_SIZE, historyRecord } from "../canvas/executionHistory";
import type { CanvasAssetDescriptor, CanvasExecutionHistoryPage, CanvasHistoricalExecution } from "../canvas/types";

export async function getCanvasExecutionHistory(canvasId: string, nodeId: string, cursor: string | null, signal?: AbortSignal): Promise<CanvasExecutionHistoryPage> {
  const query = new URLSearchParams({ limit: String(CANVAS_HISTORY_PAGE_SIZE) });
  if (cursor) query.set("cursor", cursor);
  const value = historyRecord(await apiFetch<unknown>(
    `/canvases/${encodeURIComponent(canvasId)}/nodes/${encodeURIComponent(nodeId)}/history?${query}`, { signal },
  ));
  if (!value || !Array.isArray(value.items) || value.items.length > CANVAS_HISTORY_PAGE_SIZE ||
    (value.next_cursor != null && typeof value.next_cursor !== "string")) throw new TypeError("无效的历史分页响应");
  const items = value.items.map((item): CanvasHistoricalExecution => {
    const raw = historyRecord(item);
    const execution = normalizeExecution(item);
    if (!raw || !execution.id || execution.node_id !== nodeId) throw new TypeError("历史响应与所选节点不一致");
    const text = (key: string) => typeof raw[key] === "string" && raw[key] ? raw[key] as string : null;
    return { ...execution, config_snapshot: historyRecord(raw.config_snapshot), input_snapshot: historyRecord(raw.input_snapshot),
      definition_hash: text("definition_hash"), input_hash: text("input_hash"), processor_version: text("processor_version") };
  });
  return { items, next_cursor: typeof value.next_cursor === "string" && value.next_cursor ? value.next_cursor : null };
}

export function retryCanvasVideoPreparation(asset: CanvasAssetDescriptor): Promise<CanvasAssetDescriptor> {
  if (asset.kind !== "video" || asset.preparation_state !== "failed" ||
    !/^[a-f0-9]{64}$/.test(asset.source_sha256) || !Number.isSafeInteger(asset.preparation_revision) ||
    asset.preparation_revision === null || asset.preparation_revision < 0) {
    return Promise.reject(new Error("缺少可靠的素材准备版本，请先刷新状态"));
  }
  const body = { expected_source_sha256: asset.source_sha256, expected_preparation_revision: asset.preparation_revision };
  return withSemanticPostIdempotency({ operation: "canvas.video.preparation.retry", assetId: asset.asset_id }, body, async (key) => {
    const raw = historyRecord(await apiFetch<unknown>(`/videos/${encodeURIComponent(asset.asset_id)}/preparation/retry`,
      idempotentPostRequest({ ...body, idempotency_key: key })));
    const result = normalizeCanvasAssets([raw?.asset])?.[0];
    if (!result || result.asset_id !== asset.asset_id || result.source_sha256 !== asset.source_sha256) throw new TypeError("素材准备响应不匹配");
    return result;
  });
}
