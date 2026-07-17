"use client";

import {
  type CSSProperties,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useSearchParams } from "next/navigation";
import { LandscapeBanner } from "./LandscapeBanner";
import { MobileStudioTopBar } from "./MobileStudioTopBar";
import { MobileTabBar } from "./MobileTabBar";
import { MobileConversationCanvas } from "@/components/ui/chat/mobile/MobileConversationCanvas";
import { MobileComposerPill } from "@/components/ui/composer/mobile/MobileComposerPill";
import { MobileEmptyStudio } from "@/components/ui/chat/mobile/MobileEmptyStudio";
import { TaskIsland } from "@/components/ui/tray/TaskIsland";
import { useChatStore } from "@/store/useChatStore";
import { cn } from "@/lib/utils";
import { useElementBlockSize } from "@/hooks/useElementBlockSize";
import { useConversationRouteSync } from "./useConversationRouteSync";

export function MobileStudio() {
  const messages = useChatStore((s) => s.messages);
  const generations = useChatStore((s) => s.generations);
  const currentConvId = useChatStore((s) => s.currentConvId);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const loadHistoricalMessages = useChatStore((s) => s.loadHistoricalMessages);
  const sendMessage = useChatStore((s) => s.sendMessage);
  const retryAssistant = useChatStore((s) => s.retryAssistant);
  const retryGeneration = useChatStore((s) => s.retryGeneration);
  const regenerateAssistant = useChatStore((s) => s.regenerateAssistant);
  const promoteImageToReference = useChatStore((s) => s.promoteImageToReference);
  const setText = useChatStore((s) => s.setText);
  const setMode = useChatStore((s) => s.setMode);
  // runtime_defaults 由 RuntimeDefaultsBootstrap（layout 级）统一同步到 store

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [composerMetrics, setComposerMetrics] = useState<{
    height: number;
    bottom: number | null;
  }>({
    height: 56,
    bottom: null,
  });
  const [topChromeRef, topChromeHeight] =
    useElementBlockSize<HTMLDivElement>();
  const [taskIslandRef, taskIslandHeight] =
    useElementBlockSize<HTMLDivElement>();
  const handleComposerMetricsChange = useCallback(
    (next: { height: number; bottom: number }) => {
      setComposerMetrics((prev) =>
        Math.abs(prev.height - next.height) < 1 &&
        prev.bottom !== null &&
        Math.abs(prev.bottom - next.bottom) < 1
          ? prev
          : next,
      );
    },
    [],
  );
  const search = useSearchParams();
  const scrollTo = search.get("scrollTo");
  const scrollSignature = useMemo(() => {
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
      ].join(":");
    }

    return [
      messages.length,
      last.id,
      last.role,
      last.text?.length ?? 0,
      last.attachments?.length ?? 0,
    ].join(":");
  }, [messages]);
  const latestIsStreaming = useMemo(() => {
    const last = messages[messages.length - 1];
    return last?.role === "assistant" && last.status === "streaming";
  }, [messages]);

  useConversationRouteSync({
    currentConvId,
    loadHistoricalMessages,
    setCurrentConv,
    rootStartsNew: true,
  });

  const stickToBottomRef = useRef(true);
  const userScrolledUpRef = useRef(false);
  const previousScrollTopRef = useRef(0);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const top = el.scrollTop;
      const distance = el.scrollHeight - top - el.clientHeight;
      const movingUp = top < previousScrollTopRef.current - 1;

      if (movingUp && distance > 24) {
        userScrolledUpRef.current = true;
      }
      if (distance < 24) {
        userScrolledUpRef.current = false;
      }

      stickToBottomRef.current =
        distance < 32 && !userScrolledUpRef.current;
      previousScrollTopRef.current = top;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  // 切换会话：强制回到底部；之后新消息到达只在"贴底"状态下才滚。
  useEffect(() => {
    stickToBottomRef.current = true;
    userScrolledUpRef.current = false;
    previousScrollTopRef.current = 0;
  }, [currentConvId]);

  useEffect(() => {
    if (scrollTo) return; // deep-link 定位时不干扰
    const el = scrollRef.current;
    if (!el) return;
    if (messages.length === 0) {
      requestAnimationFrame(() => {
        el.scrollTo({ top: 0, behavior: "auto" });
      });
      return;
    }
    if (!stickToBottomRef.current) return;
    const activeElement = document.activeElement;
    if (
      activeElement instanceof HTMLElement &&
      activeElement !== el &&
      el.contains(activeElement)
    ) {
      return;
    }
    const prefersReduced =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    requestAnimationFrame(() => {
      el.scrollTo({
        top: el.scrollHeight,
        behavior: prefersReduced || latestIsStreaming ? "auto" : "smooth",
      });
    });
  }, [
    currentConvId,
    generations,
    latestIsStreaming,
    messages.length,
    scrollSignature,
    scrollTo,
  ]);

  const isEmpty = messages.length === 0;
  const overlayGap = taskIslandHeight > 0 ? 20 : 12;
  const composerBottom =
    composerMetrics.bottom === null
      ? "calc(var(--mobile-tabbar-height) + 6px)"
      : `${composerMetrics.bottom}px`;
  const topChromeBlockSize =
    topChromeHeight > 0
      ? `${topChromeHeight}px`
      : "calc(var(--mobile-topbar-h) + 52px + var(--top-banner-stack-height, 0px) + env(safe-area-inset-top, 0px))";

  return (
    <div
      data-app-viewport
      className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col overflow-hidden bg-[var(--bg-0)]"
      style={
        {
          "--mobile-composer-height": `${composerMetrics.height}px`,
          "--mobile-composer-bottom": composerBottom,
          "--mobile-task-island-height": `${taskIslandHeight}px`,
          "--mobile-top-chrome-height": topChromeBlockSize,
          "--bottom-overlay-stack": `calc(var(--mobile-composer-bottom) + var(--mobile-composer-height) + var(--mobile-task-island-height) + ${overlayGap}px)`,
        } as CSSProperties
      }
    >
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <div ref={topChromeRef} className="shrink-0">
        <LandscapeBanner />
        <MobileStudioTopBar />
      </div>

      <main
        ref={scrollRef}
        data-app-scroll
        data-testid="conversation-scroll"
        className="min-h-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain touch-pan-y [scrollbar-gutter:stable]"
        style={{
          paddingBottom: "var(--bottom-overlay-stack)",
          scrollPaddingBottom: "var(--bottom-overlay-stack)",
        }}
      >
        <div
          className={cn(
            "mx-auto max-w-[640px] px-3",
            isEmpty ? "min-h-full flex flex-col justify-center" : "pt-1",
          )}
        >
          {isEmpty ? (
            <MobileEmptyStudio
              onPick={(text, mode) => {
                setText(text);
                setMode(mode);
              }}
            />
          ) : (
            <MobileConversationCanvas
              messages={messages}
              generations={generations}
              scrollRef={scrollRef}
              scrollToMessageId={scrollTo}
              onEditImage={promoteImageToReference}
              onRetryGen={(gid) => void retryGeneration(gid)}
              onRetryText={(id) => void retryAssistant(id)}
              onRegenerate={(id, intent) => {
                if (intent) void regenerateAssistant(id, intent);
              }}
            />
          )}
        </div>
      </main>

      <div
        ref={taskIslandRef}
        data-testid="task-island"
        className="fixed bottom-[calc(var(--mobile-composer-bottom,54px)+var(--mobile-composer-height,48px)+var(--overlay-gap))] left-1/2 z-[calc(var(--z-composer)+1)] max-w-[calc(100vw-32px)] -translate-x-1/2"
      >
        <TaskIsland className="max-w-full shadow-[var(--shadow-2)]" />
      </div>
      <MobileComposerPill
        onSubmit={sendMessage}
        onMetricsChange={handleComposerMetricsChange}
      />
      <MobileTabBar />
    </div>
  );
}
