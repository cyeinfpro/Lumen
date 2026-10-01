"use client";

import { useEffect } from "react";

import type { ConversationSummary } from "@/lib/apiClient";
import { useChatStore } from "@/store/useChatStore";
import { firstActiveConversation } from "./conversationSelection";

function hasStartedStudioWork(): boolean {
  // Read at effect execution, not render: a late list response must not
  // replace a conversation or draft the user has just started editing.
  const { currentConvId, composer, composerExpanded } = useChatStore.getState();
  return Boolean(
    currentConvId || composerExpanded || composer.text || composer.attachments.length || composer.mask,
  );
}

interface UseDefaultConversationSelectionOptions {
  currentConvId: string | null;
  urlConversationId: string | null;
  conversations: readonly ConversationSummary[];
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => unknown;
  loadHistoricalMessages: (conversationId: string) => Promise<void>;
  setCurrentConv: (conversationId: string | null) => void;
}

export function useDefaultConversationSelection({
  currentConvId,
  urlConversationId,
  conversations,
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  loadHistoricalMessages,
  setCurrentConv,
}: UseDefaultConversationSelectionOptions): void {
  useEffect(() => {
    if (currentConvId || urlConversationId) return;
    if (hasStartedStudioWork()) return;

    const first = firstActiveConversation(conversations);
    if (!first) return;

    setCurrentConv(first.id);
    void loadHistoricalMessages(first.id).catch(() => {});
  }, [
    conversations,
    currentConvId,
    loadHistoricalMessages,
    setCurrentConv,
    urlConversationId,
  ]);

  useEffect(() => {
    if (currentConvId || urlConversationId) return;
    if (hasStartedStudioWork()) return;
    if (!hasNextPage || isFetchingNextPage) return;
    if (firstActiveConversation(conversations)) return;

    void fetchNextPage();
  }, [
    conversations,
    currentConvId,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    urlConversationId,
  ]);
}
