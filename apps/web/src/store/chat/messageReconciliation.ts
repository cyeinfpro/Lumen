import type { BackendCompletion } from "../../lib/apiClient";
import type {
  AssistantMessage,
  Message,
} from "../../lib/types";

export function isTerminalTaskStatus(
  status: string | null | undefined,
): boolean {
  return status === "succeeded" || status === "failed" || status === "canceled";
}

export function shouldAcceptTaskSnapshot(
  currentStatus: string | null | undefined,
  incomingStatus: string | null | undefined,
): boolean {
  return !(
    isTerminalTaskStatus(currentStatus) && !isTerminalTaskStatus(incomingStatus)
  );
}

function compareMessages(a: Message, b: Message): number {
  if (a.created_at !== b.created_at) return a.created_at - b.created_at;
  return a.id.localeCompare(b.id);
}

export function preferredMessageSnapshot(
  current: Message | undefined,
  incoming: Message,
): Message {
  if (
    !current ||
    current.role !== "assistant" ||
    incoming.role !== "assistant"
  ) {
    return incoming;
  }
  if (!shouldAcceptTaskSnapshot(current.status, incoming.status)) {
    return current;
  }
  const currentText = current.text ?? "";
  const incomingText = incoming.text ?? "";
  if (
    isTerminalTaskStatus(current.status) &&
    isTerminalTaskStatus(incoming.status) &&
    currentText.length > incomingText.length
  ) {
    return {
      ...incoming,
      text: currentText,
      last_delta_at: current.last_delta_at,
    };
  }
  return incoming;
}

export function mergeMessagesById(
  existing: Message[],
  incoming: Message[],
): Message[] {
  const byId = new Map<string, Message>();
  for (const message of existing) byId.set(message.id, message);
  for (const message of incoming) {
    byId.set(
      message.id,
      preferredMessageSnapshot(byId.get(message.id), message),
    );
  }
  return Array.from(byId.values()).sort(compareMessages);
}

export function latestPersistedMessageId(
  messages: Message[],
): string | undefined {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const id = messages[index]?.id;
    if (id && !id.startsWith("opt-")) return id;
  }
  return undefined;
}

function assistantStatusFromCompletion(
  status: string | null | undefined,
  fallback: AssistantMessage["status"],
): AssistantMessage["status"] {
  switch (status) {
    case "queued":
      return "pending";
    case "running":
    case "streaming":
      return "streaming";
    case "succeeded":
    case "failed":
    case "canceled":
      return status;
    default:
      return fallback;
  }
}

function applyCompletionStatus(
  message: AssistantMessage,
  fresh: BackendCompletion,
  now: number,
): boolean {
  const status = assistantStatusFromCompletion(fresh.status, message.status);
  if (message.status !== status) {
    message.status = status;
  } else if (status !== "streaming" || message.stream_started_at) {
    return false;
  }
  if (status === "streaming" && !message.stream_started_at) {
    message.stream_started_at = now;
  }
  return true;
}

function applyCompletionText(
  message: AssistantMessage,
  fresh: BackendCompletion,
  now: number,
): boolean {
  const currentText = message.text ?? "";
  const freshIsTerminal = isTerminalTaskStatus(fresh.status);
  if (
    typeof fresh.text === "string" &&
    fresh.text !== message.text &&
    (freshIsTerminal || fresh.text.length >= currentText.length)
  ) {
    message.text = fresh.text;
    message.last_delta_at = now;
    return true;
  }
  if (fresh.status !== "failed" || message.text) return false;
  const errorMessage = fresh.error_message ?? "文本生成失败";
  const code = fresh.error_code ?? "completion_failed";
  message.text = `${errorMessage}（${code}）`;
  message.last_delta_at = now;
  return true;
}

function reconcileCompletionMessage(
  message: Message,
  completionId: string,
  fresh: BackendCompletion,
  now: number,
): Message {
  if (
    message.role !== "assistant" ||
    message.completion_id !== completionId
  ) {
    return message;
  }
  const incomingStatus = assistantStatusFromCompletion(
    fresh.status,
    message.status,
  );
  if (!shouldAcceptTaskSnapshot(message.status, incomingStatus)) {
    return message;
  }
  const next = { ...message };
  const statusChanged = applyCompletionStatus(next, fresh, now);
  const textChanged = applyCompletionText(next, fresh, now);
  return statusChanged || textChanged ? next : message;
}

export function applyCompletionSnapshot(
  messages: Message[],
  completionId: string,
  fresh: BackendCompletion,
  now = Date.now(),
): Message[] {
  const nextMessages = messages.map((message) =>
    reconcileCompletionMessage(message, completionId, fresh, now),
  );
  return nextMessages.some((message, index) => message !== messages[index])
    ? nextMessages
    : messages;
}
