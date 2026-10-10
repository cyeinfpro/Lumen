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
const history = moduleAt("../src/lib/canvas/executionHistory.ts");
const assets = moduleAt("../src/lib/canvas/assets.ts");
function apiHarness(respond) {
  const calls = [];
  const http = { apiFetch: async (path, request) => { calls.push({ path, request }); return respond(path, request); } };
  const canvases = moduleAt("../src/lib/api/canvases.ts", {
    "./http": http, "./semanticIdempotency": {}, "../canvas/assets": assets,
    "../canvas/generationDetails": moduleAt("../src/lib/canvas/generationDetails.ts"),
    "../canvas/executionHistory": history, "../canvas/graph": { normalizeCanvasGraph: (value) => value },
  });
  const api = moduleAt("../src/lib/api/canvasHistory.ts", { "./http": http, "./canvases": canvases,
    "./semanticIdempotency": {}, "../canvas/assets": assets, "../canvas/executionHistory": history });
  return { api, canvases, calls };
}
test("history request has exact bounded page size, opaque cursor and abort signal; provenance and output indexes remain intact", async () => {
  const h = apiHarness(() => ({ items: [{ id: "e", node_id: "n/1", status: "partial_failed", config_snapshot: { count: 2 },
    input_snapshot: { prompt: "saved", bindings: [] }, definition_hash: "definition", input_hash: "input", processor_version: "v1",
    outputs: [{ type: "image", image_id: null }, { type: "image", image_id: "image-2" }] }], next_cursor: "next|cursor" }));
  const signal = new AbortController().signal;
  const page = await h.api.getCanvasExecutionHistory("c/1", "n/1", "a+b&c|d", signal);
  assert.equal(h.calls[0].path, "/canvases/c%2F1/nodes/n%2F1/history?limit=30&cursor=a%2Bb%26c%7Cd");
  assert.equal(h.calls[0].request.signal, signal);
  assert.equal(page.items[0].outputs.length, 2); assert.equal(page.items[0].outputs[1].image_id, "image-2");
  assert.equal(page.items[0].input_snapshot.prompt, "saved"); assert.equal(page.items[0].processor_version, "v1");
});
test("history rejects oversized and wrong-node pages; legacy snapshots stay unknown", async () => {
  const h = apiHarness(() => ({ items: [{ id: "e", node_id: "node", outputs: [] }], next_cursor: null }));
  const page = await h.api.getCanvasExecutionHistory("canvas", "node", null);
  assert.equal(page.items[0].config_snapshot, null); assert.equal(page.items[0].input_hash, null);
  for (const items of [Array.from({ length: 31 }, () => ({ id: "e", node_id: "node" })), [{ id: "e", node_id: "other" }]]) {
    const invalid = apiHarness(() => ({ items, next_cursor: null }));
    await assert.rejects(invalid.api.getCanvasExecutionHistory("canvas", "node", null));
  }
});
test("selection keeps existing CAS endpoint and conflict cannot trigger execution or silent retry", async () => {
  const conflict = Object.assign(new Error("selection conflict"), { status: 409 });
  const h = apiHarness(() => { throw conflict; });
  await assert.rejects(h.canvases.selectCanvasExecutionOutput("c", "e", 3, 9), { status: 409 });
  assert.equal(h.calls.length, 1); assert.equal(h.calls[0].path, "/canvases/c/executions/e/select");
  assert.deepEqual(JSON.parse(h.calls[0].request.body), { output_index: 3, selection_revision: 9 });
});
test("preparation rejects unknown revision/hash and nonfailed states before any request", async () => {
  const h = apiHarness(() => assert.fail("must not request"));
  for (const patch of [{ preparation_revision: null }, { source_sha256: "" }, { preparation_state: "preparing" }, { kind: "image" }]) {
    await assert.rejects(h.api.retryCanvasVideoPreparation({ kind: "video", asset_id: "v", preparation_state: "failed",
      preparation_revision: 2, source_sha256: "a".repeat(64), ...patch }));
  }
  assert.equal(h.calls.length, 0);
});
function selectionHarness() {
  let identity = { userId: "owner", epoch: 1 };
  const calls = [], refs = [];
  let snapshot = { id: "canvas", graph: { identity: "preserved" }, selections: [{ node_id: "node", revision: 4 }],
    execution_freshness: { e1: { state: "fresh", reason: null } }, stale_node_ids: ["older"] };
  const options = moduleAt("../src/lib/queries/canvases.ts", {
    react: { useRef: (value) => { const ref = { current: value }; refs.push(ref); return ref; } },
    "@tanstack/react-query": { useMutation: (value) => value, useQueryClient: () => ({ invalidateQueries() {}, setQueryData(_key, update) { snapshot = update(snapshot); } }) },
    "@/lib/auth/privateIdentityEpoch": { getPrivateIdentitySnapshot: () => ({ ...identity }),
      isPrivateIdentitySnapshotCurrent: (value) => value.userId === identity.userId && value.epoch === identity.epoch },
    "@/lib/canvas/assets": {}, "./userScope": {},
    "@/lib/api/canvases": { selectCanvasExecutionOutput: (...args) => new Promise((resolve, reject) => calls.push({ args, resolve, reject })) },
    "@/lib/canvas/documentMerge": {}, "@/shared/realtime/browser": {},
  }).useSelectCanvasOutputMutation("canvas");
  return { options, calls, snapshot: () => snapshot, changeIdentity() { identity = { userId: "other", epoch: 2 }; } };
}
test("queued output selection aborts on identity change and discards delayed responses", async () => {
  const h = selectionHarness();
  const first = h.options.mutationFn({ nodeId: "node", executionId: "e1", outputIndex: 0, selectionRevision: 4 });
  const second = h.options.mutationFn({ nodeId: "node", executionId: "e2", outputIndex: 2, selectionRevision: 4 });
  const firstRejected = assert.rejects(first, { name: "AbortError" }), secondRejected = assert.rejects(second, { name: "AbortError" });
  await new Promise((resolve) => setTimeout(resolve, 0)); assert.equal(h.calls.length, 1);
  h.changeIdentity(); h.calls[0].resolve({ node_id: "node", execution_id: "e1", output_index: 0, revision: 5 });
  await Promise.all([firstRejected, secondRejected]); assert.equal(h.calls.length, 1);
});
test("selection CAS conflict clears local revision knowledge without automatically resubmitting", async () => {
  const h = selectionHarness();
  const first = h.options.mutationFn({ nodeId: "node", executionId: "e1", outputIndex: 0, selectionRevision: 4 });
  const rejected = assert.rejects(first, { status: 409 });
  await new Promise((resolve) => setTimeout(resolve, 0));
  h.calls[0].reject(Object.assign(new Error("conflict"), { status: 409 })); await rejected;
  assert.equal(h.calls.length, 1);
  const next = h.options.mutationFn({ nodeId: "node", executionId: "e2", outputIndex: 1, selectionRevision: 7 });
  await new Promise((resolve) => setTimeout(resolve, 0)); assert.equal(h.calls[1].args[3], 7);
  h.calls[1].resolve({ node_id: "node", execution_id: "e2", output_index: 1, revision: 8 }); await next;
});

