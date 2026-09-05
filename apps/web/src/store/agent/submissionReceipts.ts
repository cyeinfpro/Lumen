import type { AgentDraft, AgentPendingSubmission, AgentRun } from "@/features/agent/model/contracts";
import { semanticRequestFingerprint } from "@/lib/api/semanticIdempotencySemantics";

/** Preview URLs change on reload; only submitted draft fields identify an edit. */
export function agentDraftFingerprint(draft: AgentDraft): string {
  return semanticRequestFingerprint("agent.draft", {
    text: draft.text,
    model: draft.model,
    attachments: draft.attachments.map(({ imageId, role, label }) => ({ imageId, role, label })),
    files: draft.files,
    allowImage: draft.allowImage,
    allowWebSearch: draft.allowWebSearch,
    allowFileTools: draft.allowFileTools,
    imageDefaults: draft.imageDefaults,
    reasoningEffort: draft.reasoningEffort ?? "auto",
  });
}

export function restoreAgentPendingSubmissions(value: unknown): AgentPendingSubmission[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is AgentPendingSubmission => Boolean(
    item && typeof item.key === "string" && item.key.length > 0 &&
    typeof item.payloadFingerprint === "string" && typeof item.draftFingerprint === "string",
  ));
}

export function acknowledgeAgentDraft(draft: AgentDraft, runs: AgentRun[]): AgentDraft {
  const keys = new Set(runs.filter((run) => !run.id.startsWith("optimistic:")).map((run) => run.idempotency_key));
  const receipts = draft.pendingSubmissions ?? [];
  const confirmed = receipts.filter((receipt) => keys.has(receipt.key));
  if (!confirmed.length) return draft;
  const clearContent = confirmed.some((receipt) => receipt.draftFingerprint === agentDraftFingerprint(draft));
  return {
    ...draft,
    pendingSubmissions: receipts.filter((receipt) => !keys.has(receipt.key)),
    ...(clearContent ? { text: "", attachments: [], files: [] } : {}),
  };
}
