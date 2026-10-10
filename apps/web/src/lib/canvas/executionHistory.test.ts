import assert from "node:assert/strict";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";
import type { CanvasDocument, CanvasExecutionHistoryPage, CanvasHistoricalExecution } from "./types";
const { CanvasHistoryPager, canvasHistoryQueryKey, compareHistoricalExecutions, immutableHistorySelection, visibleExecutionFreshness } = await import("./executionHistory.ts");
const { buildHistoricalBranch } = await import("./historicalBranch.ts");
const { createCanvasEditorStore } = await import("./store.ts");
const { createCanvasNode } = await import("./registry.ts");
const { mergeCanvasDocumentByRevision } = await import("./documentMerge.ts");

function fixture() {
  const prompt = createCanvasNode("prompt", { x: 0, y: 0 }, { id: "prompt", config: { text: "CURRENT text" } });
  const asset = createCanvasNode("image_asset", { x: 0, y: 200 }, { id: "asset", config: { image_id: "CURRENT image" } });
  const source = createCanvasNode("image_generate", { x: 0, y: 400 }, { id: "source" });
  const target = createCanvasNode("image_generate", { x: 400, y: 0 }, { id: "target" });
  const graph = { schema_version: 1 as const, nodes: [prompt, asset, source, target], edges: [], frames: [], settings: { snap_to_grid: false, grid_size: 20 } };
  const execution: CanvasHistoricalExecution = {
    id: "execution", node_id: target.id, node_type: target.type, status: "succeeded",
    outputs: [{ type: "image", image_id: "saved-result" }], config_snapshot: { ...target.config, count: 2 },
    definition_hash: "definition", input_hash: "input", processor_version: "v1",
    input_snapshot: { prompt: "SAVED text", bindings: [
      { edge_id: "text", source_node_id: "prompt", target_handle: "prompt", role: null, order: 0, binding_mode: "follow_active", text: "SAVED text" },
      { edge_id: "asset", source_node_id: "asset", target_handle: "references", role: "reference", order: 0, binding_mode: "follow_active",
        asset: { image_id: "SAVED image", sha256: "a".repeat(64) } },
      { edge_id: "generated", source_node_id: "source", target_handle: "references", role: "style", order: 1, binding_mode: "follow_active",
        source_execution_id: "SAVED source execution", output_index: 3,
        asset: { image_id: "SAVED generated image", sha256: "b".repeat(64), source_execution_id: "SAVED source execution", output_index: 3 } },
    ] },
  };
  const document: CanvasDocument = { id: "canvas", title: "Canvas", revision: 2, graph, created_at: "", updated_at: "",
    selections: [], recent_executions: [execution], active_runs: [], execution_freshness: { execution: { state: "fresh", reason: null } } };
  return { graph, execution, document };
}
const scope = { userId: "owner", epoch: 2, canvasId: "canvas", nodeId: "target" };
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }

