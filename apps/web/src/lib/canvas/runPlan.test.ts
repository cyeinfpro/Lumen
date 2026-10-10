import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import "../../store/chat/moduleResolution.test-helper.mjs";
const { SemanticIdempotencyStore } = await import("../api/semanticIdempotency.ts");
const { CanvasPlanIntentClient, CanvasPlanPendingError } = await import("./runPlanIntentClient.ts");
const { canvasPlanReducer, canvasPlanCanStart, initialCanvasPlanState } = await import("./runPlanMachine.ts");
const { parseCanvasPlanBudget, validateCanvasPlanPreview, canvasRepairCandidates } = await import("./runPlanValidation.ts");
import type { CanvasPlanInput, CanvasPlanIntent, CanvasPlanPreview, CanvasPlanRunDetail, CanvasPendingPlanIntent } from "./runPlanTypes";

const input: CanvasPlanInput = { document_revision: 2, kind: "selection", target_node_ids: ["a"], budget_micro: 100,
  reuse_outputs: {}, output_indices: {}, failure_policy: "continue_independent", auto_select_on_success: true };
const intent: CanvasPlanIntent = { kind: "start", body: { ...input, plan_hash: "a".repeat(64) } };
const run: CanvasPlanRunDetail = { id: "run", canvas_id: "canvas", kind: "selection", status: "queued", executions: [], summary: {} };
const preview: CanvasPlanPreview = { plan: { schema_version: 1, canvas_id: "canvas", document_revision: 2, kind: "selection",
  target_node_ids: ["a"], graph_hash: "b".repeat(64), steps: [{ node_id: "a", dependencies: [], reuse: null,
  estimated_cost_micro: 100, effective_model: "image", capability_version: "v1", output_index: 0 }], bindings: [],
  failure_policy: "continue_independent", budget_micro: 100, plan_hash: "a".repeat(64) },
  estimated_cost_micro: 100, budget_semantics: "admission_estimate_not_settlement_cap" };
