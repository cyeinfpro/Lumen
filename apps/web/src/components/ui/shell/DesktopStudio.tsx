"use client";

// 桌面创作外壳：全局 App Bar + 会话 Context Bar + 三态侧栏。

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Images, MessageSquareText, PanelLeftOpen, Plus, X } from "lucide-react";

import { DesktopTopNav } from "@/components/ui/shell/DesktopTopNav";
import { Sidebar } from "@/components/ui/Sidebar";
import { Onboarding } from "@/components/Onboarding";
import { DesktopComposerPill } from "@/components/ui/composer/desktop";
import { IconButton } from "@/components/ui/primitives";
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
import { DURATION, EASE, SPRING } from "@/lib/motion";
import { cn } from "@/lib/utils";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { StudioContextBar } from "./StudioContextBar";
import { useConversationRouteSync } from "./useConversationRouteSync";

declare global {
  interface WindowEventMap {
    "lumen:sidebar-toggle": CustomEvent<void>;
  }
}

export function DesktopStudio() {
  const sidebarOpen = useUiStore((s) => s.sidebarOpen);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);
  const setSidebarOpen = useUiStore((s) => s.setSidebarOpen);
  const studioView = useUiStore((s) => s.studioView);
  const setStudioView = useUiStore((s) => s.setStudioView);

  const messages = useChatStore((s) => s.messages);
  const generations = useChatStore((s) => s.generations);
  const currentConvId = useChatStore((s) => s.currentConvId);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const loadHistoricalMessages = useChatStore((s) => s.loadHistoricalMessages);
  const sendMessage = useChatStore((s) => s.sendMessage);
  const retryAssistant = useChatStore((s) => s.retryAssistant);
  const retryGeneration = useChatStore((s) => s.retryGeneration);
  const regenerateAssistant = useChatStore((s) => s.regenerateAssistant);
  const rerollImage = useChatStore((s) => s.rerollImage);
  const promoteImageToReference = useChatStore((s) => s.promoteImageToReference);
  const setText = useChatStore((s) => s.setText);
  const setMode = useChatStore((s) => s.setMode);
  const fast = useChatStore((s) => s.composer.fast);
  const setFast = useChatStore((s) => s.setFast);
  const isWideSidebar = useMediaQuery("(min-width: 1440px)");

  // 宽屏默认固定侧栏，中屏/窄屏默认窄栏或抽屉。
  useEffect(() => {
    const wide = window.matchMedia("(min-width: 1440px)");
    const sync = () => setSidebarOpen(wide.matches);
    sync();
    wide.addEventListener("change", sync);
    return () => wide.removeEventListener("change", sync);
  }, [setSidebarOpen]);

  const convsQuery = useListConversationsInfiniteQuery({ limit: 30 });
  const {
    data: contextStats,
    refetch: refetchContextStats,
  } = useConversationContextQuery(currentConvId, { refetchInterval: 30_000 });
  const urlConversationId = useConversationRouteSync({
    currentConvId,
    loadHistoricalMessages,
    setCurrentConv,
  });

  useEffect(() => {
    if (currentConvId) return;
    if (urlConversationId) return;
    const items = convsQuery.data?.pages.flatMap((p) => p.items) ?? [];
    const first = items.find((c) => !c.archived);
    if (!first) return;
    setCurrentConv(first.id);
    void loadHistoricalMessages(first.id).catch(() => {});
  }, [
    currentConvId,
    convsQuery.data,
    loadHistoricalMessages,
    setCurrentConv,
    urlConversationId,
  ]);

  useEffect(() => {
    if (currentConvId || urlConversationId) return;
    if (!convsQuery.hasNextPage || convsQuery.isFetchingNextPage) return;
    const items = convsQuery.data?.pages.flatMap((p) => p.items) ?? [];
    if (items.some((c) => !c.archived)) return;
    void convsQuery.fetchNextPage();
  }, [
    currentConvId,
    convsQuery,
    convsQuery.data,
    convsQuery.hasNextPage,
    convsQuery.isFetchingNextPage,
    urlConversationId,
  ]);

  useEffect(() => {
    if (!currentConvId) return;
    void refetchContextStats();
  }, [currentConvId, messages.length, refetchContextStats]);

  const toggleSidebarRef = useRef(toggleSidebar);
  useEffect(() => {
    toggleSidebarRef.current = toggleSidebar;
  }, [toggleSidebar]);

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

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const imageViewScrollKeyRef = useRef<string | null>(null);
  const isEmpty = messages.length === 0;
  const currentTitle = useMemo(() => {
    const items = convsQuery.data?.pages.flatMap((page) => page.items) ?? [];
    const current = items.find((item) => item.id === currentConvId);
    if (current?.title) return current.title;
    const firstUser = messages.find((message) => message.role === "user");
    return firstUser?.text?.slice(0, 48) || "新对话";
  }, [convsQuery.data, currentConvId, messages]);

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

  return (
    <div
      className="studio-shell relative flex h-[100dvh] min-h-0 flex-col bg-[var(--bg-0)]"
      data-sidebar-open={sidebarOpen ? "true" : "false"}
    >
      <DesktopTopNav
        active="studio"
        onToggleSidebar={toggleSidebar}
      />

      <div className="flex min-h-0 flex-1">
        <DesktopSidebarDock
          expanded={sidebarOpen}
          onToggle={toggleSidebar}
          onCreate={() => !createMut.isPending && createMut.mutate({})}
          creating={createMut.isPending}
          view={studioView}
          onViewChange={setStudioView}
        />

        <section className="flex min-w-0 flex-1 flex-col">
          <StudioContextBar
            title={currentTitle}
            view={studioView}
            onViewChange={setStudioView}
            fast={fast}
            onFastChange={setFast}
            contextStats={contextStats}
          />

          <main
            ref={scrollRef}
            className="relative min-h-0 flex-1 overflow-y-auto overflow-x-hidden lumen-studio-bg"
          >
            <div
              className="mx-auto w-full max-w-[var(--content-workbench)] px-3 py-3 xl:px-5"
              style={{
                paddingBottom:
                  "calc(96px + env(safe-area-inset-bottom, 0px))",
              }}
            >
              <AnimatePresence mode="sync" initial={false}>
                {studioView === "images" ? (
                  <motion.div
                    key="conversation-images"
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: DURATION.page, ease: EASE.develop }}
                  >
                    <ConversationImageGallery
                      messages={messages}
                      generations={generations}
                    />
                  </motion.div>
                ) : isEmpty ? (
                  <motion.div
                    key="onboarding"
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: DURATION.page, ease: EASE.develop }}
                  >
                    <Onboarding
                      onPick={(text, m) => {
                        setText(text);
                        setMode(m);
                      }}
                    />
                  </motion.div>
                ) : (
                  <motion.div
                    key="conversation"
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: DURATION.page, ease: EASE.develop }}
                  >
                    <DesktopConversationCanvas
                      messages={messages}
                      generations={generations}
                      scrollRef={scrollRef}
                      onEditImage={promoteImageToReference}
                      onRetryGen={handleRetryGen}
                      onRetryText={(assistantId) => void retryAssistant(assistantId)}
                      onRegenerate={(assistantId, newIntent) => {
                        if (!newIntent) return;
                        return regenerateAssistant(assistantId, newIntent);
                      }}
                    />
                  </motion.div>
                )}
              </AnimatePresence>
            </div>

            {!isEmpty && <div className="lumen-bottom-fade" aria-hidden />}
          </main>
        </section>
      </div>

      <DesktopSidebarDrawer
        open={sidebarOpen && isWideSidebar === false}
        onClose={() => setSidebarOpen(false)}
      >
        <Sidebar embedded showBrand />
      </DesktopSidebarDrawer>

      <DesktopComposerPill onSubmit={() => sendMessage()} />
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────
// 私有子组件
// ──────────────────────────────────────────────────────────────────

