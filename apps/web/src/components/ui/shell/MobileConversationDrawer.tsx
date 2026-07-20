"use client";

// 移动端会话抽屉：左滑全屏抽屉，复刻桌面 Sidebar 能力，使用移动原生交互。
// - 顶部：标题 + 关闭
// - 主 CTA：新建会话（amber）
// - 搜索（始终可见）
// - SegmentedControl：对话 / 归档
// - 时间分桶列表（今天 / 昨天 / 本周 / 更早）
// - 每行：SwipeRow 左滑 + 显式 ••• ActionSheet
// - 无限滚动 + 空态/错误态/骨架

import {
  AnimatePresence,
  motion,
  useReducedMotion,
} from "framer-motion";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { pushMobileToast } from "@/components/ui/primitives/mobile";
import {
  useCreateConversationMutation,
  useDeleteConversationMutation,
  useListConversationsInfiniteQuery,
  usePatchConversationMutation,
} from "@/lib/queries";
import type { ConversationSummary } from "@/lib/apiClient";
import { useChatStore } from "@/store/useChatStore";
import { useHaptic } from "@/hooks/useHaptic";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { logWarn } from "@/lib/logger";
import { cn } from "@/lib/utils";
import { DURATION, resolveDrawerMotion } from "@/lib/motion";

import {
  deriveConversationDrawerModel,
  isInitialConversationLoad,
  type TabKind,
} from "./mobileConversationDrawerModel";
import { MobileConversationDrawerView } from "./MobileConversationDrawerView";

