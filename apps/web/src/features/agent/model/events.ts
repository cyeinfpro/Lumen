import type {
  AgentAssistantMessage,
  AgentEventEnvelope,
  AgentRun,
  AgentRunStatus,
} from "./contracts";
import { isAgentRunTerminal } from "./contracts";

export type AgentEventDecision =
  | { accepted: true; nextRun: AgentRun; nextMessage: AgentAssistantMessage }
  | {
      accepted: false;
      reason: "wrong_run" | "stale_epoch" | "stale_sequence" | "terminal";
    };

function eventStatus(
  eventName: AgentEventEnvelope["event_name"],
  fallback: AgentRunStatus,
): AgentRunStatus {
  if (eventName === "agent.run.queued") return "queued";
  if (eventName === "agent.run.started") return "running";
  if (eventName === "agent.run.succeeded") return "succeeded";
  if (eventName === "agent.run.partial") return "partial";
  if (eventName === "agent.run.failed") return "failed";
  if (eventName === "agent.run.cancelled") return "cancelled";
  return fallback;
}

function eventIsTerminal(eventName: AgentEventEnvelope["event_name"]): boolean {
  return (
    eventName === "agent.run.succeeded" ||
    eventName === "agent.run.partial" ||
    eventName === "agent.run.failed" ||
    eventName === "agent.run.cancelled"
  );
}

export function applyAgentEvent(
  run: AgentRun,
  message: AgentAssistantMessage,
  event: AgentEventEnvelope,
): AgentEventDecision {
  if (event.agent_run_id !== run.id || event.assistant_message_id !== message.id) {
    return { accepted: false, reason: "wrong_run" };
  }
  if (event.execution_epoch < run.execution_epoch) {
    return { accepted: false, reason: "stale_epoch" };
  }
  if (
    event.execution_epoch === run.execution_epoch &&
    event.event_seq <= run.last_event_seq
  ) {
    return { accepted: false, reason: "stale_sequence" };
  }
  const nextStatus = eventStatus(event.event_name, run.status);
  if (
    isAgentRunTerminal(run.status) &&
    (!eventIsTerminal(event.event_name) || nextStatus !== run.status)
  ) {
    return { accepted: false, reason: "terminal" };
  }
  const generationIds = Array.from(
    new Set([
      ...message.generationIds,
      ...(event.generation_ids ?? []),
    ]),
  );
  const resetForNewEpoch = event.execution_epoch > run.execution_epoch;
  const baseText = resetForNewEpoch ? "" : message.text;
  const nextText =
    event.event_name === "agent.output.delta" && event.text_delta
      ? `${baseText}${event.text_delta}`
      : baseText;
  return {
    accepted: true,
    nextRun: {
      ...run,
      status: nextStatus,
      execution_epoch: event.execution_epoch,
      last_event_seq: event.event_seq,
      updated_at: new Date().toISOString(),
    },
    nextMessage: {
      ...message,
      text: nextText,
      status: nextStatus,
      generationIds,
      partial: message.partial || nextStatus === "partial",
    },
  };
}

export function parseAgentEventEnvelope(
  eventName: string,
  value: unknown,
): AgentEventEnvelope | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (
    item.event_name !== eventName ||
    typeof item.agent_session_id !== "string" ||
    typeof item.agent_run_id !== "string" ||
    typeof item.assistant_message_id !== "string" ||
    typeof item.execution_epoch !== "number" ||
    typeof item.event_seq !== "number"
  ) {
    return null;
  }
  return item as unknown as AgentEventEnvelope;
}
