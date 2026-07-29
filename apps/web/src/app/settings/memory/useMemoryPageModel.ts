"use client";

import { useMemo, useState } from "react";

import { useUserQueryScope } from "@/components/QueryProvider";
import {
  exportMemories,
  type MemoryItemOut,
  type MemoryType,
} from "@/lib/apiClient";

import { useMemoryMutations } from "./mutations/useMemoryMutations";
import { isEmptyFirstRun, removeEditValue } from "./memoryPageUtils";
import { useMemoryQueries } from "./queries/useMemoryQueries";
import type { MemoryPatchBody } from "./types";

export function useMemoryPageModel() {
  const userScope = useUserQueryScope();
  const [selectedScope, setSelectedScope] = useState("all");
  const [newScopeName, setNewScopeName] = useState("");
  const [newScopeEmoji, setNewScopeEmoji] = useState("");
  const [newMemoryType, setNewMemoryType] =
    useState<MemoryType>("preference");
  const [newMemoryContent, setNewMemoryContent] = useState("");
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [stagingEdits, setStagingEdits] = useState<Record<string, string>>({});
  const [clearText, setClearText] = useState("");
  const [memorySearch, setMemorySearch] = useState("");
  const [selectedMemoryIds, setSelectedMemoryIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [showCapabilityModal, setShowCapabilityModal] = useState(false);

  const queries = useMemoryQueries(userScope, selectedScope);
  const mutations = useMemoryMutations({
    userScope,
    stagingEdits,
    callbacks: {
      onMemoryCreated: () => setNewMemoryContent(""),
      onScopeCreated: () => {
        setNewScopeName("");
        setNewScopeEmoji("");
      },
      onScopeDeleted: () => setSelectedScope("all"),
      onMemoriesCleared: () => setClearText(""),
      onBulkScopeMoved: () => setSelectedMemoryIds(new Set()),
    },
  });

  const scopes = useMemo(
    () => queries.scopesQuery.data ?? [],
    [queries.scopesQuery.data],
  );
  const defaultScope = scopes.find((scope) => scope.is_default) ?? scopes[0];
  const memories = useMemo(
    () => queries.memoriesQuery.data?.items ?? [],
    [queries.memoriesQuery.data],
  );
  const filteredMemories = useMemo(() => {
    const query = memorySearch.trim().toLowerCase();
    if (!query) return memories;
    return memories.filter((memory) => {
      const scope = scopes.find((item) => item.id === memory.scope_id);
      return [
        memory.content,
        memory.source_excerpt ?? "",
        memory.type,
        scope?.name ?? "",
        scope?.emoji ?? "",
      ]
        .join(" ")
        .toLowerCase()
        .includes(query);
    });
  }, [memories, memorySearch, scopes]);
  const typeCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const memory of memories) {
      counts[memory.type] = (counts[memory.type] ?? 0) + 1;
    }
    return counts;
  }, [memories]);

  const embeddingAvailable =
    queries.settingsQuery.data?.embedding_available ?? true;
  const emptyFirstRun = isEmptyFirstRun({
    settingsPending: queries.settingsQuery.isPending,
    memoriesPending: queries.memoriesQuery.isPending,
    memoryCount: memories.length,
    onboardingSeen: queries.settingsQuery.data?.onboarding_seen ?? 0,
  });

  function requestEnableMemory(next: boolean) {
    if (!userScope.enabled) return;
    if (!embeddingAvailable && next) {
      setShowCapabilityModal(true);
      return;
    }
    mutations.settingsMutation.mutate({ disabled: !next });
  }

  function saveMemoryEdit(memory: MemoryItemOut) {
    const content = editing[memory.id]?.trim();
    if (content && content !== memory.content) {
      mutations.patchMemoryMutation.mutate({
        id: memory.id,
        body: { content },
      });
    }
    setEditing((previous) => removeEditValue(previous, memory.id));
  }

  async function exportJson() {
    if (!userScope.enabled) return;
    const data = await exportMemories();
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `lumen-memory-${new Date().toISOString().slice(0, 10)}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  return {
    capabilityBanner: {
      available: embeddingAvailable,
    },
    settingsToggles: {
      settings: queries.settingsQuery.data,
      embeddingAvailable,
      pending: mutations.settingsMutation.isPending,
      onEnableChange: requestEnableMemory,
      onPausedChange: (paused: boolean) =>
        mutations.settingsMutation.mutate({ paused }),
      onConfirmationChange: (confirmation_enabled: boolean) =>
        mutations.settingsMutation.mutate({ confirmation_enabled }),
    },
    firstRun: {
      visible: emptyFirstRun,
      onPause: () => mutations.settingsMutation.mutate({ paused: true }),
      onConfirm: () => mutations.onboardingMutation.mutate(0),
    },
    scopeSidebar: {
      scopes,
      selectedScope,
      newScopeName,
      newScopeEmoji,
      creating: mutations.createScopeMutation.isPending,
      onSelectScope: setSelectedScope,
      onRenameScope: (id: string, name: string) =>
        mutations.patchScopeMutation.mutate({ id, body: { name } }),
      onDeleteScope: (id: string) =>
        mutations.deleteScopeMutation.mutate(id),
      onNewScopeNameChange: setNewScopeName,
      onNewScopeEmojiChange: setNewScopeEmoji,
      onCreateScope: () =>
        mutations.createScopeMutation.mutate({
          name: newScopeName.trim(),
          emoji: newScopeEmoji.trim() || null,
        }),
    },
    manualMemory: {
      typeCounts,
      memoryType: newMemoryType,
      content: newMemoryContent,
      creating: mutations.createMemoryMutation.isPending,
      onTypeChange: setNewMemoryType,
      onContentChange: setNewMemoryContent,
      onCreate: () =>
        mutations.createMemoryMutation.mutate({
          type: newMemoryType,
          content: newMemoryContent.trim(),
          scope_id:
            selectedScope === "all"
              ? (defaultScope?.id ?? null)
              : selectedScope,
        }),
    },
    memoryLibrary: {
      memories,
      filteredMemories,
      scopes,
      selectedScope,
      selectedMemoryIds,
      editing,
      search: memorySearch,
      pending: queries.memoriesQuery.isPending,
      error: queries.memoriesQuery.error,
      bulkMoving: mutations.bulkScopeMutation.isPending,
      onRefresh: () => void queries.memoriesQuery.refetch(),
      onExport: () => void exportJson(),
      onSearchChange: setMemorySearch,
      onBulkMove: (scopeId: string) =>
        mutations.bulkScopeMutation.mutate({
          ids: Array.from(selectedMemoryIds),
          scopeId,
        }),
      onToggleSelected: (id: string, checked: boolean) =>
        setSelectedMemoryIds((previous) => {
          const next = new Set(previous);
          if (checked) next.add(id);
          else next.delete(id);
          return next;
        }),
      onEditValue: (id: string, value: string) =>
        setEditing((previous) => ({ ...previous, [id]: value })),
      onSaveEdit: saveMemoryEdit,
      onCancelEdit: (id: string) =>
        setEditing((previous) => removeEditValue(previous, id)),
      onPatch: (id: string, body: MemoryPatchBody) =>
        mutations.patchMemoryMutation.mutate({ id, body }),
      onDelete: (id: string) =>
        mutations.deleteMemoryMutation.mutate(id),
    },
    memoryStaging: {
      staging: queries.stagingQuery.data?.items ?? [],
      scopes,
      edits: stagingEdits,
      pending: queries.stagingQuery.isPending,
      onEdit: (id: string, value: string) =>
        setStagingEdits((previous) => ({ ...previous, [id]: value })),
      onScopeChange: (id: string, scopeId: string) =>
        mutations.patchStagingMutation.mutate({
          id,
          body: { scope_id: scopeId },
        }),
      onAccept: mutations.acceptStagingMutation.mutate,
      onReject: mutations.rejectStagingMutation.mutate,
    },
    timelineAndClear: {
      events: queries.timelineQuery.data?.items ?? [],
      timelinePending: queries.timelineQuery.isPending,
      clearText,
      clearing: mutations.clearMemoriesMutation.isPending,
      onClearTextChange: setClearText,
      onClear: () => mutations.clearMemoriesMutation.mutate(),
    },
    capabilityModal: {
      open: showCapabilityModal,
      onClose: () => setShowCapabilityModal(false),
    },
  };
}
