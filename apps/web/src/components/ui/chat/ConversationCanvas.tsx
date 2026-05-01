"use client";

import {
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, ArrowDownToLine } from "lucide-react";
import { MessageRow } from "./MessageRow";
import { Button } from "@/components/ui/primitives";
import type { Generation, Intent, Message } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/useChatStore";
import { useHistoryPaging } from "@/hooks/useHistoryPaging";

const EASE_OUT_EXPO = [0.16, 1, 0.3, 1] as const;
const STICK_TO_BOTTOM_PX = 120;
// 从 80 降到 50：移动端长会话挂载更多 row 会感觉明显卡顿（P2-UX）。
const VIRTUALIZE_AFTER = 50;

interface ConversationCanvasProps {
  messages: Message[];
  generations: Record<string, Generation>;
  scrollRef: React.RefObject<HTMLDivElement | null>;
  onEditImage: (imageId: string) => void;
  onRetry: (gen: Generation) => void;
  onRetryText: (assistantId: string) => void;
  onRegenerate: (
    assistantId: string,
    newIntent: Exclude<Intent, "auto">,
  ) => Promise<void>;
}

function generationSignature(generations: Record<string, Generation>): string {
  return Object.values(generations)
    .map((g) => `${g.id}:${g.status}:${g.stage}:${g.image?.id ?? ""}`)
    .join("|");
}

function messageScrollSignature(messages: Message[]): string {
  const last = messages[messages.length - 1];
  if (!last) return "empty";

  if (last.role === "assistant") {
    return [
      messages.length,
      last.id,
      last.status,
      last.text?.length ?? 0,
      last.thinking?.length ?? 0,
      last.last_delta_at ?? 0,
      last.generation_id ?? "",
      last.generation_ids?.join(",") ?? "",
    ].join(":");
  }

  return [
    messages.length,
    last.id,
    last.role,
    last.text?.length ?? 0,
    last.attachments?.length ?? 0,
  ].join(":");
}

function latestAssistantIsStreaming(messages: Message[]): boolean {
  const last = messages[messages.length - 1];
  return last?.role === "assistant" && last.status === "streaming";
}

function HistoryLoadControl({
  sentinelRef,
  hasMore,
  loading,
  error,
  onLoadMore,
  onRetry,
}: {
  sentinelRef: RefObject<HTMLDivElement | null>;
  hasMore: boolean;
  loading: boolean;
  error: string | null;
  onLoadMore: () => void;
  onRetry: () => void;
}) {
  if (!hasMore && !loading && !error) return null;

  return (
    <div ref={sentinelRef} className="relative z-[1] flex justify-center pb-4">
      {error ? (
        <div
          role="alert"
          className={cn(
            "flex max-w-full items-center gap-3 rounded-md border px-3 py-2",
            "border-[var(--danger)]/25 bg-[var(--danger-soft)] text-sm text-[var(--fg-0)]",
          )}
        >
          <AlertTriangle
            className="h-4 w-4 shrink-0 text-[var(--danger)]"
            aria-hidden
          />
          <span className="min-w-0 truncate">{error}</span>
          <Button
            size="sm"
            variant="outline"
            loading={loading}
            onClick={onRetry}
            className="shrink-0"
          >
            重试
          </Button>
        </div>
      ) : (
        <Button
          size="sm"
          variant="ghost"
          loading={loading}
          onClick={onLoadMore}
          disabled={!hasMore && !loading}
          className="text-[var(--fg-2)]"
        >
          {loading ? "正在加载" : "加载更早消息"}
        </Button>
      )}
    </div>
  );
}

