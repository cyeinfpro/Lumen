import assert from "node:assert/strict";
import test from "node:test";

import type { CanvasNodeExecution } from "./types";

const {
  canvasExecutionElapsedMs,
  canvasExecutionPrimaryTask,
  canvasExecutionProgressPercent,
  canvasExecutionStageLabel,
  formatCanvasTaskElapsed,
} = await import(
  new URL("./executionPresentation.ts", import.meta.url).href
);

function execution(
  overrides: Partial<CanvasNodeExecution> = {},
): CanvasNodeExecution {
  return {
    id: "execution-1",
    node_id: "video-1",
    node_type: "video_generate",
    status: "running",
    outputs: [],
    ...overrides,
  };
}

test("canvas execution presentation prefers active video task progress", () => {
  const value = execution({
    tasks: [
      {
        id: "task-old",
        kind: "generation",
        status: "succeeded",
        progress_stage: "finished",
        progress_pct: 100,
      },
      {
        id: "task-video",
        kind: "video_generation",
        status: "running",
        progress_stage: "fetching",
        progress_pct: 93,
      },
    ],
  });

  assert.equal(canvasExecutionPrimaryTask(value)?.id, "task-video");
  assert.equal(canvasExecutionProgressPercent(value), 93);
  assert.equal(canvasExecutionStageLabel(value), "取回成品");
});

test("canvas execution presentation derives elapsed time from timestamps", () => {
  const value = execution({
    started_at: "2026-07-15T04:00:00Z",
  });
  assert.equal(
    canvasExecutionElapsedMs(value, Date.parse("2026-07-15T04:01:05Z")),
    65_000,
  );
  assert.equal(formatCanvasTaskElapsed(65_000), "1 分 5 秒");
});

test("successful executions without task percentages report complete", () => {
  assert.equal(
    canvasExecutionProgressPercent(execution({ status: "succeeded" })),
    100,
  );
});
