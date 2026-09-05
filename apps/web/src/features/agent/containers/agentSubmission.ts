import { isAmbiguousRequestFailure, semanticRequestFingerprint } from "@/lib/api/semanticIdempotency";
import { agentDraftFingerprint } from "@/store/agent/submissionReceipts";
import { assertAgentRequestIdentity } from "../api/logicalAgentRequests";
import type { PrivateIdentitySnapshot } from "@/lib/auth/privateIdentityEpoch";
import { agentErrorPresentation } from "../model/errors";
import type {
  AgentDraft,
  AgentImageDefaults,
  AgentMessageCreateInput,
  AgentMessage,
  AgentRun,
  AgentSession,
} from "../model/contracts";
import { useAgentStore } from "@/store/agent/useAgentStore";

export interface AgentSubmissionFence {
  current: boolean;
}

export function acquireAgentSubmissionFence(fence: AgentSubmissionFence): boolean {
  if (fence.current) return false;
  fence.current = true;
  return true;
}

export function releaseAgentSubmissionFence(fence: AgentSubmissionFence): void {
  fence.current = false;
}

export function uniqueAgentId(prefix: string): string {
  const id =
    typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${id}`;
}

export function agentDraftHasContent(draft: AgentDraft): boolean {
  return Boolean(draft.text.trim() || draft.attachments.length || draft.files.length);
}

export function clearConfirmedAgentDraft(sessionId: string, submittedDraft: AgentDraft): void {
  const state = useAgentStore.getState();
  if (state.draftsBySession[sessionId] === submittedDraft) state.clearDraftContent(sessionId);
}

export function retainedAgentSubmissionKey(draft: AgentDraft, toolGatewayConfigured: boolean): string | undefined {
  const fingerprint = semanticRequestFingerprint("agent.message", agentMessagePayload(draft, toolGatewayConfigured));
  return draft.pendingSubmissions?.find((receipt) => receipt.payloadFingerprint === fingerprint)?.key;
}

export function rememberAgentSubmission(sessionId: string, draft: AgentDraft, toolGatewayConfigured: boolean, key: string): void {
  const state = useAgentStore.getState();
  const current = state.draftsBySession[sessionId] ?? draft;
  const receipts = current.pendingSubmissions ?? [];
  if (receipts.some((receipt) => receipt.key === key)) return;
  state.setDraft(sessionId, { pendingSubmissions: [...receipts, {
    key,
    payloadFingerprint: semanticRequestFingerprint("agent.message", agentMessagePayload(draft, toolGatewayConfigured)),
    draftFingerprint: agentDraftFingerprint(draft),
  }] });
}

export function agentMessagePayload(
  draft: AgentDraft,
  toolGatewayConfigured: boolean,
): Omit<AgentMessageCreateInput, "idempotency_key"> {
  return {
    text: draft.text,
    attachments: draft.attachments.map((attachment) => ({
      image_id: attachment.imageId,
      role: attachment.role,
      label: attachment.label,
    })),
    files: draft.files.map((file) => ({
      name: file.name,
      mime_type: file.mimeType,
      size: file.size,
      content: file.content,
    })),
    image_defaults: draft.imageDefaults,
    allow_image: draft.allowImage && toolGatewayConfigured,
    allow_web_search: draft.allowWebSearch,
    allow_file_tools: draft.allowFileTools,
    ...(draft.model ? { model: draft.model } : {}),
    ...(draft.reasoningEffort && draft.reasoningEffort !== "auto"
      ? { reasoning_effort: draft.reasoningEffort }
      : {}),
  };
}

function optimisticRun(
  sessionId: string,
  userMessageId: string,
  assistantMessageId: string,
  idempotencyKey: string,
  reasoningEffort: AgentDraft["reasoningEffort"],
  model: string | null,
): AgentRun {
  const now = new Date().toISOString();
  return {
    id: `optimistic:${assistantMessageId}`,
    agent_session_id: sessionId,
    user_message_id: userMessageId,
    assistant_message_id: assistantMessageId,
    status: "queued",
    execution_epoch: 0,
    last_event_seq: 0,
    output_revision: 0,
    output_runtime_seq: 0,
    idempotency_key: idempotencyKey,
    model,
    reasoning_effort:
      reasoningEffort && reasoningEffort !== "auto" ? reasoningEffort : null,
    memory_state: null,
    continuable: false,
    turn_count: 0,
    tool_call_count: 0,
    usage: {},
    error_code: null,
    error_message: null,
    started_at: null,
    finished_at: null,
    cancel_requested_at: null,
    created_at: now,
    updated_at: now,
    references: [],
    tool_calls: [],
  };
}

function optimisticMessages(
  draft: AgentDraft,
  userMessageId: string,
  assistantMessageId: string,
  runId: string,
): [AgentMessage, AgentMessage] {
  const now = new Date().toISOString();
  return [
    {
      id: userMessageId,
      role: "user",
      text: draft.text,
      attachments: draft.attachments.map((attachment) => ({
        image_id: attachment.imageId,
        role: attachment.role,
        label: attachment.label,
      })),
      files: draft.files.map((file) => ({
        name: file.name,
        mime_type: file.mimeType,
        size: file.size,
      })),
      createdAt: now,
      optimistic: true,
    },
    {
      id: assistantMessageId,
      role: "assistant",
      text: "",
      status: "queued",
      agentRunId: runId,
      parentUserMessageId: userMessageId,
      generationIds: [],
      toolCalls: [],
      blocks: [],
      outputRevision: 0,
      outputRuntimeSeq: 0,
      createdAt: now,
      partial: false,
      optimistic: true,
    },
  ];
}

export function agentSubmissionDeliveryIsUncertain(error: unknown): boolean {
  return isAmbiguousRequestFailure(error);
}

export function agentSubmissionErrorPresentation(error: unknown) {
  return agentErrorPresentation(
    agentSubmissionDeliveryIsUncertain(error)
      ? { code: "agent_submission_uncertain" }
      : error,
  );
}

export function agentSubmissionHasDedicatedFeedback(
  sessionId: string | null,
  attempt: { runId: string; assistantMessageId: string } | null,
): boolean {
  if (!sessionId || !attempt) return false;
  const state = useAgentStore.getState();
  if (state.currentSessionId !== sessionId) return false;
  const run = state.runsById[attempt.runId];
  return Boolean(
    run?.id.startsWith("optimistic:") &&
    run.agent_session_id === sessionId &&
    run.assistant_message_id === attempt.assistantMessageId &&
    run.error_code === "agent_submission_uncertain" &&
    state.messagesBySession[sessionId]?.some((message) =>
      message.id === attempt.assistantMessageId && message.optimistic &&
      message.role === "assistant" && message.agentRunId === run.id),
  );
}

export function applyAgentSubmissionFeedback(input: {
  sessionId: string | null;
  attempt: { runId: string; assistantMessageId: string } | null;
  error: unknown;
  setError: (error: string) => void;
  setAction: (action: { href: string; label: string } | null) => void;
}): void {
  if (agentSubmissionHasDedicatedFeedback(input.sessionId, input.attempt)) return;
  const presentation = agentSubmissionErrorPresentation(input.error);
  input.setError(presentation.detail);
  input.setAction(presentation.href && presentation.actionLabel
    ? { href: presentation.href, label: presentation.actionLabel }
    : null);
}

export async function createSessionForSubmission(input: {
  identity: PrivateIdentitySnapshot;
  draft: AgentDraft;
  toolGatewayConfigured: boolean;
  create: (body: {
    image_defaults: AgentImageDefaults;
    allow_image: boolean;
    allow_web_search: boolean;
    allow_file_tools: boolean;
  }) => Promise<AgentSession>;
  upsert: (session: AgentSession) => void;
  migrateDraft: (from: string | null, to: string) => void;
  select: (sessionId: string | null) => void;
  navigate: (sessionId: string) => void;
}): Promise<string> {
  const session = await input.create({
    image_defaults: input.draft.imageDefaults,
    allow_image: input.draft.allowImage && input.toolGatewayConfigured,
    allow_web_search: input.draft.allowWebSearch,
    allow_file_tools: input.draft.allowFileTools,
  });
  assertAgentRequestIdentity(input.identity);
  input.upsert(session);
  input.migrateDraft(null, session.id);
  input.select(session.id);
  input.navigate(session.id);
  return session.id;
}

export async function createAgentSessionFromDraft(input: {
  draft: AgentDraft;
  toolGatewayConfigured: boolean;
  create: (body: {
    image_defaults: AgentImageDefaults;
    allow_image: boolean;
    allow_web_search: boolean;
    allow_file_tools: boolean;
  }) => Promise<AgentSession>;
  upsert: (session: AgentSession) => void;
  migrateDraft: (from: string | null, to: string) => void;
  navigate: (sessionId: string) => void;
  setError: (message: string) => void;
  setAction: (action: { href: string; label: string } | null) => void;
}): Promise<void> {
  try {
    const session = await input.create({
      image_defaults: input.draft.imageDefaults,
      allow_image: input.draft.allowImage && input.toolGatewayConfigured,
      allow_web_search: input.draft.allowWebSearch,
      allow_file_tools: input.draft.allowFileTools,
    });
    input.upsert(session);
    input.migrateDraft(null, session.id);
    input.navigate(session.id);
  } catch (error) {
    const presentation = agentErrorPresentation(error);
    input.setError(presentation.detail);
    input.setAction(
      presentation.href && presentation.actionLabel
        ? { href: presentation.href, label: presentation.actionLabel }
        : null,
    );
  }
}

export function stageOptimisticSubmission(input: {
  sessionId: string;
  draft: AgentDraft;
  append: ReturnType<typeof useAgentStore.getState>["appendOptimistic"];
  idempotencyKey: string;
}) {
  const userMessageId = uniqueAgentId("agent-user");
  const assistantMessageId = uniqueAgentId("agent-assistant");
  const run = optimisticRun(
    input.sessionId,
    userMessageId,
    assistantMessageId,
    input.idempotencyKey,
    input.draft.reasoningEffort,
    input.draft.model,
  );
  const [userMessage, assistantMessage] = optimisticMessages(
    input.draft,
    userMessageId,
    assistantMessageId,
    run.id,
  );
  return input.append({
    sessionId: input.sessionId,
    userMessage,
    assistantMessage,
    run,
  });
}

export function reconcileFailedAgentSubmission(input: {
  sessionId: string | null;
  optimistic: { runId: string; assistantMessageId: string } | null;
  error: unknown;
  discard: ReturnType<typeof useAgentStore.getState>["discardOptimistic"];
  fail: ReturnType<typeof useAgentStore.getState>["failOptimistic"];
}): void {
  if (
    input.sessionId &&
    input.optimistic &&
    !agentSubmissionDeliveryIsUncertain(input.error)
  ) {
    input.discard({
      sessionId: input.sessionId,
      runId: input.optimistic.runId,
    });
    return;
  }
  if (!input.sessionId || !input.optimistic) return;
  input.fail({
    sessionId: input.sessionId,
    runId: input.optimistic.runId,
    assistantMessageId: input.optimistic.assistantMessageId,
    errorCode: "agent_submission_uncertain",
    errorMessage: agentSubmissionErrorPresentation(input.error).detail,
  });
}
