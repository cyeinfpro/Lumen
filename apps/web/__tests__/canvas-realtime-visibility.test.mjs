import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const notice = { schema_version: 1, canvas_id: "canvas", run_id: "run", seq: 1,
  event_type: "canvas.execution.status_changed" };
function harness() {
  const prior = globalThis.document;
  const document = Object.assign(new EventTarget(), { visibilityState: "visible" });
  globalThis.document = document;
  let current = true, observers = 1;
  const invalidations = [], received = [], cleanups = [];
  const queryClient = {
    getQueryData: () => undefined,
    getQueryCache: () => ({ find: () => ({ getObserversCount: () => observers }) }),
    invalidateQueries: (options) => { invalidations.push(options); return Promise.resolve(); },
  };
  class Coordinator {
    receive(payload) { received.push(payload); return Promise.resolve(); }
    dispose() {}
  }
  const mocks = {
    react: { useRef: (value) => ({ current: value }), useCallback: (fn) => fn,
      useEffect: (fn) => cleanups.push(fn()) },
    "@tanstack/react-query": { useQueryClient: () => queryClient },
    "@/lib/api/canvasRunUpdates": {},
    "@/lib/canvas/runRealtime": { CanvasRunRealtimeCoordinator: Coordinator,
      parseCanvasRunNotice: (value) => value.schema_version === 1 && value.canvas_id ? value : null },
    "@/lib/queries/canvases": { canvasQueryKeys: { all: ["canvas"], detail: (id) => ["canvas", "detail", id] } },
  };
  const code = ts.transpileModule(readFileSync(new URL("../src/features/realtime/model/useCanvasRealtime.ts", import.meta.url), "utf8"),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const compiled = { exports: {} };
  new Function("require", "module", "exports", code)((id) => mocks[id] ?? {}, compiled, compiled.exports);
  const handler = compiled.exports.useCanvasRealtime(() => current);
  return { document, handler, invalidations, received,
    setObserved(value) { observers = value; },
    setCurrent(value) { current = value; },
    unmount() { for (const cleanup of cleanups.splice(0)) cleanup(); },
    close() { this.unmount(); if (prior === undefined) delete globalThis.document; else globalThis.document = prior; } };
}
test("Visible observed Canvas applies increments without invalidating its whole document", () => {
  const h = harness(); try { h.handler(notice); assert.deepEqual(h.received, [notice]); assert.deepEqual(h.invalidations, []); } finally { h.close(); }
});
test("Hidden Canvas marks its cache stale without fetching", () => {
  const h = harness(); try {
    h.document.visibilityState = "hidden"; h.handler(notice);
    assert.deepEqual(h.received, []);
    assert.deepEqual(h.invalidations, [{ queryKey: ["canvas", "detail", "canvas"], exact: true, refetchType: "none" }]);
  } finally { h.close(); }
});
test("Closed Canvas marks its cache stale so reopening refetches", () => {
  const h = harness(); try {
    h.setObserved(0); h.handler(notice);
    assert.equal(h.invalidations[0].refetchType, "none"); assert.deepEqual(h.received, []);
  } finally { h.close(); }
});
test("Returning to visible refreshes active Canvas and restarts preparation observation", () => {
  const h = harness(); try {
    h.document.visibilityState = "hidden"; h.document.dispatchEvent(new Event("visibilitychange"));
    assert.deepEqual(h.invalidations, []);
    h.document.visibilityState = "visible"; h.document.dispatchEvent(new Event("visibilitychange"));
    assert.deepEqual(h.invalidations, [{ queryKey: ["canvas"], refetchType: "active" }]);
  } finally { h.close(); }
});
test("Changed identity and unmounted listeners cannot refresh the old Canvas", () => {
  const h = harness(); try {
    h.setCurrent(false); h.handler(notice); h.document.dispatchEvent(new Event("visibilitychange"));
    assert.deepEqual(h.invalidations, []); assert.deepEqual(h.received, []);
    h.setCurrent(true); h.unmount(); h.document.dispatchEvent(new Event("visibilitychange"));
    assert.deepEqual(h.invalidations, []);
  } finally { h.close(); }
});
test("Malformed notices never invalidate another cache", () => {
  const h = harness(); try { h.handler({}); assert.deepEqual(h.invalidations, []); assert.deepEqual(h.received, []); } finally { h.close(); }
});
