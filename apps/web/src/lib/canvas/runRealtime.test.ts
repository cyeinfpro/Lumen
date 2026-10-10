import assert from "node:assert/strict";
import test from "node:test";
import type { CanvasDocument } from "./types";
import type { CanvasRunRealtimeDependencies, CanvasRunEventBatch } from "./runRealtime";
const { CanvasRunRealtimeCoordinator, parseCanvasRunNotice, continuousCanvasBatch, mergeCanvasRunDetail } =
  await import(new URL("./runRealtime.ts", import.meta.url).href);
const notice = (seq = 1) => ({ schema_version: 1, canvas_id: "c", run_id: "r", seq,
  execution_id: "e", event_type: "canvas.execution.status_changed" });
const batch = (after: number, end: number, last = end): CanvasRunEventBatch => ({
  items: Array.from({ length: end - after }, (_, index) => ({ run_id: "r", seq: after + index + 1, payload: {} })),
  after_seq: after, next_after_seq: end, last_event_seq: last, has_more: end < last, snapshot_required: false,
});
function document(): CanvasDocument {
  return { id: "c", title: "draft", revision: 3, created_at: "", updated_at: "",
    graph: { schema_version: 1, nodes: [], edges: [], frames: [], settings: { snap_to_grid: false, grid_size: 16 } },
    selections: [], recent_executions: [], active_runs: [] };
}
function harness(overrides: Partial<CanvasRunRealtimeDependencies> = {}) {
  const calls: string[] = [];
  const deps: CanvasRunRealtimeDependencies = {
    isCurrent: () => true, current: () => document(), interested: () => true,
    batch: async (_c, _r, after) => { calls.push("batch:" + after); return batch(after, after + 1); },
    detail: async () => ({ run: { id: "r", status: "running", last_event_seq: 1 }, executions: [] }),
    apply: () => { calls.push("apply"); },
    snapshot: async () => { calls.push("snapshot"); },
    ...overrides,
  };
  return { calls, deps, coordinator: new CanvasRunRealtimeCoordinator(deps) };
}

test("canvas notice parser rejects unsafe shapes and strips unrelated payload fields", () => {
  for (const seq of [0, -1, 1.1, Infinity, Number.MAX_SAFE_INTEGER + 1, "1"]) {
    assert.equal(parseCanvasRunNotice({ ...notice(), seq }), null);
  }
  assert.equal(parseCanvasRunNotice({ ...notice(), schema_version: 2 }), null);
  assert.equal(parseCanvasRunNotice({ ...notice(), canvas_id: "" }), null);
  assert.equal(parseCanvasRunNotice({ ...notice(), event_type: "Bad Kind" }), null);
  assert.deepEqual(Object.keys(parseCanvasRunNotice({ ...notice(), provider_secret: "drop" })!).sort(),
    ["canvas_id", "event_type", "execution_id", "run_id", "seq"]);
});

test("event batch validation rejects gaps, foreign runs, future cursors and malformed items", () => {
  assert.equal(continuousCanvasBatch(batch(0, 2), "r", 0), true);
  for (const value of [
    { ...batch(0, 1), snapshot_required: true },
    { ...batch(0, 1), items: [{ run_id: "foreign", seq: 1, payload: {} }] },
    { ...batch(0, 1), items: [{ run_id: "r", seq: 2, payload: {} }] },
    { ...batch(0, 1), items: [null] },
    { ...batch(0, 1), next_after_seq: 5 },
    { ...batch(0, 0), last_event_seq: 2 },
  ]) assert.equal(continuousCanvasBatch(value as CanvasRunEventBatch, "r", 0), false);
});

test("ordered updates apply only once across duplicate or older notices", async () => {
  const h = harness(); await h.coordinator.receive(notice()); await h.coordinator.receive(notice());
  assert.deepEqual(h.calls, ["batch:0", "apply"]);
});

test("snapshot-proven sequence avoids replaying old notifications", async () => {
  const h = harness({ current: () => ({ ...document(), active_runs: [{ id: "r", status: "running", last_event_seq: 4 }] }) });
  await h.coordinator.receive(notice(3)); assert.deepEqual(h.calls, []);
});

