"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { acquireBodyScrollLock } from "@/hooks/useBodyScrollLock";
import type { ConversationSummary } from "@/lib/apiClient";
import { logWarn } from "@/lib/logger";
import {
  useCreateConversationMutation,
  useDeleteConversationMutation,
  useListConversationsInfiniteQuery,
  usePatchConversationMutation,
} from "@/lib/queries";
import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";
import { titleOf } from "./ConversationItem";

const MOBILE_SIDEBAR_QUERY = "(max-width: 767px)";

export type SidebarTab = "active" | "archived";
export type SidebarBucket = "today" | "yesterday" | "last7" | "older";

export const SIDEBAR_BUCKET_ORDER: SidebarBucket[] = [
  "today",
  "yesterday",
  "last7",
  "older",
];

export const SIDEBAR_BUCKET_LABEL: Record<SidebarBucket, string> = {
  today: "今天",
  yesterday: "昨天",
  last7: "本周",
  older: "更早",
};

interface SidebarControllerOptions {
  embedded: boolean;
  onNavigate?: () => void;
}

interface ConversationPage {
  items: ConversationSummary[];
}

function flattenConversationPages(
  data: { pages: ConversationPage[] } | undefined,
): ConversationSummary[] {
  return data?.pages.flatMap((page) => page.items) ?? [];
}

function dayKeyOf(iso: string): SidebarBucket {
  const timestamp = Date.parse(iso);
  if (!Number.isFinite(timestamp)) return "older";

  const now = new Date();
  const startOfDay = (date: Date) =>
    new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const todayStart = startOfDay(now);
  const timestampValue = new Date(timestamp).getTime();

  if (timestampValue >= todayStart) return "today";
  if (timestampValue >= todayStart - 24 * 3600 * 1000) return "yesterday";
  if (timestampValue >= todayStart - 7 * 24 * 3600 * 1000) return "last7";
  return "older";
}

function deriveConversationState(
  conversations: ConversationSummary[],
  tab: SidebarTab,
  query: string,
) {
  const normalizedQuery = query.trim().toLowerCase();
  const filtered = conversations
    .filter((conversation) =>
      tab === "archived" ? conversation.archived : !conversation.archived,
    )
    .filter(
      (conversation) =>
        !normalizedQuery ||
        titleOf(conversation).toLowerCase().includes(normalizedQuery),
    );
  const grouped: Record<SidebarBucket, ConversationSummary[]> = {
    today: [],
    yesterday: [],
    last7: [],
    older: [],
  };

  for (const conversation of filtered) {
    grouped[dayKeyOf(conversation.last_activity_at)].push(conversation);
  }

  return {
    filtered,
    grouped,
    archivedTotal: conversations.filter((conversation) => conversation.archived)
      .length,
  };
}

function shouldFetchNextConversationPage({
  conversations,
  currentConversationId,
  hasNextPage,
  isFetchingNextPage,
  query,
  tab,
}: {
  conversations: ConversationSummary[];
  currentConversationId: string | null;
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  query: string;
  tab: SidebarTab;
}): boolean {
  if (!hasNextPage || isFetchingNextPage || query.trim()) return false;

  const currentLoaded =
    currentConversationId == null ||
    conversations.some(
      (conversation) => conversation.id === currentConversationId,
    );
  const tabHasResults = conversations.some((conversation) =>
    tab === "archived" ? conversation.archived : !conversation.archived,
  );
  return !currentLoaded || !tabHasResults;
}

function shouldCloseMobileSidebar(embedded: boolean): boolean {
  return (
    !embedded &&
    typeof window !== "undefined" &&
    window.matchMedia(MOBILE_SIDEBAR_QUERY).matches
  );
}

function useMobileSidebarLifecycle({
  sidebarOpen,
  currentConversationId,
  setSidebarOpen,
}: {
  sidebarOpen: boolean;
  currentConversationId: string | null;
  setSidebarOpen: (open: boolean) => void;
}) {
  const triggerElementRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const mobileQuery = window.matchMedia(MOBILE_SIDEBAR_QUERY);
    let releaseScrollLock: (() => void) | null = null;
    const apply = () => {
      const shouldLock = sidebarOpen && mobileQuery.matches;
      if (shouldLock && !releaseScrollLock) {
        releaseScrollLock = acquireBodyScrollLock();
      } else if (!shouldLock && releaseScrollLock) {
        releaseScrollLock();
        releaseScrollLock = null;
      }
    };

    apply();
    mobileQuery.addEventListener("change", apply);
    return () => {
      mobileQuery.removeEventListener("change", apply);
      releaseScrollLock?.();
    };
  }, [sidebarOpen]);

  useEffect(() => {
    if (!sidebarOpen || typeof window === "undefined") return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && active !== document.body) {
      triggerElementRef.current = active;
    }
    const onKey = (event: KeyboardEvent) => {
      if (
        event.key !== "Escape" ||
        !window.matchMedia(MOBILE_SIDEBAR_QUERY).matches
      ) {
        return;
      }
      setSidebarOpen(false);
      const trigger = triggerElementRef.current;
      if (trigger && document.body.contains(trigger)) {
        window.requestAnimationFrame(() => trigger.focus());
      }
    };

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setSidebarOpen, sidebarOpen]);

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      !window.matchMedia(MOBILE_SIDEBAR_QUERY).matches
    ) {
      return;
    }
    setSidebarOpen(false);
    // Route and conversation changes should close only the mobile drawer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentConversationId]);
}

