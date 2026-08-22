"use client";

import { useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";

export function useAgentSessionRouteSync({
  sessionIds,
  currentSessionId,
  loading,
  onSelect,
}: {
  sessionIds: string[];
  currentSessionId: string | null;
  loading: boolean;
  onSelect: (sessionId: string | null) => void;
}) {
  const router = useRouter();
  const search = useSearchParams();
  const requestedSessionId = search.get("session");

  useEffect(() => {
    if (loading) return;
    const requested =
      requestedSessionId && sessionIds.includes(requestedSessionId)
        ? requestedSessionId
        : null;
    const current =
      currentSessionId && sessionIds.includes(currentSessionId)
        ? currentSessionId
        : null;
    const target = requested ?? current ?? sessionIds[0] ?? null;
    if (target !== currentSessionId) onSelect(target);
    if (target && requestedSessionId !== target) {
      const next = new URLSearchParams(search.toString());
      next.set("session", target);
      next.delete("scrollTo");
      router.replace(`/agent?${next.toString()}`, { scroll: false });
    }
    if (!target && requestedSessionId) {
      const next = new URLSearchParams(search.toString());
      next.delete("session");
      next.delete("scrollTo");
      router.replace(next.size ? `/agent?${next.toString()}` : "/agent", {
        scroll: false,
      });
    }
  }, [
    currentSessionId,
    loading,
    onSelect,
    requestedSessionId,
    router,
    search,
    sessionIds,
  ]);

  const selectWithRoute = (sessionId: string) => {
    onSelect(sessionId);
    const next = new URLSearchParams(search.toString());
    next.set("session", sessionId);
    next.delete("scrollTo");
    router.push(`/agent?${next.toString()}`, { scroll: false });
  };

  return { requestedSessionId, selectWithRoute };
}
