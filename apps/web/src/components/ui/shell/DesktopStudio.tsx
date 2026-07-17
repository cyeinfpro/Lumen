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
import { PanelLeftOpen, Plus, X } from "lucide-react";

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
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [composerMetrics, setComposerMetrics] = useState({
    height: 56,
    bottom: 16,
  });
  const sidebarTriggerRef = useRef<HTMLButtonElement | null>(null);
  const workspaceRef = useRef<HTMLDivElement | null>(null);

  const convsQuery = useListConversationsInfiniteQuery({ limit: 30 });
  const {
    data: contextStats,
    refetch: refetchContextStats,
  } = useConversationContextQuery(currentConvId, { refetchInterval: 30_000 });
  useConversationRouteSync({
    currentConvId,
    loadHistoricalMessages,
    setCurrentConv,
    rootStartsNew: true,
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
          <DesktopSidebarDock
            expanded={isWideSidebar === true && sidebarOpen}
            onToggle={handleSidebarToggle}
            onCreate={() => !createMut.isPending && createMut.mutate({})}
            creating={createMut.isPending}
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
                  studioView === "images"
                    ? "max-w-[var(--content-workbench)]"
                    : isEmpty
                      ? "max-w-[var(--content-composer)]"
                      : "max-w-[var(--content-media)]",
                )}
                style={{
                  paddingBottom:
                    "calc(var(--desktop-composer-height, 56px) + var(--desktop-composer-bottom, 16px) + 24px + env(safe-area-inset-bottom, 0px))",
                }}
              >
                <AnimatePresence mode="sync" initial={false}>
                  {studioView === "images" ? (
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
                  ) : isEmpty ? (
                    <motion.div
                      key="onboarding"
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                      transition={{ duration: DURATION.instant, ease: EASE.shutter }}
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
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0 }}
                      transition={{ duration: DURATION.instant, ease: EASE.shutter }}
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
            </main>
          </section>
        </div>

        <DesktopComposerPill
          onSubmit={() => sendMessage()}
          onMetricsChange={handleComposerMetricsChange}
        />
      </div>

      <DesktopSidebarDrawer
        open={drawerOpen && isWideSidebar !== true}
        onClose={closeSidebarDrawer}
        backgroundRef={workspaceRef}
        returnFocusRef={sidebarTriggerRef}
      >
        <Sidebar
          embedded
          showBrand
          onNavigate={closeSidebarDrawer}
        />
      </DesktopSidebarDrawer>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────
// 私有子组件
// ──────────────────────────────────────────────────────────────────

function DesktopSidebarDrawer({
  open,
  onClose,
  backgroundRef,
  returnFocusRef,
  children,
}: {
  open: boolean;
  onClose: () => void;
  backgroundRef: RefObject<HTMLElement | null>;
  returnFocusRef: RefObject<HTMLButtonElement | null>;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    const background = backgroundRef.current;
    const returnFocusTarget = returnFocusRef.current;
    const previousBodyOverflow = document.body.style.overflow;
    const previousBackgroundInert = background?.inert ?? false;
    const previousBackgroundAriaHidden =
      background?.getAttribute("aria-hidden") ?? null;

    if (background) {
      background.inert = true;
      background.setAttribute("aria-hidden", "true");
    }
    document.body.style.overflow = "hidden";

    const focusFrame = window.requestAnimationFrame(() => panel?.focus());
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panel) return;

      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter(
        (element) =>
          !element.hasAttribute("hidden") && element.getClientRects().length > 0,
      );
      if (focusable.length === 0) {
        e.preventDefault();
        panel.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (
        e.shiftKey &&
        (document.activeElement === first || document.activeElement === panel)
      ) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousBodyOverflow;
      if (background) {
        background.inert = previousBackgroundInert;
        if (previousBackgroundAriaHidden === null) {
          background.removeAttribute("aria-hidden");
        } else {
          background.setAttribute(
            "aria-hidden",
            previousBackgroundAriaHidden,
          );
        }
      }
      window.requestAnimationFrame(() => returnFocusTarget?.focus());
    };
  }, [backgroundRef, open, onClose, returnFocusRef]);

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            key="drawer-backdrop"
            className="fixed inset-x-0 bottom-0 z-[calc(var(--z-dialog)-1)] bg-[var(--surface-scrim)] min-[1440px]:hidden"
            style={{
              top: "calc(var(--top-banner-stack-height, 0px) + env(safe-area-inset-top, 0px))",
            }}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
            onClick={onClose}
            aria-hidden
          />
          <motion.aside
            ref={panelRef}
            key="drawer-panel"
            tabIndex={-1}
            className="fixed bottom-0 left-0 z-[var(--z-dialog)] w-72 overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-1)] pb-[env(safe-area-inset-bottom,0px)] min-[1440px]:hidden"
            style={{
              top: "calc(var(--top-banner-stack-height, 0px) + env(safe-area-inset-top, 0px))",
            }}
            initial={{ x: -288 }}
            animate={{ x: 0 }}
            exit={{ x: -288 }}
            transition={SPRING.drawer}
            role="dialog"
            aria-modal="true"
            aria-labelledby="desktop-sidebar-drawer-title"
          >
            <h2 id="desktop-sidebar-drawer-title" className="sr-only">
              会话侧栏
            </h2>
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
}: {
  expanded: boolean;
  onToggle: () => void;
  onCreate: () => void;
  creating: boolean;
}) {
  return (
    <aside
      aria-label="会话导航"
      className={cn(
        "hidden min-[1120px]:flex shrink-0 overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-1)]",
        "transition-[width] duration-[var(--dur-panel)]",
        expanded
          ? "w-[var(--sidebar-rail-w)] min-[1440px]:w-[var(--sidebar-panel-w)]"
          : "w-[var(--sidebar-rail-w)]",
      )}
    >
      {expanded ? (
        <div className="hidden h-full min-w-0 flex-1 min-[1440px]:flex">
          <Sidebar embedded />
        </div>
      ) : null}
      <div
        className={cn(
          "flex h-full w-[var(--sidebar-rail-w)] shrink-0 flex-col items-center gap-2 px-2 py-3",
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
      </div>
    </aside>
  );
}
