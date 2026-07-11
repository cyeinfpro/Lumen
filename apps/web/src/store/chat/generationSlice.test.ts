import assert from "node:assert/strict";
import test from "node:test";
import type { BackendGeneration } from "../../lib/apiClient";
import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
} from "../../lib/types";
import "./moduleResolution.test-helper.mjs";

const {
  activeGenerationFromBackend,
  aggregateGenerationStatus,
  assistantHasGeneration,
  generationExplainabilityFromBackend,
  generationIdsOfMessage,
  mergeExplainabilityIntoImage,
  mergeUnknownActiveGenerations,
  preferredGenerationSnapshot,
  updateGenerationAssistantStatuses,
} = await import(new URL("./generationSlice.ts", import.meta.url).href);

function generation(
  overrides: Partial<Generation> = {},
): Generation {
  return {
    id: "generation-1",
    message_id: "assistant-1",
    action: "generate",
    prompt: "draw",
    size_requested: "auto",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status: "running",
    stage: "rendering",
    attempt: 1,
    started_at: 10,
    ...overrides,
  };
}

function assistant(
  overrides: Partial<AssistantMessage> = {},
): AssistantMessage {
  return {
    id: "assistant-1",
    role: "assistant",
    parent_user_message_id: "user-1",
    intent_resolved: "text_to_image",
    status: "pending",
    created_at: 1,
    ...overrides,
  };
}

function backendGeneration(
  overrides: Partial<BackendGeneration> = {},
): BackendGeneration {
  return {
    id: "generation-1",
    message_id: "assistant-1",
    action: "generate",
    prompt: "draw",
    size_requested: "auto",
    aspect_ratio: "1:1",
    input_image_ids: [],
    primary_input_image_id: null,
    status: "running",
    progress_stage: "rendering",
    attempt: 1,
    error_code: null,
    error_message: null,
    started_at: "2026-01-01T00:00:00.000Z",
    finished_at: null,
    ...overrides,
  };
}

function image(
  overrides: Partial<GeneratedImage> = {},
): GeneratedImage {
  return {
    id: "image-1",
    data_url: "/images/image-1",
    width: 1024,
    height: 1024,
    parent_image_id: null,
    from_generation_id: "generation-1",
    size_requested: "1024x1024",
    size_actual: "1024x1024",
    ...overrides,
  };
}

test("generation identity helpers retain canonical array references", () => {
  const generationIds = ["generation-1", "generation-2"];
  const message = assistant({ generation_ids: generationIds });

  assert.equal(generationIdsOfMessage(message), generationIds);
  assert.equal(assistantHasGeneration(message, "generation-2"), true);
  assert.equal(assistantHasGeneration(message, "generation-3"), false);
});

test("generation snapshots preserve existing identity for stale and inflight merges", () => {
  const terminal = generation({ status: "succeeded", stage: "finalizing" });
  const stale = generation({ status: "running" });
  assert.equal(preferredGenerationSnapshot(terminal, stale), terminal);

  const inflight = generation({ status: "running", attempt: 2 });
  const duplicate = generation({ status: "running", attempt: 1 });
  assert.equal(preferredGenerationSnapshot(inflight, duplicate), inflight);

  const succeeded = generation({ status: "succeeded", stage: "finalizing" });
  assert.equal(preferredGenerationSnapshot(inflight, succeeded), succeeded);
});

test("unknown active generation merge is identity-aware on no-op", () => {
  const existingGeneration = generation();
  const existing = { [existingGeneration.id]: existingGeneration };

  assert.equal(
    mergeUnknownActiveGenerations(existing, [backendGeneration()]),
    null,
  );

  const merged = mergeUnknownActiveGenerations(existing, [
    backendGeneration({ id: "generation-2" }),
  ]);
  assert.ok(merged);
  assert.notEqual(merged, existing);
  assert.equal(merged["generation-1"], existingGeneration);
  assert.equal(merged["generation-2"]?.id, "generation-2");
});

test("explainability merge preserves image identity when metadata is absent", () => {
  const current = image();
  assert.equal(mergeExplainabilityIntoImage(current, {}), current);

  const explained = mergeExplainabilityIntoImage(current, {
    revised_prompt: "revised",
    trace_id: "trace-1",
  });
  assert.ok(explained);
  assert.notEqual(explained, current);
  assert.equal(explained.metadata_jsonb?.revised_prompt, "revised");
  assert.equal(explained.metadata_jsonb?.trace_id, "trace-1");
});

test("backend snapshots normalize status and explainability fields", () => {
  const backend = backendGeneration({
    status: "queued",
    progress_stage: "queued",
    diagnostics: { revised_prompt: "diagnostic prompt" },
    trace_id: "trace-1",
    queue_position: 3,
  });
  const explainability = generationExplainabilityFromBackend(backend);
  const active = activeGenerationFromBackend(backend);

  assert.equal(explainability.revised_prompt, "diagnostic prompt");
  assert.equal(active.status, "queued");
  assert.equal(active.stage, "queued");
  assert.equal(active.queue_position, 3);
  assert.equal(active.trace_id, "trace-1");
});

test("assistant status aggregation only replaces matching message references", () => {
  const matching = assistant({ generation_ids: ["generation-1"] });
  const unrelated = assistant({
    id: "assistant-2",
    generation_ids: ["generation-2"],
  });
  const generations = {
    "generation-1": generation({
      status: "succeeded",
      stage: "finalizing",
    }),
  };

  assert.equal(
    aggregateGenerationStatus(
      ["generation-1"],
      generations,
      "pending",
    ),
    "succeeded",
  );
  const updated = updateGenerationAssistantStatuses(
    [matching, unrelated],
    "generation-1",
    generations,
  );
  assert.notEqual(updated[0], matching);
  assert.equal((updated[0] as AssistantMessage).status, "succeeded");
  assert.equal(updated[1], unrelated);
});
