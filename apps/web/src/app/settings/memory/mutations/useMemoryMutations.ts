"use client";

import { useCallback } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { userMemoryQueryKeys } from "@/components/QueryProvider";
import {
  acceptMemoryStaging,
  clearMemories,
  createMemory,
  createMemoryScope,
  deleteMemory,
  deleteMemoryScope,
  markMemoryOnboardingSeen,
  patchMemory,
  patchMemoryScope,
  patchMemorySettings,
  patchMemoryStaging,
  rejectMemoryStaging,
  type MemoryStagingOut,
} from "@/lib/apiClient";

import type { MemoryPageQueryScope } from "../types";

type MemoryMutationCallbacks = {
  onMemoryCreated: () => void;
  onScopeCreated: () => void;
  onScopeDeleted: () => void;
  onMemoriesCleared: () => void;
  onBulkScopeMoved: () => void;
};

export function useMemoryMutations({
  userScope,
  stagingEdits,
  callbacks,
}: {
  userScope: MemoryPageQueryScope;
  stagingEdits: Record<string, string>;
  callbacks: MemoryMutationCallbacks;
}) {
  const queryClient = useQueryClient();
  const invalidate = useCallback(() => {
    if (!userScope.enabled) return;
    void queryClient.invalidateQueries({
      queryKey: userMemoryQueryKeys.all(userScope.userId),
    });
  }, [queryClient, userScope.enabled, userScope.userId]);

  const settingsMutation = useMutation({
    mutationFn: patchMemorySettings,
    onSuccess: invalidate,
  });
  const onboardingMutation = useMutation({
    mutationFn: markMemoryOnboardingSeen,
    onSuccess: invalidate,
  });
  const createMemoryMutation = useMutation({
    mutationFn: createMemory,
    onSuccess: () => {
      callbacks.onMemoryCreated();
      invalidate();
    },
  });
  const patchMemoryMutation = useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string;
      body: Parameters<typeof patchMemory>[1];
    }) => patchMemory(id, body),
    onSuccess: invalidate,
  });
  const deleteMemoryMutation = useMutation({
    mutationFn: deleteMemory,
    onSuccess: invalidate,
  });
  const createScopeMutation = useMutation({
    mutationFn: createMemoryScope,
    onSuccess: () => {
      callbacks.onScopeCreated();
      invalidate();
    },
  });
  const patchScopeMutation = useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string;
      body: Parameters<typeof patchMemoryScope>[1];
    }) => patchMemoryScope(id, body),
    onSuccess: invalidate,
  });
  const deleteScopeMutation = useMutation({
    mutationFn: deleteMemoryScope,
    onSuccess: () => {
      callbacks.onScopeDeleted();
      invalidate();
    },
  });
  const acceptStagingMutation = useMutation({
    mutationFn: async (item: MemoryStagingOut) => {
      const content = stagingEdits[item.id]?.trim();
      if (content && content !== item.content) {
        await patchMemoryStaging(item.id, { content });
      }
      return acceptMemoryStaging(item.id);
    },
    onSuccess: invalidate,
  });
  const rejectStagingMutation = useMutation({
    mutationFn: rejectMemoryStaging,
    onSuccess: invalidate,
  });
  const patchStagingMutation = useMutation({
    mutationFn: ({
      id,
      body,
    }: {
      id: string;
      body: Parameters<typeof patchMemoryStaging>[1];
    }) => patchMemoryStaging(id, body),
    onSuccess: invalidate,
  });
  const clearMemoriesMutation = useMutation({
    mutationFn: clearMemories,
    onSuccess: () => {
      callbacks.onMemoriesCleared();
      invalidate();
    },
  });
  const bulkScopeMutation = useMutation({
    mutationFn: async ({
      ids,
      scopeId,
    }: {
      ids: string[];
      scopeId: string;
    }) => {
      await Promise.all(
        ids.map((id) => patchMemory(id, { scope_id: scopeId })),
      );
    },
    onSuccess: () => {
      callbacks.onBulkScopeMoved();
      invalidate();
    },
  });

  return {
    settingsMutation,
    onboardingMutation,
    createMemoryMutation,
    patchMemoryMutation,
    deleteMemoryMutation,
    createScopeMutation,
    patchScopeMutation,
    deleteScopeMutation,
    acceptStagingMutation,
    rejectStagingMutation,
    patchStagingMutation,
    clearMemoriesMutation,
    bulkScopeMutation,
  };
}
