import assert from "node:assert/strict";
import test from "node:test";

import type { VideoGenerationOut } from "./types";

const {
  activeVideoTemporaryDownload,
  isVideoRequestFenceCurrent,
  isTerminalVideoEvent,
  mergeVideoGenerationEvent,
  mergeVideoGenerationLists,
  mergeVideoGenerationSnapshot,
  nextVideoRequestFence,
} = await import(new URL("./videoEventSnapshot.ts", import.meta.url).href);

function generation(
  overrides: Partial<VideoGenerationOut> = {},
): VideoGenerationOut {
  return {
    id: "video-1",
    action: "t2v",
    model: "seedance-2.0",
    prompt: "test",
    reference_media: [],
    duration_s: 5,
    resolution: "720p",
    aspect_ratio: "16:9",
    generate_audio: true,
    status: "running",
    progress_stage: "rendering",
    progress_pct: 40,
    submission_epoch: 2,
    est_token_upper: 1,
    est_cost: { micro: 1, rmb: "0.000001" },
    created_at: "2026-07-10T00:00:00Z",
    updated_at: "2026-07-10T00:00:00Z",
    ...overrides,
  };
}

test("video events ignore lower submission epochs", () => {
  const current = generation();
  const merged = mergeVideoGenerationEvent(current, {
    video_generation_id: current.id,
    submission_epoch: 1,
    status: "submitted",
    stage: "rendering",
    progress_pct: 10,
  });

  assert.equal(merged, current);
});

test("video events ignore same-epoch status and stage regressions", () => {
  const current = generation({
    status: "running",
    progress_stage: "fetching",
    progress_pct: 96,
  });
  const merged = mergeVideoGenerationEvent(current, {
    video_generation_id: current.id,
    submission_epoch: 2,
    status: "submitted",
    stage: "rendering",
    progress_pct: 20,
  });

  assert.equal(merged, current);
});

test("video events accept a newer epoch and terminal events", () => {
  const current = generation({ status: "failed", progress_stage: "finished" });
  const retried = mergeVideoGenerationEvent(current, {
    video_generation_id: current.id,
    submission_epoch: 3,
    status: "submitting",
    stage: "submitting",
    progress_pct: 5,
    error_code: null,
  });
  const finished = mergeVideoGenerationEvent(retried, {
    video_generation_id: current.id,
    submission_epoch: 3,
    status: "succeeded",
    stage: "finished",
    progress_pct: 100,
  });

  assert.equal(retried.status, "submitting");
  assert.equal(retried.error_code, null);
  assert.equal(finished.status, "succeeded");
  assert.equal(isTerminalVideoEvent({ status: "succeeded" }), true);
});

test("video events accept an explicit same-epoch submit retry transition", () => {
  const current = generation({
    status: "submitting",
    progress_stage: "submitting",
    progress_pct: 5,
    submission_epoch: 2,
  });
  const retried = mergeVideoGenerationEvent(current, {
    video_generation_id: current.id,
    submission_epoch: 2,
    status: "queued",
    stage: "queued",
    progress_pct: 5,
    retry_transition: true,
    retry_after_s: 8,
  });

  assert.equal(retried.status, "queued");
  assert.equal(retried.progress_stage, "queued");
  assert.equal(retried.submission_epoch, 2);
});

test("stale HTTP snapshots cannot overwrite newer terminal state", () => {
  const current = generation({
    status: "succeeded",
    progress_stage: "finished",
    progress_pct: 100,
    submission_epoch: 3,
  });
  const stale = generation({
    status: "running",
    progress_stage: "rendering",
    progress_pct: 60,
    submission_epoch: 2,
  });

  assert.equal(mergeVideoGenerationSnapshot(current, stale), current);
});

test("older same-epoch snapshots cannot replace newer task metadata", () => {
  const current = generation({
    updated_at: "2026-07-10T00:00:10Z",
    temporary_download: {
      source: "volcano",
      url: "https://example.test/new",
      expires_at: "2026-07-10T00:20:00Z",
      expires_in_s: 1200,
    },
  });
  const stale = generation({
    updated_at: "2026-07-10T00:00:05Z",
    temporary_download: {
      source: "volcano",
      url: "https://example.test/old",
      expires_at: "2026-07-10T00:10:00Z",
      expires_in_s: 600,
    },
  });

  assert.equal(mergeVideoGenerationSnapshot(current, stale), current);
});

test("older timestamps still allow a real lifecycle advance", () => {
  const current = generation({
    status: "submitted",
    progress_stage: "rendering",
    progress_pct: 20,
    updated_at: "2026-07-10T00:00:10Z",
  });
  const progressed = generation({
    status: "running",
    progress_stage: "fetching",
    progress_pct: 95,
    updated_at: "2026-07-10T00:00:05Z",
  });

  const merged = mergeVideoGenerationSnapshot(current, progressed);
  assert.equal(merged.status, "running");
  assert.equal(merged.progress_stage, "fetching");
  assert.equal(merged.progress_pct, 95);
});

test("request fences require both the current task and epoch", () => {
  const initial = { taskId: "draft:new", epoch: 0 };
  const taskA = nextVideoRequestFence(initial, "task-a");
  const taskB = nextVideoRequestFence(taskA, "task-b");

  assert.equal(isVideoRequestFenceCurrent(taskA, taskA), true);
  assert.equal(isVideoRequestFenceCurrent(taskB, taskA), false);
  assert.equal(
    isVideoRequestFenceCurrent(taskB, { ...taskB, taskId: "task-a" }),
    false,
  );
});

test("a retry response with a new generation id is merged beside the original", () => {
  const original = generation({
    id: "video-original",
    status: "failed",
    progress_stage: "finished",
    progress_pct: 100,
    submission_epoch: 2,
    created_at: "2026-07-10T00:00:00Z",
  });
  const retried = generation({
    id: "video-retry",
    status: "queued",
    progress_stage: "queued",
    progress_pct: 0,
    submission_epoch: 0,
    created_at: "2026-07-10T00:01:00Z",
  });

  const merged = mergeVideoGenerationLists([original], [retried]);

  assert.deepEqual(
    merged.map((item: VideoGenerationOut) => item.id),
    ["video-retry", "video-original"],
  );
  assert.equal(merged[0], retried);
  assert.equal(merged[1], original);
});

test("temporary downloads expire by absolute time, not stale server TTL", () => {
  const item = generation({
    temporary_download: {
      source: "volcano",
      url: " https://example.test/video ",
      expires_at: "2026-07-10T00:10:00Z",
      expires_in_s: 600,
    },
  });
  const active = activeVideoTemporaryDownload(
    item,
    Date.parse("2026-07-10T00:01:00Z"),
  );
  const expired = activeVideoTemporaryDownload(
    item,
    Date.parse("2026-07-10T00:09:40Z"),
  );

  assert.equal(active?.url, "https://example.test/video");
  assert.equal(active?.expires_in_s, 540);
  assert.equal(expired, null);
});