test("confirmed output selection immediately invalidates freshness while preserving graph and CAS selections", async () => {
  const h = selectionHarness(), before = structuredClone(h.snapshot());
  const pending = h.options.mutationFn({ nodeId: "node", executionId: "e1", outputIndex: 2, selectionRevision: 4 });
  await new Promise((resolve) => setTimeout(resolve, 0));
  h.calls[0].resolve({ node_id: "node", execution_id: "e1", output_index: 2, revision: 5 });
  const selected = await pending;
  h.options.onSuccess(selected);
  assert.equal(h.snapshot().execution_freshness, undefined); assert.equal(h.snapshot().stale_node_ids, undefined);
  assert.deepEqual(h.snapshot().graph, before.graph); assert.deepEqual(h.snapshot().selections, before.selections);
  assert.equal(h.calls.length, 1);
});
test("identity change between selection acknowledgement and success callback cannot alter the next owner's projection", async () => {
  const h = selectionHarness(), before = structuredClone(h.snapshot());
  const pending = h.options.mutationFn({ nodeId: "node", executionId: "e1", outputIndex: 2, selectionRevision: 4 });
  await new Promise((resolve) => setTimeout(resolve, 0));
  h.calls[0].resolve({ node_id: "node", execution_id: "e1", output_index: 2, revision: 5 });
  const selected = await pending;
  h.changeIdentity(); h.options.onSuccess(selected);
  assert.deepEqual(h.snapshot(), before); assert.equal(h.calls.length, 1);
});
