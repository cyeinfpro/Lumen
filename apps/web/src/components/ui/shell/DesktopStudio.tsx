"use client";

// 桌面端创作外壳（V1.0 重设计版）——对齐移动 Darkroom 逻辑：
//   · 顶部主导航三 Tab (创作 / 图库 / 我的) 替代原 Header
//   · Sidebar 从固定侧栏改为按需抽屉，由 ⌘K 或顶部 ≡ 触发
//   · 底部 Composer 改为居中 Pill（max-w 720，由 Agent 1 提供）
//   · 会话画布改用 Scene 无气泡（由 Agent 2 提供）

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import { Plus, Zap } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { DesktopTopNav } from "@/components/ui/shell/DesktopTopNav";
import { Sidebar } from "@/components/ui/Sidebar";
import { SystemPromptManager } from "@/components/ui/SystemPromptManager";
import { Onboarding } from "@/components/Onboarding";
import { DesktopComposerPill } from "@/components/ui/composer/desktop";
import {
  ConversationImageGallery,
  ContextWindowMeter,
  DesktopConversationCanvas,
} from "@/components/ui/chat/desktop";
import { useUiStore } from "@/store/useUiStore";
import { useChatStore } from "@/store/useChatStore";
import {
  useCreateConversationMutation,
  useConversationContextQuery,
  useListConversationsQuery,
} from "@/lib/queries";
import { getMe, type AuthUser } from "@/lib/apiClient";
import { SPRING } from "@/lib/motion";

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
  const setTaskTrayMinimized = useUiStore((s) => s.setTaskTrayMinimized);

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

  const meQuery = useQuery<AuthUser & { role?: "admin" | "member" }>({
    queryKey: ["me"],
    queryFn: () =>
      getMe() as Promise<AuthUser & { role?: "admin" | "member" }>,
    retry: false,
    staleTime: 60_000,
  });
  const isAdmin = meQuery.data?.role === "admin";

  // 默认收起抽屉（移动端跳到 MobileStudio，不会走此分支；桌面首次进入也收起）。
  useEffect(() => {
    setSidebarOpen(false);
  }, [setSidebarOpen]);

  const convsQuery = useListConversationsQuery({ limit: 30 });
  const {
    data: contextStats,
    refetch: refetchContextStats,
  } = useConversationContextQuery(currentConvId, { refetchInterval: 30_000 });

  useEffect(() => {
    if (currentConvId) return;
    const items = convsQuery.data?.items ?? [];
    const first = items.find((c) => !c.archived);
    if (!first) return;
    setCurrentConv(first.id);
    void loadHistoricalMessages(first.id).catch(() => {});
  }, [currentConvId, convsQuery.data, setCurrentConv, loadHistoricalMessages]);

  useEffect(() => {
    if (!currentConvId) return;
    void refetchContextStats();
  }, [currentConvId, messages.length, refetchContextStats]);

  const toggleSidebarRef = useRef(toggleSidebar);
  useEffect(() => {
    toggleSidebarRef.current = toggleSidebar;
  }, [toggleSidebar]);

  // ⌘K：切换侧栏抽屉；同时监听 Composer 派发的自定义事件。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
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
      const gen = generations[generationId];
      if (gen?.status === "succeeded" && gen.image) {
        void rerollImage(gen.image.id);
        return;
      }
      void retryGeneration(generationId);
    },
    [generations, rerollImage, retryGeneration],
  );

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const imageViewScrollKeyRef = useRef<string | null>(null);
  const isEmpty = messages.length === 0;

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

  // 生成环数据
  const running = useMemo(() => {
    const list = Object.values(generations);
    const r = list.filter(
      (g) => g.status === "running" || g.status === "queued",
    );
    const done = list.filter((g) => g.status === "succeeded").length;
    return {
      total: r.length,
      pct: r.length ? Math.round((done / (done + r.length)) * 100) : 0,
      any: r.length > 0,
    };
  }, [generations]);

  // 新建会话（桌面 TopNav slot 右侧按钮）
  const createMut = useCreateConversationMutation({
    onSuccess: (conv) => {
      setStudioView("chat");
      setCurrentConv(conv.id);
    },
  });

  const topNavRight = (
    <>
      <button
        type="button"
        onClick={() => setFast(!fast)}
        aria-label={fast ? "关闭 Fast 模式" : "开启 Fast 模式"}
        title={fast ? "Fast 模式 · 已开启" : "Fast 模式 · 点击开启"}
        className="inline-flex items-center justify-center w-7 h-7 rounded-full hover:bg-white/8 cursor-pointer transition-colors"
      >
        <FastLamp on={fast} />
      </button>
      <AnimatePresence initial={false}>
        {running.any && (
          <motion.button
            key="gen-ring"
            type="button"
            initial={{ opacity: 0, scale: 0.8 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.8 }}
            onClick={() => setTaskTrayMinimized(false)}
            aria-label={`生成中 ${running.total} 张，点击查看任务面板`}
            className="inline-flex items-center justify-center w-7 h-7 rounded-full hover:bg-white/8 cursor-pointer"
          >
            <GenerationRing pct={running.pct} total={running.total} />
          </motion.button>
        )}
      </AnimatePresence>
      <ContextWindowMeter stats={contextStats} />
      <SystemPromptManager compact />
      {isAdmin && (
        <Link
          href="/admin"
          className="cursor-pointer hover:text-[var(--fg-0)] transition-colors hidden lg:inline text-xs"
        >
          管理
        </Link>
      )}
      <button
        type="button"
        onClick={() => !createMut.isPending && createMut.mutate({})}
        aria-label="新建对话"
        title="新建对话"
        disabled={createMut.isPending}
        className={[
          "inline-flex items-center justify-center w-7 h-7 rounded-full",
          "text-[var(--fg-2)] hover:text-[var(--fg-0)] hover:bg-white/8",
          "cursor-pointer disabled:opacity-50 transition-colors",
        ].join(" ")}
      >
        <Plus className="w-4 h-4" />
      </button>
      <Link
        href="/me"
        className="shrink-0 inline-flex items-center justify-center w-7 h-7 rounded-full bg-white/10 hover:bg-white/15 transition-colors cursor-pointer"
        aria-label="我的账号"
        title="我的账号"
      >
        <span className="text-xs font-medium text-[var(--fg-1)]">
          {(meQuery.data?.name ?? meQuery.data?.email)?.charAt(0)?.toUpperCase() || "U"}
        </span>
      </Link>
    </>
  );

  return (
    <div className="relative flex flex-col h-[100dvh] bg-[var(--bg-0)]">
      <DesktopTopNav
        active="studio"
        right={topNavRight}
        onToggleSidebar={toggleSidebar}
      />

      <main
        ref={scrollRef}
        className="relative flex-1 overflow-y-auto overflow-x-hidden lumen-studio-bg"
      >
        <div
          className="mx-auto w-full max-w-[1680px] px-3 py-2 xl:px-4 2xl:px-6"
          style={{
            paddingBottom: "calc(84px + env(safe-area-inset-bottom, 0px))",
          }}
        >
          <AnimatePresence mode="wait" initial={false}>
            {studioView === "images" ? (
              <motion.div
                key="conversation-images"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -8 }}
                transition={{ duration: 0.24 }}
              >
                <ConversationImageGallery
                  messages={messages}
                  generations={generations}
                />
              </motion.div>
            ) : isEmpty ? (
              <motion.div
                key="onboarding"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -8 }}
                transition={{ duration: 0.24 }}
              >
                <Onboarding
                  onPick={(text, m) => {
                    setText(text);
                    setMode(m);
                  }}
                />
              </motion.div>
            ) : (
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
            )}
          </AnimatePresence>
        </div>

        {!isEmpty && <div className="lumen-bottom-fade" aria-hidden />}
      </main>

      <DesktopComposerPill onSubmit={() => sendMessage()} />

      <DesktopSidebarDrawer
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      >
        <Sidebar />
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
            className="fixed inset-0 bg-black/50 z-40"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18 }}
            onClick={onClose}
            aria-hidden
          />
          <motion.aside
            key="drawer-panel"
            className="fixed left-0 top-0 bottom-0 w-72 bg-[var(--bg-1)] border-r border-[var(--border-subtle)] z-50 overflow-hidden"
            initial={{ x: -288 }}
            animate={{ x: 0 }}
            exit={{ x: -288 }}
            transition={SPRING.sheet}
            role="dialog"
            aria-label="会话侧栏"
          >
            {children}
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}

