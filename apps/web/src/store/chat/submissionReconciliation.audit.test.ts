import assert from "node:assert/strict";
import test from "node:test";
import type { AssistantMessage, Generation, Message } from "../../lib/types";

const { mergeSubmissionMessages, mergeSubmissionGenerations } = await import(
  new URL("./submissionReconciliation.ts", import.meta.url).href
);

function assistant(id: string, status: AssistantMessage["status"] = "pending"): AssistantMessage {
  return { id, role: "assistant", parent_user_message_id: "user-1",
    intent_resolved: "text_to_image", status, created_at: 1 };
}
function generation(status: Generation["status"]): Generation {
  return { id: "gen-1", message_id: "asst-1", action: "generate", prompt: "cat",
    size_requested: "1024x1024", aspect_ratio: "1:1", input_image_ids: [],
    primary_input_image_id: null, status, stage: "finalizing", attempt: 2, started_at: 1 };
}

test("late submission confirmation does not duplicate a realtime message", () => {
  const current = { ...assistant("asst-1", "succeeded"), text: "finished" };
  const result = mergeSubmissionMessages([current], [assistant("asst-1")]);
  assert.equal(result.length, 1);
  assert.equal(result[0].status, "succeeded");
  assert.equal(result[0].text, "finished");
});

test("optimistic and realtime IDs reconcile into one actual message", () => {
  const existing: Message[] = [assistant("opt-asst"), assistant("asst-1", "streaming")];
  const result = mergeSubmissionMessages(existing, [assistant("asst-1")], { "opt-asst": "asst-1" });
  assert.deepEqual(result.map((message: Message) => message.id), ["asst-1"]);
  assert.equal(result[0].status, "streaming");
});

test("batch acknowledgements retain every returned generation ID", () => {
  const ack = { ...assistant("asst-1"), generation_ids: ["g1", "g2", "g3"] };
  const current = { ...assistant("asst-1", "streaming"), generation_id: "g1" };
  const result = mergeSubmissionMessages([current], [ack]);
  assert.deepEqual(result[0].generation_ids, ["g1", "g2", "g3"]);
});

test("an empty batch list still preserves a realtime legacy generation ID", () => {
  const current = { ...assistant("asst-1", "streaming"),
    generation_ids: [], generation_id: "early-generation" };
  const ack = { ...assistant("asst-1"), generation_ids: ["g1", "g2"] };
  const result = mergeSubmissionMessages([current], [ack]);
  assert.deepEqual(result[0].generation_ids, ["g1", "g2", "early-generation"]);
  assert.equal(result[0].status, "streaming");
});

test("an empty acknowledgement batch list does not discard its primary ID", () => {
  const current = { ...assistant("asst-1", "succeeded"), generation_ids: ["current"] };
  const ack = { ...assistant("asst-1"), generation_ids: [], generation_id: "ack-primary" };
  const result = mergeSubmissionMessages([current], [ack]);
  assert.deepEqual(result[0].generation_ids, ["ack-primary", "current"]);
  assert.equal(result[0].status, "succeeded");
});

test("queued placeholders cannot replace an existing terminal task", () => {
  const completed = { ...generation("succeeded"), finished_at: 3 };
  const result = mergeSubmissionGenerations({ "gen-1": completed }, { "gen-1": generation("queued") });
  assert.strictEqual(result["gen-1"], completed);
});

test("only missing placeholders are added without mutating inputs", () => {
  const running = generation("running");
  const current = { "gen-1": running };
  const next = { ...generation("queued"), id: "gen-2" };
  const result = mergeSubmissionGenerations(current, { "gen-2": next });
  assert.deepEqual(Object.keys(current), ["gen-1"]);
  assert.strictEqual(result["gen-1"], running);
  assert.strictEqual(result["gen-2"], next);
});

test("repeated acknowledgements remain idempotent", () => {
  const ack = { ...assistant("asst-1"), generation_ids: ["g1", "g2"] };
  const once = mergeSubmissionMessages([], [ack]);
  const twice = mergeSubmissionMessages(once, [ack]);
  assert.deepEqual(twice, once);
});
