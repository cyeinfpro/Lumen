import type { RealtimeDomainEvent } from "./contracts";
import type { RealtimeEffectRegistry } from "./eventRouter";

export type LumenRealtimeEffectContext = {
  applyStoreEvent(name: string, payload: Record<string, unknown>): void;
  invalidateTasks(): void;
  invalidateConversations(): void;
  invalidateMemorySettings(): void;
  invalidateConversationMemory(conversationId: string): void;
};

const TASK_EVENTS = new Set([
  "generation.queued",
  "generation.started",
  "generation.succeeded",
  "generation.failed",
  "generation.canceled",
  "generation.retrying",
  "completion.queued",
  "completion.started",
  "completion.succeeded",
  "completion.failed",
  "completion.restarted",
]);

function applyLumenEvent(
  event: RealtimeDomainEvent,
  context: LumenRealtimeEffectContext,
): void {
  context.applyStoreEvent(event.type, event.payload);
  if (TASK_EVENTS.has(event.type)) context.invalidateTasks();
  if (event.type === "conv.renamed") context.invalidateConversations();
  if (event.type === "account_settings_updated") {
    context.invalidateMemorySettings();
  }
  if (event.type === "conversation.memory.updated") {
    const conversationId = event.payload.conversation_id;
    if (typeof conversationId === "string" && conversationId) {
      context.invalidateConversationMemory(conversationId);
    }
  }
}

export function createLumenEffectRegistry(
  eventNames: readonly string[],
  context: LumenRealtimeEffectContext,
): RealtimeEffectRegistry {
  return Object.fromEntries(
    eventNames.map((name) => [
      name,
      {
        apply(event: RealtimeDomainEvent) {
          applyLumenEvent(event, context);
        },
      },
    ]),
  );
}