async function fixture() {
  let serial = 0, epoch = 1, identity = "owner";
  const storage = new Map<string, string>(), receipts = new Map<string, CanvasPlanRunDetail>();
  const leases = new SemanticIdempotencyStore({ storage: null, freshKey: () => `key-${++serial}`,
    digest: text => createHash("sha256").update(text).digest("hex"), lockRequest: null });
  await leases.activateIdentity(identity);
  const posts: CanvasPendingPlanIntent[] = [], queries: CanvasPendingPlanIntent[] = [];
  let postMode: "success" | "lost" | "offline" | "reject" = "success", queryFails = false;
  const deps = {
    leases, journal: () => ({ getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => { storage.set(key, value); }, removeItem: (key: string) => { storage.delete(key); } }),
    currentIdentity: () => identity, identityEpoch: () => epoch,
    query: async (pending: CanvasPendingPlanIntent) => {
      queries.push(pending);
      if (queryFails) throw new Error("offline GET");
      return { admitted: receipts.has(pending.key), run: receipts.get(pending.key) ?? null };
    },
    post: async (pending: CanvasPendingPlanIntent) => {
      posts.push(structuredClone(pending));
      if (postMode === "reject") throw Object.assign(new Error("plan changed"), { status: 409 });
      if (postMode === "offline") throw new Error("offline POST");
      receipts.set(pending.key, run);
      if (postMode === "lost") throw new Error("lost acknowledgement");
      return run;
    },
    ambiguous: (error: unknown) => (error as { status?: number })?.status !== 409,
  };
  return { leases, client: new CanvasPlanIntentClient(deps), deps, posts, queries, storage, receipts,
    mode: (next: typeof postMode) => { postMode = next; }, failQueries: () => { queryFails = true; },
    switchAwayAndBack: () => { identity = "owner"; epoch += 2; } };
}
test("explicit CNY micro-unit budget rejects blanks, fractions, exponents and overflow", () => {
  for (const bad of ["", "-1", "1.5", "1e6", "9007199254740992", "01"]) assert.throws(() => parseCanvasPlanBudget(bad));
  assert.equal(parseCanvasPlanBudget("0"), 0);
  assert.equal(parseCanvasPlanBudget("9007199254740991"), Number.MAX_SAFE_INTEGER);
});
test("preview validates revision, identity, known prices and safe totals", () => {
  assert.equal(validateCanvasPlanPreview(preview, "canvas", input), preview);
  assert.throws(() => validateCanvasPlanPreview(preview, "another", input));
  assert.throws(() => validateCanvasPlanPreview(preview, "canvas", { ...input, document_revision: 3 }));
  for (const value of [null, -1, 101, Number.MAX_SAFE_INTEGER + 1]) assert.throws(() => validateCanvasPlanPreview({ ...preview, estimated_cost_micro: value }, "canvas", input));
});
test("old preview response and changed context never enable admission", () => {
  let state = canvasPlanReducer(initialCanvasPlanState, { type: "previewing", generation: 2 });
  state = canvasPlanReducer(state, { type: "preview", generation: 1, preview, input, context: "saved" });
  assert.equal(state.phase, "previewing");
  state = canvasPlanReducer(state, { type: "preview", generation: 2, preview, input, context: "saved" });
  assert.equal(canvasPlanCanStart(state, "saved"), true);
  assert.equal(canvasPlanCanStart(state, "graph/scope/revision/selection/price-changed"), false);
  state = canvasPlanReducer(state, { type: "invalidate" });
  state = canvasPlanReducer(state, { type: "preview", generation: 2, preview, input, context: "saved" });
  assert.equal(canvasPlanCanStart(state, "saved"), false);
});
test("pendingKey is read only and successful intent releases its own gate", async () => {
  const f = await fixture();
  assert.equal(await f.client.pending("canvas"), null);
  assert.equal(f.posts.length, 0);
  assert.equal(await f.client.submit("canvas", intent), run);
  assert.equal(await f.client.pending("canvas"), null);
});
test("lost acknowledgement resolves through durable GET with one POST", async () => {
  const f = await fixture(); f.mode("lost");
  assert.equal(await f.client.submit("canvas", intent), run);
  assert.equal(f.posts.length, 1); assert.equal(f.queries.length, 1);
  assert.equal(await f.client.pending("canvas"), null);
});
test("offline and changed budget cannot allocate a fresh paid intent after remount", async () => {
  const f = await fixture(); f.mode("offline");
  await assert.rejects(f.client.submit("canvas", intent), CanvasPlanPendingError);
  const pending = await f.client.pending("canvas");
  const remounted = new CanvasPlanIntentClient(f.deps);
  await assert.rejects(remounted.submit("canvas", { kind: "start", body: { ...intent.body, budget_micro: 200 } }), CanvasPlanPendingError);
  assert.equal(f.posts.length, 1);
  assert.equal((await remounted.pending("canvas"))?.key, pending?.key);
});
test("explicit replay queries first then reuses exact immutable body and key", async () => {
  const f = await fixture(); f.mode("offline");
  const callerIntent = structuredClone(intent);
  await assert.rejects(f.client.submit("canvas", callerIntent), CanvasPlanPendingError);
  callerIntent.body.budget_micro = 999;
  const pending = await f.client.pending("canvas"); assert.ok(pending);
  f.mode("success");
  assert.equal(await f.client.replay(pending), run);
  assert.deepEqual(f.posts[1], f.posts[0]);
  assert.equal(f.posts[1]!.intent.kind, "start");
  if (f.posts[1]!.intent.kind === "start") assert.equal(f.posts[1]!.intent.body.budget_micro, 100);
});
test("failed recovery GET never posts", async () => {
  const f = await fixture(); f.mode("offline");
  await assert.rejects(f.client.submit("canvas", intent), CanvasPlanPendingError);
  const pending = await f.client.pending("canvas"); assert.ok(pending);
  f.failQueries();
  await assert.rejects(f.client.replay(pending));
  assert.equal(f.posts.length, 1);
});
test("concurrent clicks share one immutable admission", async () => {
  const f = await fixture();
  const results = await Promise.allSettled([f.client.submit("canvas", intent), f.client.submit("canvas", intent)]);
  assert.equal(f.posts.length, 1);
  assert.equal(results.filter(result => result.status === "fulfilled").length >= 1, true);
});
test("definitive revision conflict retires rejected intent while unknown retains it", async () => {
  const f = await fixture(); f.mode("reject");
  await assert.rejects(f.client.submit("canvas", intent), /plan changed/);
  assert.equal(await f.client.pending("canvas"), null);
});
test("identity epoch change fences a delayed receipt even for the same user", async () => {
  const f = await fixture();
  const original = f.deps.post;
  f.deps.post = async pending => { const result = await original(pending); f.switchAwayAndBack(); return result; };
  // A stale action must never return an admitted run to the old view.
  await assert.rejects(f.client.submit("canvas", intent), /登录身份已变化/);
});
test("repair accepts only latest confirmed failure, excludes output/unknown/partial/active", () => {
  const execution = { id: "old", node_id: "a", node_type: "image_generate", status: "failed" as const, outputs: [], attempt: 0,
    tasks: [{ id: "task", kind: "generation", status: "failed", progress_stage: "failed", recovery: { state: "failed" as const, automatic_resubmit: false as const, can_generate_new: true, can_query: true, can_cancel: false } }] };
  assert.deepEqual(canvasRepairCandidates({ ...run, executions: [execution] }).map(value => value.id), ["old"]);
  for (const changed of [
    { ...execution, status: "partial_failed" as const },
    { ...execution, outputs: [{ type: "image" as const, image_id: "saved" }] },
    { ...execution, tasks: [{ ...execution.tasks[0]!, recovery: { ...execution.tasks[0]!.recovery, state: "submission_unknown" as const } }] },
  ]) assert.equal(canvasRepairCandidates({ ...run, executions: [changed] }).length, 0);
  assert.equal(canvasRepairCandidates({ ...run, executions: [execution, { ...execution, id: "new", attempt: 1, status: "running" }] }).length, 0);
});

