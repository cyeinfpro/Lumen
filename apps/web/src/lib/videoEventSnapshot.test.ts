import assert from "node:assert/strict";
import test from "node:test";

import type { VideoGenerationOut } from "./types";

const {
  isTerminalVideoEvent,
  mergeVideoGenerationEvent,
  mergeVideoGenerationSnapshot,
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
