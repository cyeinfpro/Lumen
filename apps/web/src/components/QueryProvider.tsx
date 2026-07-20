"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { MotionConfig } from "framer-motion";

import { useMediaQuery } from "@/hooks/useMediaQuery";
import { makeQueryClient } from "@/lib/queryClient";
import { clearPreviousUserQueryCache } from "@/lib/queries/userScope";
import { useChatStore } from "@/store/useChatStore";

export * from "@/lib/queries/userScope";

export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => makeQueryClient());
  const showDevtools = useMediaQuery("(min-width: 1024px)");
  const userId = useChatStore((state) => state.currentUserId);
  const previousUserIdRef = useRef<string | null>(userId);

  useLayoutEffect(() => {
    const previousUserId = previousUserIdRef.current;
    if (previousUserId !== userId && previousUserId) {
      clearPreviousUserQueryCache(
        client,
        previousUserId,
      );
    }
    previousUserIdRef.current = userId;
  }, [client, userId]);

  return (
    <QueryClientProvider client={client}>
      <MotionConfig reducedMotion="user">
        {children}
        {process.env.NODE_ENV !== "production" && showDevtools ? (
          <ReactQueryDevtools initialIsOpen={false} buttonPosition="top-left" />
        ) : null}
      </MotionConfig>
    </QueryClientProvider>
  );
}