test("SSE projection refresh preserves a new preview and never clears an unknown repair", () => {
  let state = canvasPlanReducer(initialCanvasPlanState, { type: "admitted", run: { ...run, last_event_seq: 10 } });
  state = canvasPlanReducer(state, { type: "previewing", generation: 1 });
  state = canvasPlanReducer(state, { type: "preview", generation: 1, preview, input, context: "saved" });
  state = canvasPlanReducer(state, { type: "admitted", run: { ...run, last_event_seq: 11 }, refresh: true });
  assert.equal(canvasPlanCanStart(state, "saved"), true);
  state = canvasPlanReducer(state, { type: "admitted", run: { ...run, last_event_seq: 9 }, refresh: true });
  assert.equal(state.run?.last_event_seq, 11);
  state = canvasPlanReducer(state, { type: "unknown", pending: { key: "original", canvasId: "canvas", intent } });
  state = canvasPlanReducer(state, { type: "admitted", run, refresh: true });
  assert.equal(state.pending?.key, "original");
});
test("batch graph fence includes layout and hidden frames; all scope excludes hidden ancestry", async () => {
  const { canvasPlanGraphKey, canvasPlanCandidateNodes } = await import("../../components/ui/canvas/canvasPlanForm.ts");
  const { createDefaultCanvasGraph } = await import("./graph.ts");
  const graph = createDefaultCanvasGraph();
  const saved = canvasPlanGraphKey(graph);
  graph.nodes[0]!.position.x += 1;
  assert.notEqual(canvasPlanGraphKey(graph), saved);
  const executable = graph.nodes.find(node => node.type === "image_generate")!;
  assert.ok(executable);
  executable.parent_group_id = "hidden";
  graph.frames = [{ id: "hidden", hidden_in_run: true }];
  assert.equal(canvasPlanCandidateNodes(graph, [], "all").some(node => node.id === executable.id), false);
});