function FastLamp({ on }: { on: boolean }) {
  return (
    <span
      className={[
        "inline-flex items-center justify-center",
        on ? "text-[var(--amber-400)]" : "text-[var(--fg-3)]",
      ].join(" ")}
      style={
        on ? { filter: "drop-shadow(0 0 6px var(--amber-glow-strong))" } : undefined
      }
    >
      <Zap className="w-4 h-4" fill={on ? "currentColor" : "none"} strokeWidth={1.8} />
    </span>
  );
}

function GenerationRing({ pct, total }: { pct: number; total: number }) {
  const R = 10;
  const C = 2 * Math.PI * R;
  const off = C * (1 - Math.max(0, Math.min(pct, 100)) / 100);
  return (
    <span
      className="inline-flex relative w-6 h-6 items-center justify-center text-[9px] font-mono text-[var(--amber-300)]"
      aria-label={`生成中 ${total} 张`}
    >
      <svg width={24} height={24} viewBox="0 0 24 24" className="absolute inset-0">
        <circle cx={12} cy={12} r={R} stroke="var(--border-subtle)" strokeWidth={2} fill="none" />
        <circle
          cx={12}
          cy={12}
          r={R}
          stroke="var(--amber-400)"
          strokeWidth={2}
          fill="none"
          strokeLinecap="round"
          strokeDasharray={C}
          strokeDashoffset={off}
          transform="rotate(-90 12 12)"
          style={{ transition: "stroke-dashoffset 300ms ease" }}
        />
      </svg>
      <span className="relative">{total}</span>
    </span>
  );
}
