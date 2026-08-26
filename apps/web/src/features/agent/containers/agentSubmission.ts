import { postAgentMessage } from "../api/agentApi";
import { agentErrorPresentation } from "../model/errors";
import type {
  AgentDraft,
  AgentImageDefaults,
  AgentMessageCreateInput,
  AgentMessage,
  AgentRun,
  AgentSession,
} from "../model/contracts";
import { ApiError } from "@/lib/api/errors";
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

export function agentMessageBody(
  draft: AgentDraft,
  toolGatewayConfigured: boolean,
  idempotencyKey: string,
): AgentMessageCreateInput {
  return {
    idempotency_key: idempotencyKey,
    text: draft.text,
    attachments: draft.attachments.map((attachment) => ({
      image_id: attachment.imageId,
      role: attachment.role,
      label: attachment.label,
    })),
    image_defaults: draft.imageDefaults,
    allow_image: draft.allowImage && toolGatewayConfigured,
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
    idempotency_key: idempotencyKey,
    model: null,
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
      createdAt: now,
      partial: false,
      optimistic: true,
    },
  ];
}

function retryableTransportError(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 0 ||
      error.code === "network_error" ||
      error.code === "request_timeout")
  );
}

export function agentSubmissionDeliveryIsUncertain(error: unknown): boolean {
  return retryableTransportError(error);
}

export async function createSessionForSubmission(input: {
  draft: AgentDraft;
  toolGatewayConfigured: boolean;
  create: (body: {
    image_defaults: AgentImageDefaults;
    allow_image: boolean;
  }) => Promise<AgentSession>;
  upsert: (session: AgentSession) => void;
  migrateDraft: (from: string | null, to: string) => void;
  select: (sessionId: string | null) => void;
  navigate: (sessionId: string) => void;
}): Promise<string> {
  const session = await input.create({
    image_defaults: input.draft.imageDefaults,
    allow_image: input.draft.allowImage && input.toolGatewayConfigured,
  });
  input.upsert(session);
  input.migrateDraft(null, session.id);
  input.select(session.id);
  input.navigate(session.id);
  return session.id;
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
  );
  const [userMessage, assistantMessage] = optimisticMessages(
    input.draft,
    userMessageId,
    assistantMessageId,
    run.id,
  );
  input.append({
    sessionId: input.sessionId,
    userMessage,
    assistantMessage,
    run,
  });
  return { userMessageId, assistantMessageId, runId: run.id };
}

export async function postAgentMessageWithTransportRetry(
  sessionId: string,
  body: Parameters<typeof postAgentMessage>[1],
) {
  try {
    return await postAgentMessage(sessionId, body);
  } catch (error) {
    if (!retryableTransportError(error)) throw error;
    return postAgentMessage(sessionId, body);
  }
}

export function failLatestOptimisticSubmission(
  sessionId: string | null,
  error: unknown,
  fail: ReturnType<typeof useAgentStore.getState>["failOptimistic"],
): void {
  if (!sessionId) return;
  const state = useAgentStore.getState();
  const optimistic = (state.messagesBySession[sessionId] ?? []).findLast(
    (message) => message.role === "assistant" && message.optimistic,
  );
  if (optimistic?.role !== "assistant" || !optimistic.agentRunId) return;
  const presentation = agentErrorPresentation(error);
  fail({
    sessionId,
    runId: optimistic.agentRunId,
    assistantMessageId: optimistic.id,
    errorCode:
      error instanceof ApiError ? error.code : "agent_submission_failed",
    errorMessage: presentation.detail,
  });
}

export function reconcileFailedAgentSubmission(input: {
  sessionId: string | null;
  optimistic: { runId: string } | null;
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
  failLatestOptimisticSubmission(input.sessionId, input.error, input.fail);
}
