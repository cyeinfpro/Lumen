import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
const code = ts.transpileModule(readFileSync(new URL("../src/lib/canvas/generationDetails.ts", import.meta.url), "utf8"),
 { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const compiled = { exports: {} };
new Function("module", "exports", code)(compiled, compiled.exports);
const { normalizeCanvasTaskRecovery: recovery, normalizeCanvasBilling: billing,
 formatCanvasMicroAmount: format, canvasBillingRows: rows, canvasTaskCancelTarget: cancel,
 canvasRecoveryLabel: label, canvasRecoveryExplanation: explanation } = compiled.exports;
const caps = { state: "running", can_query: true, can_cancel: true, can_generate_new: true, automatic_resubmit: false };
test("Unknown submit never enables a new candidate even with inconsistent remote flags", () => {
 const value = recovery({ ...caps, state: "submission_unknown" });
 assert.equal(value.can_generate_new, false); assert.equal(value.can_query, true);
 assert.equal(label(value), "提交状态待确认"); assert.match(explanation(value), /避免重复提交和扣费/);
});
test("Saving artifact exposes its own state and does not offer generation recovery", () => {
 const value = recovery({ ...caps, state: "saving_artifact" });
 assert.equal(value.can_generate_new, false); assert.equal(label(value), "保存成品中");
 assert.match(explanation(value), /不会重新生成/);
});
test("Cancel requested never offers another cancellation or generation", () => {
 const value = recovery({ ...caps, state: "cancel_requested" });
 assert.equal(value.can_cancel, false); assert.equal(value.can_generate_new, false);
});
test("Only explicit terminal recovery permits a separately requested new candidate", () => {
 for (const state of ["succeeded", "failed", "canceled", "expired"]) {
  const value = recovery({ ...caps, state });
  assert.equal(value.can_generate_new, true); assert.equal(value.can_cancel, false);
  assert.equal(recovery({ ...caps, state, can_generate_new: false }).can_generate_new, false);
 }
});
test("Legacy, malformed, unknown states or automatic-resubmit claims fail closed", () => {
 for (const value of [null, {}, [], caps.state, { ...caps, automatic_resubmit: true },
  { ...caps, state: "new_server_state" }, { ...caps, can_cancel: "true" }]) assert.equal(recovery(value), undefined);
});
test("Task cancellation uses the exact generation owner and never the generic task ID", () => {
 const value = recovery(caps);
 assert.deepEqual(cancel({ id: "task", kind: "generation", generation_id: "image-owner", recovery: value }), { kind: "generation", id: "image-owner" });
 assert.deepEqual(cancel({ kind: "video_generation", video_generation_id: "video-owner", recovery: value }), { kind: "video_generation", id: "video-owner" });
 assert.equal(cancel({ id: "task", kind: "generation", recovery: value }), null);
 assert.equal(cancel({ kind: "completion", generation_id: "image-owner", recovery: value }), null);
 assert.equal(cancel({ kind: "generation", generation_id: "image-owner" }), null);
});
test("Missing billing stays absent and foreign currency is not relabelled", () => {
 for (const value of [null, {}, { currency: "USD", source: "wallet_ledger" }, { currency: "CNY", source: "guess" }]) assert.equal(billing(value), undefined);
});
test("Missing estimate/reserve/actual remains null while authoritative zero stays zero", () => {
 const value = billing({ currency: "CNY", source: "wallet_ledger", actual_cost_micro: 0 });
 assert.equal(value.estimated_cost_micro, null); assert.equal(value.reserved_micro, null);
 assert.equal(value.actual_cost_micro, 0);
 assert.deepEqual(rows(value), [["预估费用", "待确认"], ["当前预留", "待确认"], ["实际结算", "¥0"]]);
});
test("Negative, fractional, infinite and unsafe micro amounts are never shown as a cost", () => {
 for (const amount of [-1, 0.5, Infinity, NaN, Number.MAX_SAFE_INTEGER + 1, "100"]) {
  assert.equal(billing({ currency: "CNY", source: "wallet_ledger", actual_cost_micro: amount }).actual_cost_micro, null);
  assert.equal(format(amount), "待确认");
 }
});
test("Aggregate partial cost is labelled a subtotal, never a complete settlement", () => {
 const value = billing({ currency: "CNY", source: "task_ledger_aggregate", task_count: 2,
  estimated_cost_micro: null, known_estimated_cost_micro: 1_000_001, known_actual_cost_micro: 0 });
 assert.equal(value.task_count, 2);
 assert.deepEqual(rows(value), [["预估费用", "已知小计 ¥1.000001（尚未完整）"], ["当前预留", "待确认"], ["实际结算", "待确认"]]);
});
test("Formatting preserves micro precision including the maximum safe integer", () => {
 assert.equal(format(1), "¥0.000001"); assert.equal(format(1_100_000), "¥1.1");
 assert.equal(format(12_345_678_901), "¥12,345.678901");
 assert.equal(format(Number.MAX_SAFE_INTEGER), "¥9,007,199,254.740991");
});

test("Image owner unknown-result markers also prohibit a new paid candidate", () => {
 for (const error_code of ["direct_image_result_unknown", "image_job_result_unknown", "no_image_returned", "result_unknown"]) {
  const value = recovery({ ...caps, state: "failed" }, { status: "failed", error_code });
  assert.equal(value.state, "submission_unknown"); assert.equal(value.can_generate_new, false);
 }
});

test("Expired with a finished timestamp and unknown-submit error remains query-only", () => {
 const value = recovery({ ...caps, state: "expired" }, { status: "expired", finished_at: "2026-10-10", error_code: "submit_unknown" });
 assert.equal(value.state, "submission_unknown"); assert.equal(value.can_query, true);
 assert.equal(value.can_generate_new, false); assert.equal(value.can_cancel, false);
 const legacy = recovery(undefined, { status: "expired", error_code: "submit_unknown" });
 assert.equal(legacy.can_generate_new, false); assert.equal(legacy.can_cancel, false);
});
