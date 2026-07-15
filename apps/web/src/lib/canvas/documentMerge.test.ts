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

test("equal document revisions preserve newer execution projections", () => {
  const current = document(4);
  current.recent_executions = [
    {
      id: "execution-1",
      node_id: "image-1",
      node_type: "image_generate",
      status: "succeeded",
      outputs: [],
      updated_at: "2026-07-13T00:00:10Z",
    },
  ];
  const incoming = document(4, "fresh metadata");
  incoming.recent_executions = [
    {
      ...current.recent_executions[0]!,
      status: "running",
      updated_at: "2026-07-13T00:00:05Z",
    },
  ];

  const merged = mergeCanvasDocumentByRevision(current, incoming);
  assert.equal(merged.title, "fresh metadata");
  assert.equal(merged.recent_executions[0]?.status, "succeeded");
  assert.equal(
    merged.recent_executions[0],
    current.recent_executions[0],
  );
});

test("equal document revisions accept newer task progress projections", () => {
  const current = document(4);
  current.recent_executions = [
    {
      id: "execution-video",
      node_id: "video-1",
      node_type: "video_generate",
      status: "running",
      outputs: [],
      updated_at: "2026-07-13T00:00:05Z",
      tasks: [
        {
          id: "task-video",
          kind: "video_generation",
          status: "running",
          progress_stage: "rendering",
          progress_pct: 25,
          updated_at: "2026-07-13T00:00:06Z",
        },
      ],
    },
  ];
  const incoming = document(4);
  incoming.recent_executions = [
    {
      ...current.recent_executions[0]!,
      tasks: [
        {
          ...current.recent_executions[0]!.tasks![0]!,
          progress_stage: "fetching",
          progress_pct: 92,
          updated_at: "2026-07-13T00:00:12Z",
        },
      ],
    },
  ];

  const merged = mergeCanvasDocumentByRevision(current, incoming);
  assert.equal(merged.recent_executions[0]?.tasks?.[0]?.progress_pct, 92);
  assert.equal(
    merged.recent_executions[0]?.tasks?.[0]?.progress_stage,
    "fetching",
  );
});

test("equal document revisions preserve newer run and selection projections", () => {
  const current = document(4);
  current.active_runs = [
    {
      id: "run-1",
      status: "running",
      last_event_seq: 8,
      updated_at: "2026-07-13T00:00:08Z",
    },
  ];
  current.selections = [
    {
      node_id: "image-1",
      execution_id: "execution-new",
      output_index: 1,
      revision: 3,
    },
  ];
  const incoming = document(4);
  incoming.active_runs = [
    {
      id: "run-1",
      status: "queued",
      last_event_seq: 7,
      updated_at: "2026-07-13T00:00:09Z",
    },
  ];
  incoming.selections = [
    {
      node_id: "image-1",
      execution_id: "execution-old",
      output_index: 0,
      revision: 2,
    },
  ];

  const merged = mergeCanvasDocumentByRevision(current, incoming);
  assert.equal(merged.active_runs[0]?.status, "running");
  assert.equal(merged.selections[0]?.execution_id, "execution-new");
});

test("older equal-revision snapshots cannot delete newer active projections", () => {
  const current = document(4);
  current.active_runs = [
    {
      id: "run-1",
      status: "running",
      last_event_seq: 9,
      updated_at: "2026-07-13T00:00:09Z",
    },
  ];
  current.recent_executions = [
    {
      id: "execution-1",
      node_id: "image-1",
      node_type: "image_generate",
      status: "running",
      outputs: [],
      updated_at: "2026-07-13T00:00:09Z",
    },
  ];
  const incoming = document(4, "stale metadata");

  const merged = mergeCanvasDocumentByRevision(current, incoming);
  assert.equal(merged.title, "stale metadata");
  assert.equal(merged.active_runs[0], current.active_runs[0]);
  assert.equal(
    merged.recent_executions[0],
    current.recent_executions[0],
  );
});

test("equal document revisions still accept newer projection records", () => {
  const current = document(4);
  current.active_runs = [
    {
      id: "run-1",
      status: "queued",
      last_event_seq: 2,
      updated_at: "2026-07-13T00:00:02Z",
    },
  ];
  const incoming = document(4);
  incoming.active_runs = [
    {
      id: "run-1",
      status: "succeeded",
      last_event_seq: 3,
      updated_at: "2026-07-13T00:00:03Z",
    },
  ];

  const merged = mergeCanvasDocumentByRevision(current, incoming);
  assert.equal(merged.active_runs[0], incoming.active_runs[0]);
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
