"use client";

import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { Onboarding } from "@/components/Onboarding";
import { Button } from "@/components/ui/primitives";
import { useChatStore } from "@/store/useChatStore";
import { isAbortLike, errorMessage } from "@/lib/errorUtils";

/** Same starting point on every screen; the shell owns scrolling and safe areas. */
export function MobileEmptyStudio({
  onPick,
}: {
  onPick: (text: string, mode: "chat" | "image") => void;
}) {
  const currentConvId = useChatStore((state) => state.currentConvId);
  const loadHistoricalMessages = useChatStore((state) => state.loadHistoricalMessages);
  const storeLoading = useChatStore((state) => state.messagesLoading);
  const storeError = useChatStore((state) => state.messagesError);
  const [fallbackLoading, setFallbackLoading] = useState(false);
  const [fallbackError, setFallbackError] = useState<string | null>(null);
  const loading = storeLoading || fallbackLoading;
  const error = errorMessage(storeError) ?? fallbackError;

  const handleRetryHistory = async () => {
    if (!currentConvId || loading) return;
    setFallbackLoading(true);
    setFallbackError(null);
    try {
      await loadHistoricalMessages(currentConvId, false);
    } catch (err) {
      if (!isAbortLike(err)) setFallbackError(errorMessage(err) ?? "消息加载失败，重试");
    } finally {
      setFallbackLoading(false);
    }
  };

  return (
    <div className="min-w-0 px-1 pb-4">
      {error ? (
        <div role="alert" className="mt-6 flex flex-wrap items-center gap-2 rounded-[var(--radius-panel)] border border-danger-border bg-danger-soft px-3 py-3 type-body-sm text-[var(--fg-0)]">
          <AlertTriangle className="h-4 w-4 shrink-0 text-[var(--danger-fg)]" aria-hidden />
          <span className="min-w-0 flex-1 break-words">{error}</span>
          <Button size="sm" variant="outline" loading={loading} onClick={() => void handleRetryHistory()}>重试</Button>
        </div>
      ) : (
        <Onboarding
          loading={loading}
          onPick={(text, mode) => {
            if (loading) return;
            onPick(text, mode);
          }}
        />
      )}
    </div>
  );
}