export function useSidebarController({
  embedded,
  onNavigate,
}: SidebarControllerOptions) {
  const { sidebarOpen, toggleSidebar, setSidebarOpen } = useUiStore();
  const currentConversationId = useChatStore((state) => state.currentConvId);
  const setCurrentConversation = useChatStore((state) => state.setCurrentConv);
  const loadHistoricalMessages = useChatStore(
    (state) => state.loadHistoricalMessages,
  );
  const [tab, setTab] = useState<SidebarTab>("active");
  const [query, setQuery] = useState("");
  const [archiveMenuOpen, setArchiveMenuOpen] = useState(false);

  useMobileSidebarLifecycle({
    sidebarOpen,
    currentConversationId,
    setSidebarOpen,
  });

  const list = useListConversationsInfiniteQuery({ limit: 30 });
  const createMutation = useCreateConversationMutation({
    onSuccess: (conversation) => {
      setCurrentConversation(conversation.id);
      onNavigate?.();
    },
  });
  const deleteMutation = useDeleteConversationMutation();
  const patchMutation = usePatchConversationMutation();

  const conversations = useMemo(
    () => flattenConversationPages(list.data),
    [list.data],
  );
  const conversationState = useMemo(
    () => deriveConversationState(conversations, tab, query),
    [conversations, query, tab],
  );

  useEffect(() => {
    const shouldFetch = shouldFetchNextConversationPage({
      conversations,
      currentConversationId,
      hasNextPage: Boolean(list.hasNextPage),
      isFetchingNextPage: list.isFetchingNextPage,
      query,
      tab,
    });
    if (!shouldFetch) return;
    void list.fetchNextPage();
  }, [
    conversations,
    currentConversationId,
    list,
    list.hasNextPage,
    list.isFetchingNextPage,
    query,
    tab,
  ]);

  const createConversation = useCallback(() => {
    if (createMutation.isPending) return;
    createMutation.mutate({});
  }, [createMutation]);

  const selectConversation = useCallback(
    async (conversation: ConversationSummary) => {
      if (conversation.id === currentConversationId) {
        onNavigate?.();
        if (shouldCloseMobileSidebar(embedded)) setSidebarOpen(false);
        return;
      }

      setCurrentConversation(conversation.id);
      try {
        await loadHistoricalMessages(conversation.id);
        onNavigate?.();
        if (shouldCloseMobileSidebar(embedded)) setSidebarOpen(false);
      } catch (error) {
        logWarn("sidebar.load_historical_messages_failed", {
          scope: "sidebar",
          extra: { convId: conversation.id, err: String(error) },
        });
      }
    },
    [
      currentConversationId,
      embedded,
      loadHistoricalMessages,
      onNavigate,
      setCurrentConversation,
      setSidebarOpen,
    ],
  );

  const renameConversation = useCallback(
    (conversation: ConversationSummary, nextTitle: string) => {
      patchMutation.mutate({ id: conversation.id, title: nextTitle });
    },
    [patchMutation],
  );

  const archiveConversation = useCallback(
    (conversation: ConversationSummary, archived: boolean) => {
      patchMutation.mutate({ id: conversation.id, archived });
    },
    [patchMutation],
  );

  const deleteConversation = useCallback(
    (conversation: ConversationSummary) => {
      deleteMutation.mutate(conversation.id, {
        onSuccess: () => {
          if (currentConversationId === conversation.id) {
            setCurrentConversation(null);
          }
        },
      });
    },
    [currentConversationId, deleteMutation, setCurrentConversation],
  );

  const toggleArchiveMenu = useCallback(() => {
    setArchiveMenuOpen((open) => !open);
  }, []);
  const closeArchiveMenu = useCallback(() => setArchiveMenuOpen(false), []);
  const toggleArchiveTab = useCallback(() => {
    setTab((current) => (current === "active" ? "archived" : "active"));
    setArchiveMenuOpen(false);
  }, []);
  const clearQuery = useCallback(() => setQuery(""), []);
  const retryList = useCallback(() => {
    void list.refetch();
  }, [list]);
  const loadMore = useCallback(() => {
    void list.fetchNextPage();
  }, [list]);

  return {
    sidebarOpen,
    toggleSidebar,
    tab,
    query,
    archiveMenuOpen,
    currentConversationId,
    conversations,
    filteredConversations: conversationState.filtered,
    groupedConversations: conversationState.grouped,
    archivedTotal: conversationState.archivedTotal,
    hasResults: conversationState.filtered.length > 0,
    isInitialLoading: list.isLoading && conversations.length === 0,
    listLoading: list.isLoading,
    listError: list.isError,
    hasNextPage: Boolean(list.hasNextPage),
    nextPageError: list.isFetchNextPageError,
    nextPageLoading: list.isFetchingNextPage,
    createPending: createMutation.isPending,
    createError: createMutation.isError,
    createErrorMessage: createMutation.error?.message ?? "未知错误",
    deletePendingId: deleteMutation.isPending
      ? deleteMutation.variables
      : undefined,
    patchPendingId: patchMutation.isPending
      ? patchMutation.variables?.id
      : undefined,
    setQuery,
    closeArchiveMenu,
    toggleArchiveMenu,
    toggleArchiveTab,
    clearQuery,
    createConversation,
    selectConversation,
    renameConversation,
    archiveConversation,
    deleteConversation,
    retryList,
    loadMore,
  };
}

export type SidebarController = ReturnType<typeof useSidebarController>;
