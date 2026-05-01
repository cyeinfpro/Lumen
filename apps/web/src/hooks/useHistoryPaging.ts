"use client";

// BUG-027: 抽取共享 history paging hook，避免在 3 个 Canvas 组件中复制 ~80 行重复逻辑。
// 被 ConversationCanvas / DesktopConversationCanvas / MobileConversationCanvas 使用。

import {
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useChatStore } from "@/store/useChatStore";
import { isAbortLike, errorMessage } from "@/lib/errorUtils";

type LoadHistoricalMessages = (
  convId: string,
  loadMore?: boolean,
) => Promise<void> | void;

interface HistoryStoreExtras {
  messagesCursor?: string | null;
  historyCursor?: string | null;
  historicalMessagesCursor?: string | null;
  messagesHasMore?: boolean;
  hasMoreMessages?: boolean;
  historyHasMore?: boolean;
  historicalMessagesHasMore?: boolean;
  messagesLoading?: boolean;
  isLoadingMessages?: boolean;
  historyLoading?: boolean;
  historicalMessagesLoading?: boolean;
  messagesError?: unknown;
  messagesLoadError?: unknown;
  historyError?: unknown;
  historicalMessagesError?: unknown;
}

export interface UseHistoryPagingOptions {
  /** IntersectionObserver root element；传入滚动容器时也用于保留历史分页锚点。 */
  scrollRef?: RefObject<HTMLDivElement | null>;
  /** IntersectionObserver rootMargin 字符串（桌面 & 移动不同）。 */
  rootMargin?: string;
}

export interface HistoryPagingResult {
  topSentinelRef: RefObject<HTMLDivElement | null>;
  hasMore: boolean;
  loading: boolean;
  error: string | null;
  loadMore: () => void;
  retry: () => void;
}

interface ScrollAnchorSnapshot {
  root: HTMLDivElement;
  anchorId: string | null;
  anchorTop: number;
  lastScrollHeight: number;
}

const HISTORY_ANCHOR_SELECTOR = "[data-history-scroll-anchor]";

function captureScrollAnchor(
  scrollRef?: RefObject<HTMLDivElement | null>,
): ScrollAnchorSnapshot | null {
  const root = scrollRef?.current;
  if (!root) return null;

  const rootRect = root.getBoundingClientRect();
  const anchors = Array.from(
    root.querySelectorAll<HTMLElement>(HISTORY_ANCHOR_SELECTOR),
  );
  const visibleAnchor = anchors.find((anchor) => {
    const rect = anchor.getBoundingClientRect();
    return rect.bottom > rootRect.top + 1 && rect.top < rootRect.bottom - 1;
  });

  return {
    root,
    anchorId: visibleAnchor?.dataset.historyScrollAnchor ?? null,
    anchorTop: visibleAnchor
      ? visibleAnchor.getBoundingClientRect().top - rootRect.top
      : 0,
    lastScrollHeight: root.scrollHeight,
  };
}

function restoreScrollAnchor(snapshot: ScrollAnchorSnapshot | null) {
  if (!snapshot || typeof window === "undefined") return;

  const findAnchor = () => {
    if (!snapshot.anchorId) return null;
    return Array.from(
      snapshot.root.querySelectorAll<HTMLElement>(HISTORY_ANCHOR_SELECTOR),
    ).find(
      (anchor) => anchor.dataset.historyScrollAnchor === snapshot.anchorId,
    );
  };

  const restore = () => {
    if (!snapshot.root.isConnected) return;

    const rootRect = snapshot.root.getBoundingClientRect();
    const anchor = findAnchor();
    if (anchor) {
      const nextTop = anchor.getBoundingClientRect().top - rootRect.top;
      const delta = nextTop - snapshot.anchorTop;
      if (Math.abs(delta) > 0.5) {
        snapshot.root.scrollTop += delta;
      }
      snapshot.lastScrollHeight = snapshot.root.scrollHeight;
      return;
    }

    const heightDelta = snapshot.root.scrollHeight - snapshot.lastScrollHeight;
    if (Math.abs(heightDelta) > 0.5) {
      snapshot.root.scrollTop += heightDelta;
      snapshot.lastScrollHeight = snapshot.root.scrollHeight;
    }
  };

  window.requestAnimationFrame(() => {
    restore();
    window.requestAnimationFrame(restore);
  });
  window.setTimeout(restore, 120);
  window.setTimeout(restore, 280);
}

