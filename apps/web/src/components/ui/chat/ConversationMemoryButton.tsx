"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getConversation,
  getConversationUsedMemories,
  listMemoryScopes,
  patchConversationActiveScope,
  patchConversationMemoryDisabled,
} from "@/lib/apiClient";
import {
  userConversationQueryKeys,
  userMemoryQueryKeys,
  useUserQueryScope,
} from "@/components/QueryProvider";
import { useChatStore } from "@/store/useChatStore";

import { ConversationMemoryButtonView } from "./ConversationMemoryButtonView";

export function ConversationMemoryButton({ compact = false }: { compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const currentConvId = useChatStore((s) => s.currentConvId);
  const userScope = useUserQueryScope();
  const qc = useQueryClient();
  const conversationId = currentConvId ?? "";
  const canQueryConversation =
    userScope.enabled && Boolean(currentConvId);

  const convQ = useQuery({
    queryKey: userConversationQueryKeys.detail(
      userScope.userId,
      conversationId,
    ),
    queryFn: () => getConversation(conversationId),
    enabled: canQueryConversation,
    staleTime: 10_000,
  });
  const scopesQ = useQuery({
    queryKey: userMemoryQueryKeys.scopes(userScope.userId),
    queryFn: listMemoryScopes,
    enabled: open && userScope.enabled,
    staleTime: 30_000,
  });
  const usedQ = useQuery({
    queryKey: userConversationQueryKeys.usedMemories(
      userScope.userId,
      conversationId,
    ),
    queryFn: () => getConversationUsedMemories(conversationId),
    enabled: open && canQueryConversation,
    staleTime: 10_000,
  });

  const invalidateConversation = () => {
    if (!canQueryConversation) return;
    void qc.invalidateQueries({
      queryKey: userConversationQueryKeys.detail(
        userScope.userId,
        conversationId,
      ),
    });
    void qc.invalidateQueries({
      queryKey: userConversationQueryKeys.usedMemories(
        userScope.userId,
        conversationId,
      ),
    });
  };

  const toggleMut = useMutation({
    mutationFn: (disabled: boolean) =>
      patchConversationMemoryDisabled(conversationId, disabled),
    onSuccess: invalidateConversation,
  });
  const scopeMut = useMutation({
    mutationFn: (scopeId: string | null) =>
      patchConversationActiveScope(conversationId, scopeId),
    onSuccess: invalidateConversation,
  });

  const disabled = Boolean(convQ.data?.memory_disabled);
  const activeScopeId = convQ.data?.active_scope_id ?? null;
  const scopes = scopesQ.data ?? [];
  const activeScope = scopes.find((scope) => scope.id === activeScopeId);
  const used = usedQ.data?.used_memory_summary ?? [];

  return (
    <ConversationMemoryButtonView
      compact={compact}
      open={open}
      onToggleOpen={() => setOpen((value) => !value)}
      onClose={() => setOpen(false)}
      canQueryConversation={canQueryConversation}
      disabled={disabled}
      activeScopeName={activeScope?.name}
      activeScopeId={activeScopeId}
      scopes={scopes}
      used={used}
      togglePending={toggleMut.isPending}
      scopePending={scopeMut.isPending}
      onToggleDisabled={() => toggleMut.mutate(!disabled)}
      onScopeChange={(scopeId) => scopeMut.mutate(scopeId)}
    />
  );
}
