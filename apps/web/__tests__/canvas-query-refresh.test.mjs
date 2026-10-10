import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

function harness(userId = "a") {
  let identity = { userId, epoch: userId ? 1 : 0 };
  const reads = [];
  const generation = { current: 0 };
  const mocks = {
    react: { useRef: () => generation },
    "@tanstack/react-query": { useQuery: (options) => options, useQueryClient: () => ({ getQueryData: () => undefined }) },
    "./userScope": { useUserQueryScope: () => ({ userId: identity.userId, enabled: Boolean(identity.userId) }) },
    "@/lib/auth/privateIdentityEpoch": { getPrivateIdentitySnapshot: () => ({ ...identity }),
      isPrivateIdentitySnapshotCurrent: (value) => value.userId !== null && value.userId === identity.userId && value.epoch === identity.epoch },
    "@/lib/api/canvases": { getCanvas: (_id, signal) => new Promise((resolve) => { reads.push({ resolve, signal }); }) },
    "@/lib/canvas/documentMerge": { mergeCanvasDocumentByRevision: (_old, value) => value },
    "@/lib/canvas/assets": { hasPreparingCanvasAssets: (assets) => assets?.some((a) => ["pending", "preparing"].includes(a.preparation_state)) ?? false },
  };
  const code = ts.transpileModule(readFileSync(new URL("../src/lib/queries/canvases.ts", import.meta.url), "utf8"),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const compiled = { exports: {} };
  new Function("require", "module", "exports", code)((id) => mocks[id] ?? {}, compiled, compiled.exports);
  return { query: compiled.exports.useCanvasQuery("canvas"), reads,
    render() { return compiled.exports.useCanvasQuery("canvas"); },
    changeIdentity() { identity = { userId: "b", epoch: 2 }; } };
}
test("Cold Canvas waits for identity bootstrap then starts its first authenticated request", async () => {
  const h = harness(null);
  assert.equal(h.query.enabled, false);
  await assert.rejects(h.query.queryFn({ signal: new AbortController().signal }), { name: "AbortError" });
  assert.equal(h.reads.length, 0);
  h.changeIdentity(); const authenticated = h.render();
  assert.equal(authenticated.enabled, true);
  const request = authenticated.queryFn({ signal: new AbortController().signal });
  h.reads[0].resolve({ id: "canvas", assets: [] });
  assert.deepEqual(await request, { id: "canvas", assets: [] });
});
test("Canvas snapshot discards an old authenticated identity response", async () => {
  const h = harness(); const request = h.query.queryFn({ signal: new AbortController().signal });
  h.changeIdentity(); h.reads[0].resolve({ id: "canvas", assets: ["private-old"] });
  await assert.rejects(request, { name: "AbortError" });
});
test("Canvas later requests fence older content hashes before projection merging", async () => {
  const h = harness();
  const first = h.query.queryFn({ signal: new AbortController().signal });
  const second = h.query.queryFn({ signal: new AbortController().signal });
  h.reads[1].resolve({ hash: "new" }); assert.deepEqual(await second, { hash: "new" });
  h.reads[0].resolve({ hash: "old" }); await assert.rejects(first, { name: "AbortError" });
});
test("Canvas snapshot passes query cancellation through and never applies an aborted read", async () => {
  const h = harness(); const controller = new AbortController();
  const request = h.query.queryFn({ signal: controller.signal });
  assert.equal(h.reads[0].signal, controller.signal); controller.abort();
  h.reads[0].resolve({ id: "canvas" }); await assert.rejects(request, { name: "AbortError" });
});
test("Preparing assets refresh even without a run; ready and failed assets stop", () => {
  const h = harness();
  for (const state of ["pending", "preparing", "ready", "failed"]) {
    const data = { assets: [{ preparation_state: state }], active_runs: [], recent_executions: [] };
    assert.equal(h.query.refetchInterval({ state: { data } }), ["pending", "preparing"].includes(state) ? 4000 : false);
  }
  assert.equal(h.query.refetchInterval({ state: { data: { active_runs: [{ status: "running" }], recent_executions: [] } } }), 2000);
});
test("Hidden Canvas does not schedule asset or run polling", () => {
  const prior = globalThis.document; globalThis.document = { visibilityState: "hidden" };
  try {
    const h = harness();
    assert.equal(h.query.refetchInterval({ state: { data: { assets: [{ preparation_state: "pending" }],
      active_runs: [{ status: "running" }], recent_executions: [] } } }), false);
  } finally { if (prior === undefined) delete globalThis.document; else globalThis.document = prior; }
});
