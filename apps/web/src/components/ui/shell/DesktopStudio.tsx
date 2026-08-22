"use client";

// 桌面创作外壳：全局 App Bar + 会话 Context Bar + 三态侧栏。

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
  type ReactNode,
} from "react";
import { AnimatePresence, motion } from "framer-motion";

import { DesktopTopNav } from "@/components/ui/shell/DesktopTopNav";
import { Sidebar } from "@/components/ui/Sidebar";
import { Onboarding } from "@/components/Onboarding";
import { DesktopComposerPill } from "@/components/ui/composer/desktop";
import {
  ErrorState,
  Spinner,
} from "@/components/ui/primitives";
import {
  ConversationImageGallery,
  DesktopConversationCanvas,
} from "@/components/ui/chat/desktop";
import { useUiStore } from "@/store/useUiStore";
import { useChatStore } from "@/store/useChatStore";
import {
  useCreateConversationMutation,
  useConversationContextQuery,
  useListConversationsInfiniteQuery,
} from "@/lib/queries";
import { logWarn } from "@/lib/logger";
import { DURATION, EASE } from "@/lib/motion";
import type { Generation, Intent, Message } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { StudioContextBar } from "./StudioContextBar";
import { useDefaultConversationSelection } from "./useDefaultConversationSelection";
import { useConversationRouteSync } from "./useConversationRouteSync";
import {
  DesktopPrivateSidebarDock,
  DesktopPrivateSidebarDrawer,
} from "./PrivateSidebarShell";

declare global {
  interface WindowEventMap {
    "lumen:sidebar-toggle": CustomEvent<void>;
  }
}

type DesktopHistoryState =
  | { kind: "ready" }
  | { kind: "loading"; conversationId: string }
  | { kind: "failed"; conversationId: string; error: string };

const DESKTOP_HISTORY_READY: DesktopHistoryState = { kind: "ready" };

function resolveDesktopHistoryState({
  currentConversationId,
  loading,
  error,
  messageCount,
}: {
  currentConversationId: string | null;
  loading: boolean;
  error: string | null;
  messageCount: number;
}): DesktopHistoryState {
  if (!currentConversationId) return DESKTOP_HISTORY_READY;
  if (error && messageCount === 0) {
    return { kind: "failed", conversationId: currentConversationId, error };
  }
  if (loading && messageCount === 0) {
    return { kind: "loading", conversationId: currentConversationId };
  }
  return DESKTOP_HISTORY_READY;
}

function desktopContentWidthClass({
  studioView,
  isEmpty,
  historyBlocked,
}: {
  studioView: "chat" | "images";
  isEmpty: boolean;
  historyBlocked: boolean;
}): string {
  if (studioView === "images" && !historyBlocked) {
    return "max-w-[var(--content-workbench)]";
  }
  return isEmpty
    ? "max-w-[var(--content-composer)]"
    : "max-w-[var(--content-media)]";
}

