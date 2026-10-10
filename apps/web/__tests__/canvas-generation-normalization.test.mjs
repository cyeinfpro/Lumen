import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
function moduleAt(path, dependencies = {}) {
  const code = ts.transpileModule(readFileSync(new URL(path, import.meta.url), "utf8"),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const compiled = { exports: {} };
  new Function("module", "exports", "require", code)(compiled, compiled.exports, (name) => {
    if (!(name in dependencies)) throw new Error("Unexpected dependency: " + name);
    return dependencies[name];
  });
  return compiled.exports;
}
const details = moduleAt("../src/lib/canvas/generationDetails.ts");
const api = moduleAt("../src/lib/api/canvases.ts", {
  "./http": {}, "./semanticIdempotency": {},
  "../canvas/assets": {}, "../canvas/generationDetails": details,
  "../canvas/graph": {},
  "../canvas/executionHistory": moduleAt("../src/lib/canvas/executionHistory.ts"),
});
test("Actual Canvas execution normalization retains owner recovery and aggregate costs", () => {
  const execution = api.normalizeExecution({ id: "e", node_id: "n", status: "failed", outputs: [],
    tasks: [{ id: "t", generation_id: "owner", kind: "generation", status: "expired",
      finished_at: "2026-10-10T00:00:00Z", error_code: "submit_unknown",
      recovery: { state: "expired", can_query: true, can_cancel: false, can_generate_new: true, automatic_resubmit: false },
      billing: { currency: "CNY", source: "wallet_ledger", actual_cost_micro: 0 } }],
    billing: { currency: "CNY", source: "task_ledger_aggregate", task_count: 1,
      estimated_cost_micro: null, known_estimated_cost_micro: 12 } });
  assert.equal(execution.tasks[0].generation_id, "owner");
  assert.equal(execution.tasks[0].recovery.state, "submission_unknown");
  assert.equal(execution.tasks[0].recovery.can_generate_new, false);
  assert.equal(execution.tasks[0].billing.actual_cost_micro, 0);
  assert.equal(execution.billing.estimated_cost_micro, null);
  assert.equal(execution.billing.known_estimated_cost_micro, 12);
});
test("Legacy unknown tasks are made query-only without invented billing", () => {
  const execution = api.normalizeExecution({ tasks: [{ id: "t", status: "expired", progress_stage: "submission_unknown" }] });
  assert.equal(execution.tasks[0].recovery.state, "submission_unknown");
  assert.equal(execution.tasks[0].recovery.can_generate_new, false);
  assert.equal(execution.tasks[0].billing, undefined);
  assert.equal(execution.billing, undefined);
});
test("Run normalization never substitutes legacy initialized cost zero for billing", () => {
  const run = api.normalizeRun({ id: "r", reserved_micro: 0, actual_cost_micro: 0 });
  assert.equal(run.billing, undefined);
  const withBilling = api.normalizeRun({ id: "r",
    billing: { currency: "CNY", source: "task_ledger_aggregate", reserved_micro: 0 } });
  assert.equal(withBilling.billing.reserved_micro, 0);
  assert.equal(withBilling.billing.actual_cost_micro, null);
});
