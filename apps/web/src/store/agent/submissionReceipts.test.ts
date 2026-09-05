import assert from "node:assert/strict";
import test from "node:test";
import "../chat/moduleResolution.test-helper.mjs";
import { createAgentDraft, type AgentRun } from "../../features/agent/model/contracts.ts";
const { acknowledgeAgentDraft, agentDraftFingerprint } = await import("./submissionReceipts.ts");
const { deserializeAgentDrafts, serializeAgentDrafts } = await import("./draftPersistence.ts");

function submittedDraft() {
  const draft = createAgentDraft({ text: "pending text" });
  return { ...draft, pendingSubmissions: [{ key: "original-key", payloadFingerprint: "payload", draftFingerprint: agentDraftFingerprint(draft) }] };
}
const confirmed = { id: "real-run", idempotency_key: "original-key" } as AgentRun;

test("persisted Agent receipt survives reload only for its owner and clears the confirmed unchanged draft", () => {
  const raw = serializeAgentDrafts("owner", { session: submittedDraft() });
  assert.deepEqual(deserializeAgentDrafts(raw, "other"), {});
  const restored = deserializeAgentDrafts(raw, "owner").session;
  assert.equal(restored.pendingSubmissions?.[0].key, "original-key");
  const acknowledged = acknowledgeAgentDraft(restored, [confirmed]);
  assert.equal(acknowledged.text, "");
  assert.deepEqual(acknowledged.pendingSubmissions, []);
});

test("Agent snapshot acknowledgement retires only matching real receipts and preserves later edits", () => {
  const draft = submittedDraft();
  assert.equal(acknowledgeAgentDraft(draft, [{ ...confirmed, id: "optimistic:run" }]), draft);
  assert.equal(acknowledgeAgentDraft(draft, [{ ...confirmed, idempotency_key: "another-key" }]), draft);
  const changed = { ...draft, text: "a subsequent edit" };
  const acknowledged = acknowledgeAgentDraft(changed, [confirmed]);
  assert.equal(acknowledged.text, "a subsequent edit");
  assert.deepEqual(acknowledged.pendingSubmissions, []);
});
