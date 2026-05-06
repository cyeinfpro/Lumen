"use client";

import { useEffect, useRef } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

interface UseConversationRouteSyncOptions {
  currentConvId: string | null;
  loadHistoricalMessages: (convId: string) => Promise<void>;
  setCurrentConv: (id: string | null) => void;
}

export function useConversationRouteSync({
  currentConvId,
  loadHistoricalMessages,
  setCurrentConv,
}: UseConversationRouteSyncOptions): string | null {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const searchParamsString = searchParams.toString();
  const urlConversationId = (() => {
    const raw = new URLSearchParams(searchParamsString).get("conversationId");
    const trimmed = raw?.trim();
    return trimmed ? trimmed : null;
  })();

  const pendingConversationIdRef = useRef<string | null>(null);
  const syncedConversationIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!urlConversationId) {
      pendingConversationIdRef.current = null;
      syncedConversationIdRef.current = null;
      return;
    }

    if (urlConversationId === syncedConversationIdRef.current) {
      return;
    }

    pendingConversationIdRef.current = urlConversationId;
    setCurrentConv(urlConversationId);
    void loadHistoricalMessages(urlConversationId).catch(() => {});
  }, [loadHistoricalMessages, setCurrentConv, urlConversationId]);

  useEffect(() => {
    if (pendingConversationIdRef.current) {
      if (currentConvId !== pendingConversationIdRef.current) return;
      pendingConversationIdRef.current = null;
      syncedConversationIdRef.current = currentConvId;
      return;
    }

    if (currentConvId === syncedConversationIdRef.current) return;

    const next = new URLSearchParams(searchParamsString);
    if (currentConvId) next.set("conversationId", currentConvId);
    else next.delete("conversationId");
    const nextString = next.toString();
    if (nextString === searchParamsString) {
      syncedConversationIdRef.current = currentConvId;
      return;
    }

    syncedConversationIdRef.current = currentConvId;
    router.replace(nextString ? `${pathname}?${nextString}` : pathname, {
      scroll: false,
    });
  }, [currentConvId, pathname, router, searchParamsString]);

  return urlConversationId;
}
