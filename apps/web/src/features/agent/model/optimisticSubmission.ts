import type { AgentMessage, AgentRun } from "./contracts";
import { mergeAgentMessageLists } from "./reconciliation";

export interface AgentAttemptHandle {
  userMessageId: string;
  assistantMessageId: string;
  runId: string;
}

export interface AgentOptimisticInput {
  sessionId: string;
  userMessage: AgentMessage;
  assistantMessage: AgentMessage;
  run: AgentRun;
}

type AttemptState = {
  messagesBySession: Record<string, AgentMessage[]>;
  runsById: Record<string, AgentRun>;
};

export function ensureAgentOptimisticAttempt(state: AttemptState, input: AgentOptimisticInput) {
  const { sessionId } = input;
  const matches = Object.values(state.runsById).filter(
    (run) => run.agent_session_id === sessionId && run.idempotency_key === input.run.idempotency_key,
  );
  const existing = matches.find((run) => !run.id.startsWith("optimistic:")) ?? matches[0];
  const run = existing ?? input.run;
  const handle: AgentAttemptHandle = {
    userMessageId: run.user_message_id,
    assistantMessageId: run.assistant_message_id,
    runId: run.id,
  };
  if (existing && !existing.id.startsWith("optimistic:")) return { state, handle };
  const messages = state.messagesBySession[sessionId] ?? [];
  const nextMessages = existing
    ? messages.map((message) => message.id === handle.assistantMessageId && message.role === "assistant"
      ? { ...message, status: "queued" as const }
      : message)
    : mergeAgentMessageLists(messages, [input.userMessage, input.assistantMessage]);
  return {
    handle,
    state: {
      messagesBySession: { ...state.messagesBySession, [sessionId]: nextMessages },
      runsById: {
        ...state.runsById,
        [run.id]: { ...run, status: "queued" as const, error_code: null, error_message: null, finished_at: null },
      },
    },
  };
}
