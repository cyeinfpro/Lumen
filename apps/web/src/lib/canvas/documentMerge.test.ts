import assert from "node:assert/strict";
import test from "node:test";

import type { CanvasDocument } from "./types";

const {
  decideCanvasRemoteSync,
  mergeCanvasDocumentByRevision,
  mergeCanvasPatchResult,
} = await import("#canvas-document-merge");

function document(revision: number, title = `画布 ${revision}`): CanvasDocument {
  return {
    id: "canvas-1",
    title,
    description: "",
    revision,
    graph: {
      schema_version: 1,
      nodes: [],
      edges: [],
      frames: [],
      settings: { snap_to_grid: false, grid_size: 16 },
    },
    created_at: "2026-07-13T00:00:00Z",
    updated_at: `2026-07-13T00:00:0${Math.min(revision, 9)}Z`,
    selections: [],
    recent_executions: [],
    active_runs: [],
  };
}

test("canvas query results cannot replace a newer cached revision", () => {
  const current = document(4);
  assert.equal(mergeCanvasDocumentByRevision(current, document(3)), current);
  assert.equal(
    mergeCanvasDocumentByRevision(current, document(4, "同版本新快照")).title,
    "同版本新快照",
  );
  assert.equal(mergeCanvasDocumentByRevision(undefined, document(1)).revision, 1);
});

test("stale metadata patch responses preserve graph revision while applying input", () => {
  const current = document(7, "旧标题");
  const merged = mergeCanvasPatchResult(
    current,
    document(6, "过期响应"),
    { title: "新标题" },
  );

  assert.equal(merged.revision, 7);
  assert.equal(merged.title, "新标题");
  assert.equal(merged.graph, current.graph);
});

test("remote revisions defer while the local save request is in flight", () => {
  const baseState = {
    revision: 4,
    pendingOperationCount: 1,
    inFlightOperationCount: 1,
    activeInteractionCount: 0,
    editingNodeId: null,
  };

  assert.equal(decideCanvasRemoteSync(5, baseState), "defer");
  assert.equal(
    decideCanvasRemoteSync(5, {
      ...baseState,
      pendingOperationCount: 0,
      inFlightOperationCount: 0,
    }),
    "replace",
  );
  assert.equal(
    decideCanvasRemoteSync(5, {
      ...baseState,
      inFlightOperationCount: 0,
      editingNodeId: "prompt-1",
    }),
    "conflict",
  );
  assert.equal(decideCanvasRemoteSync(4, baseState), "ignore");
});