test("history paging bounds rows, deduplicates replay, keeps immutable A/B after failure and retries the exact cursor", async () => {
  const { execution } = fixture();
  const calls: Array<string | null> = [];
  let fail = true;
  const pager = new CanvasHistoryPager(scope, async (cursor) => {
    calls.push(cursor);
    if (cursor === "opaque&next" && fail) { fail = false; throw new Error("offline"); }
    return cursor === null ? { items: [execution, execution], next_cursor: "opaque&next" }
      : { items: [{ ...execution, id: "older" }], next_cursor: "opaque&next" };
  }, () => true);
  await pager.load(); assert.equal(pager.state.items.length, 1);
  pager.select("a", execution);
  execution.config_snapshot!.count = 99;
  await pager.load("next");
  assert.equal(pager.state.error, "offline"); assert.equal(pager.state.a?.config_snapshot?.count, 2);
  await pager.load("retry"); pager.select("b", pager.state.items[0]);
  assert.equal(pager.state.a?.id, "execution"); assert.equal(pager.state.b?.id, "older");
  assert.equal(pager.state.nextCursor, null);
  await pager.load("previous");
  assert.deepEqual(calls, [null, "opaque&next", "opaque&next", null]);
  assert.equal(pager.state.b?.id, "older");
  assert.notDeepEqual(canvasHistoryQueryKey(scope, null), canvasHistoryQueryKey({ ...scope, userId: "other" }, null));
  assert.notDeepEqual(canvasHistoryQueryKey(scope, null), canvasHistoryQueryKey({ ...scope, nodeId: "other" }, null));
});
test("delayed old-node and old-account responses never populate history", async () => {
  const { execution } = fixture();
  for (const change of ["node", "account"]) {
    const pending = deferred<CanvasExecutionHistoryPage>(); let current = true;
    const pager = new CanvasHistoryPager(scope, () => pending.promise, () => current);
    const request = pager.load();
    if (change === "node") pager.dispose(); else current = false;
    pending.resolve({ items: [execution], next_cursor: null }); await request;
    assert.equal(pager.state.items.length, 0);
  }
});
test("newer history request wins even when abort is ignored, and oversized/wrong-node pages fail closed", async () => {
  const { execution } = fixture();
  const first = deferred<CanvasExecutionHistoryPage>(); let n = 0;
  const pager = new CanvasHistoryPager(scope, async () => ++n === 1 ? first.promise : { items: [{ ...execution, id: "new" }], next_cursor: null }, () => true);
  const old = pager.load(); await pager.load(); first.resolve({ items: [execution], next_cursor: null }); await old;
  assert.equal(pager.state.items[0].id, "new");
  for (const items of [Array.from({ length: 31 }, () => execution), [{ ...execution, node_id: "wrong" }]]) {
    const invalid = new CanvasHistoryPager(scope, async () => ({ items, next_cursor: null }), () => true);
    await invalid.load(); assert.ok(invalid.state.error); assert.equal(invalid.state.items.length, 0);
  }
});
test("comparisons include provenance, immutable snapshots, legacy unknown and explicit truncation", () => {
  const { execution } = fixture(); const a = immutableHistorySelection(execution); const b = structuredClone(execution);
  b.input_hash = "other"; b.processor_version = "v2";
  (b.input_snapshot!.bindings as Array<Record<string, unknown>>)[2].output_index = 5;
  const result = compareHistoricalExecutions(a, b);
  assert.ok(result.rows.some((row) => row.path === "input_hash"));
  assert.ok(result.rows.some((row) => row.path === "processor_version"));
  assert.ok(result.rows.some((row) => row.path.includes("output_index")));
  assert.equal(result.incomplete, false);
  assert.equal(compareHistoricalExecutions(a, { ...b, input_snapshot: null }).incomplete, true);
  b.config_snapshot = { many: Array.from({ length: 500 }, (_, i) => i) };
  assert.equal(compareHistoricalExecutions(a, b).truncated, true);
  assert.ok(Object.isFrozen(a.input_snapshot));
});
test("saved freshness is suppressed after edits, revision mismatch, missing legacy projection and stale selection merges", () => {
  const { document, graph } = fixture();
  assert.equal(visibleExecutionFreshness(document, graph, 2, 0, "execution").state, "fresh");
  assert.equal(visibleExecutionFreshness(document, graph, 2, 1, "execution").state, "unknown");
  assert.equal(visibleExecutionFreshness(document, graph, 3, 0, "execution").state, "unknown");
  const edit = structuredClone(graph); edit.nodes[0].config.text = "edited";
  assert.equal(visibleExecutionFreshness(document, edit, 2, 0, "execution").state, "unknown");
  assert.equal(visibleExecutionFreshness({ ...document, execution_freshness: undefined }, graph, 2, 0, "execution").state, "unknown");
  const merged = mergeCanvasDocumentByRevision({ ...document, selections: [{ node_id: "source", execution_id: "new", output_index: 0, revision: 2 }] },
    { ...document, selections: [{ node_id: "source", execution_id: "old", output_index: 0, revision: 1 }] });
  assert.equal(merged.execution_freshness, undefined);
});
test("historical branch reconstructs saved config/text/assets and pins exact generated output as one undoable draft-only transaction", () => {
  const { graph, execution } = fixture(); const store = createCanvasEditorStore(graph, 2);
  const before = structuredClone(store.getState().graph);
  const result = store.getState().branchHistoricalExecution(execution); assert.ok(result.ok);
  const state = store.getState(); const target = state.graph.nodes.find((node) => node.id === result.nodeId)!;
  assert.deepEqual(target.config, execution.config_snapshot);
  assert.equal(state.graph.nodes.find((node) => node.id === "prompt")?.config.text, "CURRENT text");
  const incoming = state.graph.edges.filter((edge) => edge.target_node_id === target.id);
  assert.equal(state.graph.nodes.find((node) => node.id === incoming[0].source_node_id)?.config.text, "SAVED text");
  assert.equal(state.graph.nodes.find((node) => node.id === incoming[1].source_node_id)?.config.image_id, "SAVED image");
  assert.equal(incoming[2].binding_mode, "pinned"); assert.equal(incoming[2].pinned_execution_id, "SAVED source execution"); assert.equal(incoming[2].pinned_output_index, 3);
  assert.equal(state.history.length, 1); assert.equal(state.pendingOperationGroupSizes.length, 1);
  assert.ok(state.pendingOperations.every((op) => op.op === "add_node" || op.op === "add_edge"));
  store.getState().undo(); assert.deepEqual(store.getState().graph, before);
  store.getState().redo(); assert.equal(store.getState().graph.nodes.length, before.nodes.length + 3);
});
test("missing source, incompatible ports, legacy provenance, absent output index/hash and inconsistent prompt block branch without mutation", () => {
  for (const kind of ["source", "port", "snapshot", "index", "hash", "prompt"]) {
    const { graph, execution } = fixture();
    const bindings = execution.input_snapshot!.bindings as Array<Record<string, unknown>>;
    if (kind === "source") graph.nodes = graph.nodes.filter((node) => node.id !== "source");
    if (kind === "port") bindings[2].target_handle = "missing";
    if (kind === "snapshot") execution.input_snapshot = null;
    if (kind === "index") delete bindings[2].output_index;
    if (kind === "hash") (bindings[2].asset as Record<string, unknown>).sha256 = "";
    if (kind === "prompt") execution.input_snapshot!.prompt = "different";
    assert.equal(buildHistoricalBranch(graph, execution).ok, false, kind);
    const store = createCanvasEditorStore(graph, 2);
    assert.equal(store.getState().branchHistoricalExecution(execution).ok, false, kind);
    assert.equal(store.getState().history.length, 0); assert.equal(store.getState().pendingOperations.length, 0);
  }
});

test("merged historical prompt whitespace is preserved through no-trim reconstruction", async () => {
  const { graph, execution } = fixture();
  graph.nodes[0] = createCanvasNode("prompt_merge", { x: 0, y: 0 }, { id: "prompt" });
  const exact = "  saved prompt\\n";
  execution.input_snapshot!.prompt = exact;
  (execution.input_snapshot!.bindings as Array<Record<string, unknown>>)[0].text = exact;
  const store = createCanvasEditorStore(graph, 2);
  const result = store.getState().branchHistoricalExecution(execution); assert.ok(result.ok);
  const next = store.getState().graph;
  const edge = next.edges.find((item) => item.target_node_id === result.nodeId && item.target_handle === "prompt")!;
  const source = next.nodes.find((node) => node.id === edge.source_node_id)!;
  assert.equal(source.type, "prompt_merge"); assert.equal(source.config.trim, false);
  const { resolveCanvasTextOutput } = await import("./graph.ts");
  assert.equal(resolveCanvasTextOutput(next, source.id), exact);
  store.getState().undo(); assert.equal(store.getState().graph.nodes.length, graph.nodes.length);
});