export function ConversationCanvas({
  messages,
  generations,
  scrollRef,
  onEditImage,
  onRetry,
  onRetryText,
  onRegenerate,
}: ConversationCanvasProps) {
  const currentConvId = useChatStore((s) => s.currentConvId);
  const stickToBottomRef = useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const historyPaging = useHistoryPaging(messages.length, {
    scrollRef,
    rootMargin: "120px 0px 0px 0px",
  });
  const shouldVirtualize = messages.length > VIRTUALIZE_AFTER;
  const genSignature = useMemo(() => generationSignature(generations), [generations]);
  const scrollSignature = useMemo(
    () => messageScrollSignature(messages),
    [messages],
  );
  const latestIsStreaming = useMemo(
    () => latestAssistantIsStreaming(messages),
    [messages],
  );

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      stickToBottomRef.current = distance < STICK_TO_BOTTOM_PX;
      const shouldShow = distance > STICK_TO_BOTTOM_PX * 2;
      setShowJumpToLatest((prev) => (prev === shouldShow ? prev : shouldShow));
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => el.removeEventListener("scroll", onScroll);
  }, [scrollRef]);

  useEffect(() => {
    stickToBottomRef.current = true;
  }, [currentConvId]);

  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 220,
    overscan: 8,
    enabled: shouldVirtualize,
  });

  const scrollToLatest = useCallback(
    (behavior: ScrollBehavior = "smooth") => {
      const el = scrollRef.current;
      if (!el) return;

      const run = (mode: ScrollBehavior) => {
        if (shouldVirtualize && messages.length > 0) {
          rowVirtualizer.scrollToIndex(messages.length - 1, { align: "end" });
        }
        el.scrollTo({ top: el.scrollHeight, behavior: mode });
      };

      stickToBottomRef.current = true;
      setShowJumpToLatest(false);
      requestAnimationFrame(() => {
        run(behavior);
        requestAnimationFrame(() => run("auto"));
      });
      window.setTimeout(() => run("auto"), 140);
    },
    [messages.length, rowVirtualizer, scrollRef, shouldVirtualize],
  );

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickToBottomRef.current || messages.length === 0) return;
    const prefersReduced =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    scrollToLatest(prefersReduced || latestIsStreaming ? "auto" : "smooth");
  }, [
    currentConvId,
    genSignature,
    latestIsStreaming,
    messages.length,
    scrollRef,
    scrollToLatest,
    scrollSignature,
  ]);

  if (!shouldVirtualize) {
    return (
      <>
        <motion.div
          key="messages"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18, ease: EASE_OUT_EXPO }}
          className="flex flex-col gap-5 md:gap-6"
          role="log"
          aria-live="polite"
          aria-relevant="additions"
        >
          <HistoryLoadControl
            sentinelRef={historyPaging.topSentinelRef}
            hasMore={historyPaging.hasMore}
            loading={historyPaging.loading}
            error={historyPaging.error}
            onLoadMore={historyPaging.loadMore}
            onRetry={historyPaging.retry}
          />
          <AnimatePresence initial={false}>
            {messages.map((msg) => (
              <div
                key={msg.id}
                id={`msg-${msg.id}`}
                data-history-scroll-anchor={msg.id}
              >
                <MessageRow
                  msg={msg}
                  generations={generations}
                  onEditImage={onEditImage}
                  onRetry={onRetry}
                  onRetryText={onRetryText}
                  onRegenerate={onRegenerate}
                />
              </div>
            ))}
          </AnimatePresence>
        </motion.div>
        <JumpToLatestButton
          visible={showJumpToLatest}
          onClick={() => scrollToLatest("smooth")}
        />
      </>
    );
  }

  return (
    <>
      <motion.div
        key="messages-virtual"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.18, ease: EASE_OUT_EXPO }}
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        className="flex flex-col"
      >
        <HistoryLoadControl
          sentinelRef={historyPaging.topSentinelRef}
          hasMore={historyPaging.hasMore}
          loading={historyPaging.loading}
          error={historyPaging.error}
          onLoadMore={historyPaging.loadMore}
          onRetry={historyPaging.retry}
        />
        <div
          className="relative w-full"
          style={{ height: rowVirtualizer.getTotalSize() }}
        >
          {rowVirtualizer.getVirtualItems().map((virtualRow) => {
            const msg = messages[virtualRow.index];
            return (
              <div
                key={msg.id}
                id={`msg-${msg.id}`}
                ref={rowVirtualizer.measureElement}
                data-index={virtualRow.index}
                data-history-scroll-anchor={msg.id}
                className={cn("absolute left-0 top-0 w-full pb-5 md:pb-6")}
                style={{ transform: `translateY(${virtualRow.start}px)` }}
              >
                <MessageRow
                  msg={msg}
                  generations={generations}
                  onEditImage={onEditImage}
                  onRetry={onRetry}
                  onRetryText={onRetryText}
                  onRegenerate={onRegenerate}
                />
              </div>
            );
          })}
        </div>
      </motion.div>
      <JumpToLatestButton
        visible={showJumpToLatest}
        onClick={() => scrollToLatest("smooth")}
      />
    </>
  );
}

function JumpToLatestButton({
  visible,
  onClick,
}: {
  visible: boolean;
  onClick: () => void;
}) {
  if (!visible) return null;

  return (
    <div className="fixed left-1/2 bottom-[calc(96px+env(safe-area-inset-bottom,0px))] z-30 -translate-x-1/2">
      <Button
        size="sm"
        variant="secondary"
        leftIcon={<ArrowDownToLine className="h-3.5 w-3.5" aria-hidden />}
        onClick={onClick}
        className="border-white/15 bg-[var(--bg-1)]/88 shadow-lg backdrop-blur-xl"
      >
        最新
      </Button>
    </div>
  );
}