function trapDrawerFocus(
  event: KeyboardEvent,
  panel: HTMLElement | null,
  onClose: () => void,
) {
  if (!panel) return;
  const dialogs = Array.from(
    document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]'),
  ).filter((dialog) => dialog.isConnected);
  if (dialogs.at(-1) !== panel) return;

  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    onClose();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = Array.from(
    panel.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => element.getClientRects().length > 0);
  if (focusable.length === 0) {
    event.preventDefault();
    panel.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !panel.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !panel.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

export interface MobileConversationDrawerProps {
  open: boolean;
  onClose: () => void;
}

export function MobileConversationDrawer({
  open,
  onClose,
}: MobileConversationDrawerProps) {
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<TabKind>("active");
  const { haptic } = useHaptic();
  const reduceMotion = useReducedMotion();
  const drawerMotion = resolveDrawerMotion(reduceMotion, DURATION.normal);
  const panelRef = useRef<HTMLElement | null>(null);
  const listScrollRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  const currentConvId = useChatStore((s) => s.currentConvId);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const loadHistoricalMessages = useChatStore(
    (s) => s.loadHistoricalMessages,
  );

  const list = useListConversationsInfiniteQuery({ limit: 30 });
  const createMut = useCreateConversationMutation({
    onSuccess: (conv) => {
      setCurrentConv(conv.id);
      onClose();
    },
    onError: (err) => {
      pushMobileToast(
        err?.message ? `新建失败：${err.message}` : "新建失败，稍后重试",
        "danger",
      );
    },
  });
  const patchMut = usePatchConversationMutation();
  const deleteMut = useDeleteConversationMutation();

  const {
    allConvs,
    activeTotal,
    archivedTotal,
    filtered,
    grouped,
    hasResults,
  } = useMemo(
    () => deriveConversationDrawerModel(list.data?.pages, query, tab),
    [list.data?.pages, query, tab],
  );

  useEffect(() => {
    if (!open) return;
    returnFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const focusFrame = window.requestAnimationFrame(() =>
      closeButtonRef.current?.focus(),
    );
    const onKey = (event: KeyboardEvent) =>
      trapDrawerFocus(event, panelRef.current, onClose);
    window.addEventListener("keydown", onKey);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", onKey);
      window.requestAnimationFrame(() => returnFocusRef.current?.focus());
    };
  }, [open, onClose]);

  // ── body scroll lock ──
  useBodyScrollLock(open);

  // ── infinite scroll sentinel ──
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const {
    hasNextPage,
    isFetchingNextPage,
    isFetchNextPageError,
    fetchNextPage,
  } = list;
  useEffect(() => {
    if (!open) return;
    const el = sentinelRef.current;
    const root = listScrollRef.current;
    if (!el) return;
    if (!hasNextPage) return;
    if (isFetchNextPageError) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && !isFetchingNextPage) {
            void fetchNextPage();
          }
        }
      },
      { root, rootMargin: "200px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [
    open,
    fetchNextPage,
    hasNextPage,
    isFetchNextPageError,
    isFetchingNextPage,
  ]);

  const handleSelect = useCallback(
    async (conv: ConversationSummary) => {
      if (conv.id !== currentConvId) {
        const previousConvId = currentConvId;
        setCurrentConv(conv.id);
        try {
          await loadHistoricalMessages(conv.id);
        } catch (err) {
          if (useChatStore.getState().currentConvId === conv.id) {
            setCurrentConv(previousConvId);
          }
          logWarn("mobile_drawer.load_historical_messages_failed", {
            scope: "mobile-drawer",
            extra: { convId: conv.id, err: String(err) },
          });
          pushMobileToast("会话加载失败，请重试", "danger");
          return;
        }
      }
      haptic("light");
      onClose();
    },
    [currentConvId, setCurrentConv, loadHistoricalMessages, haptic, onClose],
  );

  const handleCreate = useCallback(() => {
    if (createMut.isPending) return;
    haptic("medium");
    createMut.mutate({});
  }, [createMut, haptic]);

  const handleRename = useCallback(
    (conv: ConversationSummary, title: string) => {
      patchMut.mutate({ id: conv.id, title });
    },
    [patchMut],
  );

  const handleArchive = useCallback(
    (conv: ConversationSummary) => {
      patchMut.mutate(
        { id: conv.id, archived: !conv.archived },
        {
          onSuccess: () => {
            pushMobileToast(
              conv.archived ? "已恢复到对话" : "已归档",
              "success",
            );
          },
        },
      );
    },
    [patchMut],
  );

  const handleDelete = useCallback(
    (conv: ConversationSummary) => {
      deleteMut.mutate(conv.id, {
        onSuccess: () => {
          if (currentConvId === conv.id) setCurrentConv(null);
          pushMobileToast("已删除会话", "success");
        },
      });
    },
    [deleteMut, currentConvId, setCurrentConv],
  );

  const isInitialLoading = isInitialConversationLoad(
    list.isLoading,
    allConvs.length,
  );

  if (typeof window === "undefined") return null;

  return createPortal(
    <AnimatePresence>
      {open && (
        <>
          {/* scrim */}
          <motion.button
            type="button"
            key="conv-drawer-scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={drawerMotion.scrimTransition}
            onClick={onClose}
            aria-label="关闭会话列表"
            className="fixed inset-0 z-[60] bg-black/55 backdrop-blur-[3px]"
          />

          {/* drawer */}
          <motion.aside
            ref={panelRef}
            key="conv-drawer-panel"
            tabIndex={-1}
            role="dialog"
            aria-modal="true"
            aria-label="会话列表"
            initial={drawerMotion.panelInitial}
            animate={drawerMotion.panelAnimate}
            exit={drawerMotion.panelExit}
            transition={drawerMotion.panelTransition}
            className={cn(
              "fixed bottom-0 left-0 top-0 z-[61] flex min-h-0 max-h-[100dvh] flex-col",
              "w-[min(360px,92vw)] bg-[var(--bg-1)]",
              "border-r border-[var(--border-subtle)] shadow-[var(--shadow-3)]",
              "overflow-hidden",
              "[@media(orientation:landscape)_and_(max-height:520px)]:w-[min(360px,55vw)]",
            )}
            style={{
              paddingTop: "env(safe-area-inset-top, 0px)",
              paddingBottom: "env(safe-area-inset-bottom, 0px)",
              paddingLeft: "env(safe-area-inset-left, 0px)",
            }}
          >
            <MobileConversationDrawerView
              closeButtonRef={closeButtonRef}
              listScrollRef={listScrollRef}
              activeTotal={activeTotal}
              archivedTotal={archivedTotal}
              createPending={createMut.isPending}
              onClose={onClose}
              onQueryChange={setQuery}
              onTabChange={setTab}
              sentinelRef={sentinelRef}
              isInitialLoading={isInitialLoading}
              isError={list.isError}
              isFetchingNextPage={isFetchingNextPage}
              isFetchNextPageError={isFetchNextPageError}
              hasNextPage={Boolean(hasNextPage)}
              hasResults={hasResults}
              query={query}
              tab={tab}
              filtered={filtered}
              grouped={grouped}
              currentConvId={currentConvId}
              onRetry={() => {
                void list.refetch();
              }}
              onLoadMore={() => {
                void fetchNextPage();
              }}
              onClearQuery={() => setQuery("")}
              onCreate={handleCreate}
              onSelect={(conv) => {
                void handleSelect(conv);
              }}
              onRename={handleRename}
              onArchive={handleArchive}
              onDelete={handleDelete}
            />
          </motion.aside>
        </>
      )}
    </AnimatePresence>,
    document.body,
  );
}
