import { apiFetch } from "./http";
import { normalizeExecution, normalizeRun } from "./canvases";
import type { CanvasRunDetail, CanvasRunEventBatch } from "../canvas/runRealtime";

export function getCanvasRunEventBatch(
  canvasId: string, runId: string, after: number, signal: AbortSignal,
): Promise<CanvasRunEventBatch> {
  return apiFetch<CanvasRunEventBatch>(
    `/canvases/${encodeURIComponent(canvasId)}/runs/${encodeURIComponent(runId)}/event-batch?after_seq=${after}&limit=100`,
    { signal },
  );
}
export async function getCanvasRunDetail(
  canvasId: string, runId: string, signal: AbortSignal,
): Promise<CanvasRunDetail> {
  const raw = await apiFetch<Record<string, unknown>>(
    `/canvases/${encodeURIComponent(canvasId)}/runs/${encodeURIComponent(runId)}`, { signal },
  );
  if (!raw || typeof raw !== "object" || !Array.isArray(raw.executions)) {
    throw new TypeError("Malformed canvas run detail");
  }
  return { run: normalizeRun(raw), executions: raw.executions.map(normalizeExecution) };
}
