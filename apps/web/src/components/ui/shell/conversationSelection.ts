export function firstActiveConversation<T extends { archived: boolean }>(
  conversations: readonly T[],
): T | null {
  // The conversations API is ordered by last_activity_at descending.
  return conversations.find((conversation) => !conversation.archived) ?? null;
}
