import { apiFetch } from "./http";
import { normalizeExecution, normalizeRun } from "./canvases";
import { idempotentPostRequest, isAmbiguousRequestFailure, semanticPostIdempotency } from "./semanticIdempotency";
import { getPrivateIdentitySnapshot } from "../auth/privateIdentityEpoch";
import { CanvasPlanIntentClient } from "../canvas/runPlanIntentClient";
import { assertCanvasPlanInput, validateCanvasPlanPreview } from "../canvas/runPlanValidation";
import type { CanvasPendingPlanIntent, CanvasPlanInput, CanvasPlanPreview, CanvasPlanReceipt, CanvasPlanRunDetail } from "../canvas/runPlanTypes";

const base = (id: string) => `/canvases/${encodeURIComponent(id)}`;
export async function previewCanvasPlan(canvasId: string, input: CanvasPlanInput, signal?: AbortSignal) {
  assertCanvasPlanInput(input);
  const result = await apiFetch<CanvasPlanPreview>(`${base(canvasId)}/plans/preview`, { method: "POST", body: JSON.stringify(input), signal });
  return validateCanvasPlanPreview(result, canvasId, input);
}
function normalizeDetail(value: CanvasPlanRunDetail, canvasId: string): CanvasPlanRunDetail {
  if (!value?.id || value.canvas_id !== canvasId || !Array.isArray(value.executions)) throw new Error("运行状态响应不完整");
  return { ...normalizeRun(value), canvas_id: value.canvas_id, kind: value.kind,
    summary: value.summary ?? {}, executions: value.executions.map(execution => ({
      ...normalizeExecution(execution), attempt: Number.isSafeInteger(execution.attempt) ? execution.attempt : 0,
    })) };
}
export async function getCanvasPlanRun(canvasId: string, runId: string, signal?: AbortSignal) {
  return normalizeDetail(await apiFetch<CanvasPlanRunDetail>(`${base(canvasId)}/runs/${encodeURIComponent(runId)}`, { signal }), canvasId);
}
export async function listCanvasPlanRuns(canvasId: string, signal?: AbortSignal) {
  const data = await apiFetch<{ items: Array<{ id: string; kind: string; status: string }> }>(`${base(canvasId)}/runs?limit=30`, { signal });
  return data.items.filter(run => run.kind !== "single");
}
async function query(pending: CanvasPendingPlanIntent): Promise<CanvasPlanReceipt> {
  const suffix = pending.intent.kind === "repair" ? `?run_id=${encodeURIComponent(pending.intent.run_id)}` : "";
  const data = await apiFetch<CanvasPlanReceipt>(`${base(pending.canvasId)}/plans/intents/${encodeURIComponent(pending.key)}${suffix}`);
  if (typeof data?.admitted !== "boolean" || data.admitted && !data.run) throw new Error("提交状态响应不完整");
  return { admitted: data.admitted, run: data.run ? normalizeDetail(data.run, pending.canvasId) : null };
}
async function post(pending: CanvasPendingPlanIntent) {
  const intent = pending.intent;
  const path = intent.kind === "start" ? "/plans/run" : `/runs/${encodeURIComponent(intent.run_id)}/retry-failed`;
  const body = { ...intent.body, idempotency_key: pending.key };
  return normalizeDetail(await apiFetch<CanvasPlanRunDetail>(`${base(pending.canvasId)}${path}`, idempotentPostRequest(body)), pending.canvasId);
}
export const canvasPlanIntents = new CanvasPlanIntentClient({
  leases: semanticPostIdempotency,
  journal: () => window.localStorage,
  currentIdentity: () => {
    const identity = getPrivateIdentitySnapshot();
    if (!identity.userId) throw new Error("请等待登录身份确认");
    return identity.userId;
  },
  identityEpoch: () => getPrivateIdentitySnapshot().epoch,
  query, post, ambiguous: isAmbiguousRequestFailure,
});