export function useHistoryStoreState() {
  const currentConvId = useChatStore((s) => s.currentConvId);
  const loadHistoricalMessages = useChatStore(
    (s) => s.loadHistoricalMessages as LoadHistoricalMessages,
  );
  const hasMore = useChatStore((s) => {
    const extra = s as unknown as HistoryStoreExtras;
    const explicit =
      extra.messagesHasMore ??
      extra.hasMoreMessages ??
      extra.historyHasMore ??
      extra.historicalMessagesHasMore;
    if (typeof explicit === "boolean") return explicit;
    return Boolean(
      extra.messagesCursor ??
        extra.historyCursor ??
        extra.historicalMessagesCursor,
    );
  });
  const loading = useChatStore((s) => {
    const extra = s as unknown as HistoryStoreExtras;
    return Boolean(
      extra.messagesLoading ??
        extra.isLoadingMessages ??
        extra.historyLoading ??
        extra.historicalMessagesLoading,
    );
  });
  const storeError = useChatStore((s) => {
    const extra = s as unknown as HistoryStoreExtras;
    return errorMessage(
      extra.messagesError ??
        extra.messagesLoadError ??
        extra.historyError ??
        extra.historicalMessagesError,
    );
  });

  return { currentConvId, loadHistoricalMessages, hasMore, loading, storeError };
}

export function useHistoryPaging(
  messageCount: number,
  opts: UseHistoryPagingOptions = {},
): HistoryPagingResult {
  const { scrollRef, rootMargin = "96px 0px 0px 0px" } = opts;
  const { currentConvId, loadHistoricalMessages, hasMore, loading, storeError } =
    useHistoryStoreState();
  const topSentinelRef = useRef<HTMLDivElement | null>(null);
  const inFlightRef = useRef<string | null>(null);
  const [fallback, setFallback] = useState<{
    convId: string | null;
    loading: boolean;
    error: string | null;
  }>({ convId: null, loading: false, error: null });
  const fallbackActive = fallback.convId === currentConvId;
  const fallbackLoading = fallbackActive && fallback.loading;
  const fallbackError = fallbackActive ? fallback.error : null;
  const effectiveLoading = loading || fallbackLoading;
  const effectiveError = storeError ?? fallbackError;

  const requestHistory = useCallback(
    async (loadMore: boolean) => {
      if (!currentConvId || inFlightRef.current === currentConvId || effectiveLoading) {
        return;
      }
      const scrollSnapshot = loadMore ? captureScrollAnchor(scrollRef) : null;
      inFlightRef.current = currentConvId;
      setFallback({ convId: currentConvId, loading: true, error: null });
      try {
        await loadHistoricalMessages(currentConvId, loadMore);
      } catch (err) {
        if (!isAbortLike(err)) {
          setFallback({
            convId: currentConvId,
            loading: true,
            error: errorMessage(err) ?? "消息加载失败，请重试",
          });
        }
      } finally {
        if (inFlightRef.current === currentConvId) {
          inFlightRef.current = null;
        }
        setFallback((prev) =>
          prev.convId === currentConvId ? { ...prev, loading: false } : prev,
        );
        restoreScrollAnchor(scrollSnapshot);
      }
    },
    [currentConvId, effectiveLoading, loadHistoricalMessages, scrollRef],
  );

  const loadMore = useCallback(() => {
    void requestHistory(true);
  }, [requestHistory]);

  // 用 ref 保留最新的 messageCount，避免 retry 闭包持有陈旧值；
  // 同时不让 messageCount 触发 retry 重新创建。
  const messageCountRef = useRef(messageCount);
  useEffect(() => {
    messageCountRef.current = messageCount;
  }, [messageCount]);

  const retry = useCallback(() => {
    void requestHistory(messageCountRef.current > 0 && hasMore);
  }, [hasMore, requestHistory]);

  useEffect(() => {
    const sentinel = topSentinelRef.current;
    if (!sentinel || !currentConvId || !hasMore || effectiveLoading || effectiveError) {
      return;
    }
    if (typeof IntersectionObserver === "undefined") return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) {
          void requestHistory(true);
        }
      },
      {
        root: scrollRef?.current ?? null,
        rootMargin,
        threshold: 0.01,
      },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [
    currentConvId,
    effectiveError,
    effectiveLoading,
    hasMore,
    requestHistory,
    scrollRef,
    rootMargin,
  ]);

  return {
    topSentinelRef,
    hasMore,
    loading: effectiveLoading,
    error: effectiveError,
    loadMore,
    retry,
  };
}