function DesktopSidebarDrawer({
  open,
  onClose,
  children,
}: {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  // ESC 关闭
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            key="drawer-backdrop"
            className="fixed inset-0 z-[calc(var(--z-dialog)-1)] bg-black/50 min-[1440px]:hidden"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
            onClick={onClose}
            aria-hidden
          />
          <motion.aside
            key="drawer-panel"
            className="fixed bottom-0 left-0 top-0 z-[var(--z-dialog)] w-72 overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-1)] min-[1440px]:hidden"
            initial={{ x: -288 }}
            animate={{ x: 0 }}
            exit={{ x: -288 }}
            transition={SPRING.sheet}
            role="dialog"
            aria-modal="true"
            aria-label="会话侧栏"
          >
            <IconButton
              size="sm"
              variant="ghost"
              onClick={onClose}
              aria-label="关闭会话侧栏"
              className="absolute right-3 top-3 z-10 rounded-[var(--radius-control)]"
            >
              <X className="h-4 w-4" aria-hidden />
            </IconButton>
            {children}
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}

function DesktopSidebarDock({
  expanded,
  onToggle,
  onCreate,
  creating,
  view,
  onViewChange,
}: {
  expanded: boolean;
  onToggle: () => void;
  onCreate: () => void;
  creating: boolean;
  view: "chat" | "images";
  onViewChange: (view: "chat" | "images") => void;
}) {
  return (
    <aside
      aria-label="会话导航"
      className={cn(
        "hidden min-[1120px]:flex shrink-0 overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-1)]",
        "transition-[width] duration-[var(--dur-panel)]",
        expanded ? "w-16 min-[1440px]:w-[264px]" : "w-16",
      )}
    >
      {expanded ? (
        <div className="hidden h-full min-w-0 flex-1 min-[1440px]:flex">
          <Sidebar embedded />
        </div>
      ) : null}
      <div
        className={cn(
          "flex h-full w-16 shrink-0 flex-col items-center gap-2 px-2 py-3",
          expanded && "min-[1440px]:hidden",
        )}
      >
        <IconButton
          size="md"
          variant="ghost"
          onClick={onToggle}
          aria-label="展开会话侧栏"
          tooltip="展开会话侧栏"
        >
          <PanelLeftOpen className="h-[18px] w-[18px]" aria-hidden />
        </IconButton>
        <IconButton
          size="md"
          variant="primary"
          onClick={onCreate}
          disabled={creating}
          aria-label="新建对话"
          tooltip="新建对话"
        >
          <Plus className="h-4 w-4" aria-hidden />
        </IconButton>
        <span className="my-1 h-px w-8 bg-[var(--border-subtle)]" aria-hidden />
        <IconButton
          size="md"
          variant={view === "chat" ? "secondary" : "ghost"}
          onClick={() => onViewChange("chat")}
          aria-label="对话视图"
          tooltip="对话视图"
        >
          <MessageSquareText className="h-4 w-4" aria-hidden />
        </IconButton>
        <IconButton
          size="md"
          variant={view === "images" ? "secondary" : "ghost"}
          onClick={() => onViewChange("images")}
          aria-label="图片视图"
          tooltip="图片视图"
        >
          <Images className="h-4 w-4" aria-hidden />
        </IconButton>
      </div>
    </aside>
  );
}
