"use client";

import { useDeferredValue, useEffect, useMemo, useState } from "react";
import { ApiError } from "@/lib/api/errors";
import {
  useAgentMessagesQuery,
  useAgentSessionQuery,
  useAgentSessionsQuery,
} from "../api/queries";
import { useAgentStore } from "@/store/agent/useAgentStore";
import { useAgentSessionRouteSync } from "./useAgentSessionRouteSync";

function agentSessionRouteLoading({
  requestedSessionId,
  requestedLoaded,
  requestedPending,
  listLoading,
}: {
  requestedSessionId: string | null;
  requestedLoaded: boolean;
  requestedPending: boolean;
  listLoading: boolean;
}): boolean {
  return listLoading || Boolean(
    requestedSessionId && !requestedLoaded && requestedPending,
  );
}

export function useAgentSessionDirectory({
  currentSessionId,
  requestedSessionId,
  applyRunSnapshot,
}: {
  currentSessionId: string | null;
  requestedSessionId: string | null;
  applyRunSnapshot: ReturnType<typeof useAgentStore.getState>["applyRunSnapshot"];
}) {
  const [sessionSearch, setSessionSearch] = useState("");
  const deferredSearch = useDeferredValue(sessionSearch.trim());
  const sessionsById = useAgentStore((state) => state.sessions);
  const sessionOrder = useAgentStore((state) => state.sessionOrder);
  const setCurrentSession = useAgentStore((state) => state.setCurrentSession);
  const replaceSessions = useAgentStore((state) => state.replaceSessions);
  const upsertSession = useAgentStore((state) => state.upsertSession);
  const sessionsQuery = useAgentSessionsQuery({
    limit: 40,
    ...(deferredSearch ? { q: deferredSearch } : {}),
  });
  const querySessions = useMemo(
    () => sessionsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [sessionsQuery.data],
  );
  useEffect(() => {
    if (querySessions.length === 0 && !sessionsQuery.isSuccess) return;
    if (deferredSearch) {
      for (const session of querySessions) upsertSession(session);
    } else {
      replaceSessions(querySessions);
    }
    for (const session of querySessions) {
      if (session.active_run) applyRunSnapshot(session.active_run);
    }
  }, [
    applyRunSnapshot,
    deferredSearch,
    querySessions,
    replaceSessions,
    sessionsQuery.isSuccess,
    upsertSession,
  ]);
  const sessions = useMemo(
    () => sessionOrder.map((id) => sessionsById[id]).filter(Boolean),
    [sessionOrder, sessionsById],
  );
  const requestedQuery = useAgentSessionQuery(
    requestedSessionId && !sessionsById[requestedSessionId]
      ? requestedSessionId
      : null,
  );
  useEffect(() => {
    if (requestedQuery.data) upsertSession(requestedQuery.data);
  }, [requestedQuery.data, upsertSession]);
  const requestedMissing =
    requestedQuery.error instanceof ApiError && requestedQuery.error.status === 404;
  const routeLoading = agentSessionRouteLoading({
    requestedSessionId,
    requestedLoaded: Boolean(
      requestedSessionId && sessionsById[requestedSessionId],
    ),
    requestedPending:
      requestedQuery.isLoading || (requestedQuery.isError && !requestedMissing),
    listLoading: sessionsQuery.isLoading,
  });
  const { selectWithRoute } = useAgentSessionRouteSync({
    sessionIds: Array.from(
      new Set([
        ...sessions.map((session) => session.id),
        ...(requestedQuery.data ? [requestedQuery.data.id] : []),
      ]),
    ),
    currentSessionId,
    loading: routeLoading,
    onSelect: setCurrentSession,
  });
  return {
    currentSession: currentSessionId
      ? sessionsById[currentSessionId] ??
        (requestedQuery.data?.id === currentSessionId ? requestedQuery.data : null)
      : null,
    sessionsQuery,
    sidebarSessions: deferredSearch ? querySessions : sessions,
    sessionSearch,
    setSessionSearch,
    selectWithRoute,
    setCurrentSession,
    upsertSession,
  };
}

export function useAgentScrollTargetPagination({
  currentSessionId,
  scrollToMessageId,
  messagesQuery,
}: {
  currentSessionId: string | null;
  scrollToMessageId: string | null;
  messagesQuery: ReturnType<typeof useAgentMessagesQuery>;
}) {
  const { data, fetchNextPage, hasNextPage, isFetchingNextPage } = messagesQuery;
  useEffect(() => {
    if (
      !currentSessionId ||
      !scrollToMessageId ||
      !data ||
      isFetchingNextPage ||
      !hasNextPage
    ) {
      return;
    }
    const loaded = useAgentStore.getState().messagesBySession[currentSessionId] ?? [];
    if (!loaded.some((message) => message.id === scrollToMessageId)) {
      void fetchNextPage();
    }
  }, [
    currentSessionId,
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    scrollToMessageId,
  ]);
}