export function DesktopStudio() {
  const sidebarOpen = useUiStore((s) => s.sidebarOpen);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);
  const studioView = useUiStore((s) => s.studioView);
  const setStudioView = useUiStore((s) => s.setStudioView);

  const messages = useChatStore((s) => s.messages);
  const messagesLoading = useChatStore((s) => s.messagesLoading);
  const messagesError = useChatStore((s) => s.messagesError);
  const generations = useChatStore((s) => s.generations);
  const currentConvId = useChatStore((s) => s.currentConvId);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const loadHistoricalMessages = useChatStore((s) => s.loadHistoricalMessages);
  const retryAssistant = useChatStore((s) => s.retryAssistant);
  const retryGeneration = useChatStore((s) => s.retryGeneration);
  const regenerateAssistant = useChatStore((s) => s.regenerateAssistant);
  const rerollImage = useChatStore((s) => s.rerollImage);
  const promoteImageToReference = useChatStore((s) => s.promoteImageToReference);
  const setText = useChatStore((s) => s.setText);
  const setMode = useChatStore((s) => s.setMode);
  const composerMode = useChatStore((s) => s.composer.mode);
  const fast = useChatStore((s) => s.composer.fast);
  const setFast = useChatStore((s) => s.setFast);
  const isWideSidebar = useMediaQuery("(min-width: 1440px)");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [composerMetrics, setComposerMetrics] = useState({
    height: 56,
    bottom: 16,
  });
  const sidebarTriggerRef = useRef<HTMLButtonElement | null>(null);
  const workspaceRef = useRef<HTMLDivElement | null>(null);

  const convsQuery = useListConversationsInfiniteQuery({ limit: 30 });
  const conversations = useMemo(
    () => convsQuery.data?.pages.flatMap((page) => page.items) ?? [],
    [convsQuery.data],
  );
  const {
    data: contextStats,
    refetch: refetchContextStats,
  } = useConversationContextQuery(currentConvId, { refetchInterval: 30_000 });
  const urlConversationId = useConversationRouteSync({
    currentConvId,
    loadHistoricalMessages,
    setCurrentConv,
  });
  useDefaultConversationSelection({
    currentConvId,
    urlConversationId,
    conversations,
    hasNextPage: Boolean(convsQuery.hasNextPage),
    isFetchingNextPage: convsQuery.isFetchingNextPage,
    fetchNextPage: convsQuery.fetchNextPage,
    loadHistoricalMessages,
    setCurrentConv,
  });

  useEffect(() => {
    if (!currentConvId) return;
    void refetchContextStats();
  }, [currentConvId, messages.length, refetchContextStats]);

  const handleSidebarToggle = useCallback(() => {
    if (isWideSidebar === true) {
      toggleSidebar();
      return;
    }
    setDrawerOpen((open) => !open);
  }, [isWideSidebar, toggleSidebar]);
  const closeSidebarDrawer = useCallback(() => setDrawerOpen(false), []);

  useEffect(() => {
    const wide = window.matchMedia("(min-width: 1440px)");
    const closeDrawerOnWide = (event: MediaQueryListEvent) => {
      if (event.matches) {
        setDrawerOpen(false);
      }
    };
    wide.addEventListener("change", closeDrawerOnWide);
    return () => wide.removeEventListener("change", closeDrawerOnWide);
  }, []);

  const toggleSidebarRef = useRef(handleSidebarToggle);
  useEffect(() => {
    toggleSidebarRef.current = handleSidebarToggle;
  }, [handleSidebarToggle]);

  // ⌘/Ctrl+B：切换侧栏抽屉；同时监听 Command Palette 派发的自定义事件。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "b" || e.key === "B")) {
        e.preventDefault();
        toggleSidebarRef.current();
      }
    };
    const onCustom = () => toggleSidebarRef.current();
    window.addEventListener("keydown", onKey);
    window.addEventListener("lumen:sidebar-toggle", onCustom);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("lumen:sidebar-toggle", onCustom);
    };
  }, []);

  const handleRetryGen = useCallback(
    (generationId: string) => {
      const gen = useChatStore.getState().generations[generationId];
      if (gen?.status === "succeeded" && gen.image) {
        void rerollImage(gen.image.id);
        return;
      }
      void retryGeneration(generationId);
    },
    [rerollImage, retryGeneration],
  );
  const handleRetryHistory = useCallback(() => {
    const state = useChatStore.getState();
    const conversationId = state.currentConvId;
    if (!conversationId || state.messagesLoading) return;

    void state.loadHistoricalMessages(conversationId).catch((error) => {
      logWarn("desktop_studio.load_historical_messages_failed", {
        scope: "desktop-studio",
        extra: { convId: conversationId, err: String(error) },
      });
    });
  }, []);
  const handleSubmit = useCallback(() => {
    const state = useChatStore.getState();
    const historyUnavailable =
      Boolean(state.currentConvId) &&
      state.messages.length === 0 &&
      (Boolean(state.messagesError) || state.messagesLoading);
    if (historyUnavailable) return;
    return state.sendMessage();
  }, []);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const imageViewScrollKeyRef = useRef<string | null>(null);
  const isEmpty = messages.length === 0;
  const historyState = resolveDesktopHistoryState({
    currentConversationId: currentConvId,
    loading: messagesLoading,
    error: messagesError,
    messageCount: messages.length,
  });
  const historyInteractionBlocked = historyState.kind !== "ready";
  const contentWidthClass = desktopContentWidthClass({
    studioView,
    isEmpty,
    historyBlocked: historyInteractionBlocked,
  });
  const currentTitle = useMemo(() => {
    const current = conversations.find((item) => item.id === currentConvId);
    if (current?.title) return current.title;
    const firstUser = messages.find((message) => message.role === "user");
    return firstUser?.text?.slice(0, 48) || "新对话";
  }, [conversations, currentConvId, messages]);

  useEffect(() => {
    if (studioView !== "images") {
      imageViewScrollKeyRef.current = null;
      return;
    }

    const key = currentConvId ?? "";
    if (imageViewScrollKeyRef.current === key) return;
    imageViewScrollKeyRef.current = key;

    const raf = window.requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: 0, behavior: "auto" });
    });
    return () => window.cancelAnimationFrame(raf);
  }, [currentConvId, studioView]);

  // 侧栏窄栏中的新建动作。
  const createMut = useCreateConversationMutation({
    onSuccess: (conv) => {
      setStudioView("chat");
      setCurrentConv(conv.id);
    },
  });

  const handleComposerMetricsChange = useCallback(
    (next: { height: number; bottom: number }) => {
      setComposerMetrics((previous) =>
        Math.abs(previous.height - next.height) < 1 &&
        Math.abs(previous.bottom - next.bottom) < 1
          ? previous
          : next,
      );
    },
    [],
  );

  return (
    <div
      className="studio-shell relative flex h-[100dvh] min-h-0 flex-col bg-[var(--bg-0)]"
      data-sidebar-open={
        isWideSidebar === true && sidebarOpen ? "true" : "false"
      }
      style={
        {
          "--desktop-composer-height": `${composerMetrics.height}px`,
          "--desktop-composer-bottom": `${composerMetrics.bottom}px`,
        } as CSSProperties
      }
    >
      <div ref={workspaceRef} className="flex min-h-0 flex-1 flex-col">
        <DesktopTopNav
          active="studio"
          onToggleSidebar={handleSidebarToggle}
          sidebarTriggerRef={sidebarTriggerRef}
          sidebarExpanded={
            isWideSidebar === true ? sidebarOpen : drawerOpen
          }
        />

        <div className="flex min-h-0 flex-1">
          <DesktopPrivateSidebarDock
            expanded={isWideSidebar === true && sidebarOpen}
            onToggle={handleSidebarToggle}
            onCreate={() => !createMut.isPending && createMut.mutate({})}
            creating={createMut.isPending}
            label="会话导航"
          >
            <Sidebar embedded />
          </DesktopPrivateSidebarDock>

          <section className="flex min-w-0 flex-1 flex-col">
            <StudioContextBar
              title={currentTitle}
              view={studioView}
              onViewChange={setStudioView}
              composerMode={composerMode}
              fast={fast}
              onFastChange={setFast}
              contextStats={contextStats}
            />

            <main
              ref={scrollRef}
              data-app-scroll
              className="lumen-studio-bg relative min-h-0 flex-1 overflow-x-clip overflow-y-auto"
              style={{
                scrollPaddingBottom:
                  "calc(var(--desktop-composer-height, 56px) + var(--desktop-composer-bottom, 16px) + 24px)",
              }}
            >
              <div
                className={cn(
                  "mx-auto w-full px-3 py-2 xl:px-5",
                  contentWidthClass,
                )}
                style={{
                  paddingBottom:
                    "calc(var(--desktop-composer-height, 56px) + var(--desktop-composer-bottom, 16px) + 24px + env(safe-area-inset-bottom, 0px))",
                }}
              >
                <DesktopStudioContent
                  historyState={historyState}
                  studioView={studioView}
                  messages={messages}
                  generations={generations}
                  scrollRef={scrollRef}
                  onPick={(text, mode) => {
                    setText(text);
                    setMode(mode);
                  }}
                  onEditImage={promoteImageToReference}
                  onRetryGen={handleRetryGen}
                  onRetryText={retryAssistant}
                  onRegenerate={regenerateAssistant}
                  onRetryHistory={handleRetryHistory}
                />
              </div>
            </main>
          </section>
        </div>

        <DesktopComposerSlot
          blocked={historyInteractionBlocked}
          onSubmit={handleSubmit}
          onMetricsChange={handleComposerMetricsChange}
        />
      </div>

      <DesktopPrivateSidebarDrawer
        open={drawerOpen && isWideSidebar !== true}
        onClose={closeSidebarDrawer}
        backgroundRef={workspaceRef}
        returnFocusRef={sidebarTriggerRef}
        title="会话侧栏"
      >
        <Sidebar
          embedded
          showBrand
          onNavigate={closeSidebarDrawer}
        />
      </DesktopPrivateSidebarDrawer>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────
// 私有子组件
// ──────────────────────────────────────────────────────────────────

function DesktopStudioContent({
  historyState,
  studioView,
  messages,
  generations,
  scrollRef,
  onPick,
  onEditImage,
  onRetryGen,
  onRetryText,
  onRegenerate,
  onRetryHistory,
}: {
  historyState: DesktopHistoryState;
  studioView: "chat" | "images";
  messages: Message[];
  generations: Record<string, Generation>;
  scrollRef: RefObject<HTMLDivElement | null>;
  onPick: (text: string, mode: "chat" | "image") => void;
  onEditImage: (imageId: string) => void;
  onRetryGen: (generationId: string) => void;
  onRetryText: (assistantId: string) => void | Promise<void>;
  onRegenerate: (
    assistantId: string,
    intent: Exclude<Intent, "auto">,
  ) => void | Promise<void>;
  onRetryHistory: () => void;
}) {
  let content: ReactNode;
  if (historyState.kind === "failed") {
    content = (
      <motion.div
        key={`history-error:${historyState.conversationId}`}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: DURATION.instant, ease: EASE.shutter }}
        className="mx-auto flex min-h-[320px] max-w-lg items-center justify-center"
      >
        <ErrorState
          title="会话加载失败"
          description="历史消息未能载入。为避免把新消息误发到不完整的会话，先重试。"
          detail={historyState.error}
          onRetry={onRetryHistory}
          retryLabel="重新加载"
          className="w-full"
        />
      </motion.div>
    );
  } else if (historyState.kind === "loading") {
    content = (
      <motion.div
        key={`history-loading:${historyState.conversationId}`}
        role="status"
        aria-live="polite"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: DURATION.instant, ease: EASE.shutter }}
        className="flex min-h-[320px] items-center justify-center gap-2 text-body-sm text-[var(--fg-2)]"
      >
        <Spinner size={20} />
        历史消息载入中…
      </motion.div>
    );
  } else if (studioView === "images") {
    content = (
      <motion.div
        key="conversation-images"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: DURATION.instant, ease: EASE.shutter }}
      >
        <ConversationImageGallery
          messages={messages}
          generations={generations}
        />
      </motion.div>
    );
  } else if (messages.length === 0) {
    content = (
      <motion.div
        key="onboarding"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: DURATION.instant, ease: EASE.shutter }}
      >
        <Onboarding onPick={onPick} />
      </motion.div>
    );
  } else {
    content = (
      <motion.div
        key="conversation"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: DURATION.instant, ease: EASE.shutter }}
      >
        <DesktopConversationCanvas
          messages={messages}
          generations={generations}
          scrollRef={scrollRef}
          onEditImage={onEditImage}
          onRetryGen={onRetryGen}
          onRetryText={(assistantId) => void onRetryText(assistantId)}
          onRegenerate={(assistantId, newIntent) => {
            if (!newIntent) return;
            return onRegenerate(assistantId, newIntent);
          }}
        />
      </motion.div>
    );
  }

  return (
    <AnimatePresence mode="sync" initial={false}>
      {content}
    </AnimatePresence>
  );
}

function DesktopComposerSlot({
  blocked,
  onSubmit,
  onMetricsChange,
}: {
  blocked: boolean;
  onSubmit: () => void | Promise<void>;
  onMetricsChange: (metrics: { height: number; bottom: number }) => void;
}) {
  if (blocked) return null;
  return (
    <DesktopComposerPill
      onSubmit={onSubmit}
      onMetricsChange={onMetricsChange}
    />
  );
}