test("gaps restore a snapshot before any cursor is acknowledged", async () => {
  const h = harness({ batch: async () => ({ ...batch(0, 1), snapshot_required: true }) });
  await h.coordinator.receive(notice()); await h.coordinator.receive(notice());
  assert.deepEqual(h.calls, ["snapshot"]);
});

test("selection or media output changes refresh authoritative selection and asset projections", async () => {
  for (const payload of [{ selection_updated: true }, { outputs: [{ image_id: "new" }] }]) {
    const h = harness({ batch: async () => ({ ...batch(0, 1), items: [{ run_id: "r", seq: 1, payload }] }) });
    await h.coordinator.receive(notice()); assert.deepEqual(h.calls, ["snapshot"]);
  }
});

test("failed snapshot does not acknowledge the sequence and can be retried", async () => {
  let attempts = 0;
  const h = harness({ batch: async () => ({ ...batch(0, 1), snapshot_required: true }),
    snapshot: async () => { if (++attempts === 1) throw new Error("temporary"); } });
  await h.coordinator.receive(notice()); await h.coordinator.receive(notice());
  assert.equal(attempts, 2);
});

test("identity transition discards an old in-flight response without cache writes", async () => {
  let current = true;
  let release!: (value: CanvasRunEventBatch) => void;
  const h = harness({ isCurrent: () => current, batch: () => new Promise((resolve) => { release = resolve; }) });
  const pending = h.coordinator.receive(notice()); current = false; release(batch(0, 1)); await pending;
  assert.deepEqual(h.calls, []);
});

test("dispose aborts in-flight reads and prevents later responses or receives", async () => {
  let release!: (value: CanvasRunEventBatch) => void;
  let signal!: AbortSignal;
  const h = harness({ batch: (_c, _r, _a, s) => { signal = s; return new Promise((resolve) => { release = resolve; }); } });
  const pending = h.coordinator.receive(notice()); h.coordinator.dispose();
  assert.equal(signal.aborted, true); release(batch(0, 1)); await pending;
  await h.coordinator.receive(notice(2)); assert.deepEqual(h.calls, []);
});

test("hidden or unopened canvases do not create per-node or per-canvas requests", async () => {
  const h = harness({ interested: () => false });
  await h.coordinator.receive(notice()); assert.deepEqual(h.calls, []);
});

test("newer notifications during a read are coalesced without loss", async () => {
  let release!: (value: CanvasRunEventBatch) => void;
  let details = 0;
  const h = harness({
    batch: async (_c, _r, after) => after === 0
      ? new Promise((resolve) => { release = resolve; }) : batch(after, 2),
    detail: async () => ({ run: { id: "r", status: "running", last_event_seq: ++details }, executions: [] }),
  });
  const pending = h.coordinator.receive(notice()); void h.coordinator.receive(notice(2));
  release(batch(0, 1, 2)); await pending;
  assert.deepEqual(h.calls, ["apply", "apply"]);
});

test("run detail sequence races or foreign executions require a full snapshot", async () => {
  const h = harness({ detail: async () => ({ run: { id: "r", status: "running", last_event_seq: 2 }, executions: [] }) });
  await h.coordinator.receive(notice());
  assert.deepEqual(h.calls, ["batch:0", "snapshot"]);
});

test("incremental run data retains graph, draft metadata, selection and unrelated executions", () => {
  const current = document();
  current.recent_executions = [{ id: "other", run_id: "other", node_id: "n", node_type: "image_generate",
    status: "succeeded", outputs: [] }];
  const merged = mergeCanvasRunDetail(current, { run: { id: "r", status: "running", last_event_seq: 1 }, executions: [] });
  assert.equal(merged.graph, current.graph); assert.equal(merged.title, "draft");
  assert.deepEqual(merged.selections, current.selections);
  assert.deepEqual(merged.recent_executions, current.recent_executions);
});

test("a notice between the drain tail and promise cleanup cannot be lost", async () => {
  let details = 0;
  const h = harness({
    detail: async () => ({ run: { id: "r", status: "running", last_event_seq: ++details }, executions: [] }),
    apply: () => { if (details === 1) queueMicrotask(() => { void h.coordinator.receive(notice(2)); }); },
  });
  await h.coordinator.receive(notice());
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  assert.equal(details, 2);
});
