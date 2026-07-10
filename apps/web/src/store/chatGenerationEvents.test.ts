import { deepEqual, equal } from "node:assert/strict";
import { test } from "node:test";

import type { Generation } from "../lib/types";

const {
  coerceGenerationStage,
  reduceGenerationLifecycleEvent,
} = await import(new URL("./chatGenerationEvents.ts", import.meta.url).href);

const BASE_GENERATION: Generation = {
  id: "generation-1",
  message_id: "message-1",
  action: "generate",
  prompt: "test",
  size_requested: "1024x1024",
  aspect_ratio: "1:1",
  input_image_ids: [],
  primary_input_image_id: null,
  status: "running",
  stage: "rendering",
  attempt: 1,
  started_at: 100,
};

test("queued transition derives provider wait state without side effects", () => {
  const patch = reduceGenerationLifecycleEvent(
    "generation.queued",
    {
      reason: "image_provider_unavailable",
      queue_position: 4,
    },
    BASE_GENERATION,
    500,
  );

  deepEqual(patch, {
    status: "queued",
    stage: "queued",
    substage: "waiting_provider",
    queue_position: 4,
    retrying: false,
    waiting_provider: true,
    cancelled: false,
    started_at: 0,
  });
  equal(BASE_GENERATION.status, "running");
});

test("progress transition increments failover and ignores invalid stages", () => {
  const patch = reduceGenerationLifecycleEvent(
    "generation.progress",
    {
      stage: "not-a-stage",
      substage: "provider_selected",
      provider_failover: true,
    },
    { ...BASE_GENERATION, failover_count: 2 },
    500,
  );

  deepEqual(patch, {
    status: "running",
    queue_position: null,
    substage: "provider_selected",
    failover_count: 3,
  });
  equal(coerceGenerationStage("not-a-stage", "rendering"), "rendering");
});

test("retry transition produces deterministic retry metadata", () => {
  const patch = reduceGenerationLifecycleEvent(
    "generation.retrying",
    {
      attempt: 2,
      max_attempts: 4,
      retry_delay_seconds: 3,
      error_code: "provider_busy",
      error_message: "retry later",
    },
    BASE_GENERATION,
    10_000,
  );

  deepEqual(patch, {
    status: "queued",
    stage: "queued",
    substage: "upstream_retrying",
    retrying: true,
    waiting_provider: false,
    cancelled: false,
    started_at: 0,
    attempt: 2,
    max_attempts: 4,
    retry_eta: 13_000,
    retry_error: "retry later",
    error_code: "provider_busy",
    error_message: "retry later",
  });
});
