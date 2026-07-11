import type {
  AssistantMessage,
  Message,
} from "../../lib/types";
import {
  coerceCompletionToolCalls,
  coerceUsedMemorySummary,
  mergeCompletionToolCall,
} from "#chat-message-adapters";
import { stringArray } from "#chat-payload";

export type SseIdGetter = (key: string) => string | undefined;

export function completionMessageMatches(
  message: AssistantMessage,
  messageId: string | undefined,
  completionId: string | undefined,
): boolean {
  return Boolean(
    (messageId && message.id === messageId) ||
      (completionId && message.completion_id === completionId),
  );
}

export function applyCompletionProgressEvent(
  message: AssistantMessage,
  payload: Record<string, unknown>,
  eventNow: number,
): void {
  message.status = "streaming";
  message.stream_started_at = message.stream_started_at ?? eventNow;
  message.last_delta_at = eventNow;
  const toolCall = coerceCompletionToolCalls([payload.tool_call])[0];
  if (toolCall) {
    message.tool_calls = mergeCompletionToolCall(
      message.tool_calls,
      toolCall,
    );
    return;
  }
  const toolCalls = coerceCompletionToolCalls(payload.tool_calls);
  if (toolCalls.length > 0) message.tool_calls = toolCalls;
}

export function applyCompletionSucceededEvent(
  message: AssistantMessage,
  payload: Record<string, unknown>,
  eventNow: number,
): void {
  message.status = "succeeded";
  if (typeof payload.text === "string") message.text = payload.text;
  const toolCalls = coerceCompletionToolCalls(payload.tool_calls);
  if (toolCalls.length > 0) message.tool_calls = toolCalls;
  const usedMemoryIds = stringArray(payload.used_memory_ids);
  if (usedMemoryIds.length > 0) {
    message.used_memory_ids = usedMemoryIds;
    message.used_memory_summary = coerceUsedMemorySummary(
      payload.used_memory_summary,
    );
  }
  if (typeof payload.confirmation_candidate_id === "string") {
    message.confirmation_candidate_id = payload.confirmation_candidate_id;
  }
  message.last_delta_at = eventNow;
}

export function applyCompletionLifecycleEvent(
  message: AssistantMessage,
  eventName: string,
  payload: Record<string, unknown>,
  getId: SseIdGetter,
  eventNow: number,
): AssistantMessage {
  const next = { ...message };
  switch (eventName) {
    case "completion.started":
      next.status = "streaming";
      next.stream_started_at = next.stream_started_at ?? eventNow;
      next.last_delta_at = next.last_delta_at ?? eventNow;
      break;
    case "completion.progress":
      applyCompletionProgressEvent(next, payload, eventNow);
      break;
    case "completion.queued":
      next.status = "pending";
      next.stream_started_at = undefined;
      next.last_delta_at = undefined;
      break;
    case "completion.succeeded":
      applyCompletionSucceededEvent(next, payload, eventNow);
      break;
    case "completion.failed": {
      next.status = "failed";
      const code = getId("code") ?? "completion_failed";
      const messageText = getId("message") ?? "文本生成失败";
      next.text = `⚠️ ${messageText}（${code}）`;
      next.last_delta_at = eventNow;
      break;
    }
    case "completion.restarted":
      next.status = "pending";
      next.text = "";
      next.thinking = "";
      next.tool_calls = undefined;
      next.stream_started_at = undefined;
      next.last_delta_at = undefined;
      break;
  }
  return next;
}

export function applyCompletionEventToMessage(
  message: Message,
  input: {
    messageId: string | undefined;
    completionId: string | undefined;
    eventName: string;
    payload: Record<string, unknown>;
    getId: SseIdGetter;
    eventNow: number;
  },
): Message {
  if (message.role !== "assistant") return message;
  if (
    !completionMessageMatches(
      message,
      input.messageId,
      input.completionId,
    )
  ) {
    return message;
  }
  return applyCompletionLifecycleEvent(
    message,
    input.eventName,
    input.payload,
    input.getId,
    input.eventNow,
  );
}
