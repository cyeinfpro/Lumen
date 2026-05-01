"use client";

// 移动端会话抽屉：左滑全屏抽屉，复刻桌面 Sidebar 能力，使用移动原生交互。
// - 顶部：标题 + 关闭
// - 主 CTA：新建会话（amber）
// - 搜索（始终可见）
// - SegmentedControl：对话 / 归档
// - 时间分桶列表（今天 / 昨天 / 本周 / 更早）
// - 每行：SwipeRow 左滑 + 显式 ••• ActionSheet
// - 无限滚动 + 空态/错误态/骨架

import { AnimatePresence, motion } from "framer-motion";
import {
  Inbox,
  Loader2,
  Plus,
  Search,
  Sparkles,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import {
  SegmentedControl,
  pushMobileToast,
} from "@/components/ui/primitives/mobile";
import { Spinner } from "@/components/ui/primitives";
import {
  useCreateConversationMutation,
  useDeleteConversationMutation,
  useListConversationsInfiniteQuery,
  usePatchConversationMutation,
} from "@/lib/queries";
import type { ConversationSummary } from "@/lib/apiClient";
import { useChatStore } from "@/store/useChatStore";
import { useHaptic } from "@/hooks/useHaptic";
import { logWarn } from "@/lib/logger";
import { cn } from "@/lib/utils";

import { ConversationRowMobile } from "@/components/ui/me/ConversationRowMobile";

type Bucket = "today" | "yesterday" | "last7" | "older";
type TabKind = "active" | "archived";

const BUCKET_ORDER: Bucket[] = ["today", "yesterday", "last7", "older"];
const BUCKET_LABEL: Record<Bucket, string> = {
  today: "今天",
  yesterday: "昨天",
  last7: "本周",
  older: "更早",
};

const SKELETON_ROWS = [
  { id: "first", titleWidth: 62 },
  { id: "second", titleWidth: 70 },
  { id: "third", titleWidth: 56 },
  { id: "fourth", titleWidth: 78 },
  { id: "fifth", titleWidth: 50 },
] as const;

function dayKeyOf(iso: string): Bucket {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "older";
  const now = new Date();
  const d = new Date(t);
  const startOfDay = (x: Date) =>
    new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const todayStart = startOfDay(now);
  const yestStart = todayStart - 24 * 3600 * 1000;
  const last7Start = todayStart - 7 * 24 * 3600 * 1000;
  const ts = d.getTime();
  if (ts >= todayStart) return "today";
  if (ts >= yestStart) return "yesterday";
  if (ts >= last7Start) return "last7";
  return "older";
}

function titleOf(c: ConversationSummary): string {
  return c.title?.trim() || "New Canvas";
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
        err?.message ? `新建失败：${err.message}` : "新建失败，请稍后重试",
        "danger",
      );
    },
  });
  const patchMut = usePatchConversationMutation();
  const deleteMut = useDeleteConversationMutation();

  const allConvs = useMemo(() => {
    const pages = list.data?.pages ?? [];
    return pages.flatMap((p) => p.items);
  }, [list.data]);

  const activeTotal = useMemo(
    () => allConvs.filter((c) => !c.archived).length,
    [allConvs],
  );
  const archivedTotal = useMemo(
    () => allConvs.filter((c) => c.archived).length,
    [allConvs],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const base = allConvs.filter((c) =>
      tab === "archived" ? c.archived : !c.archived,
    );
    if (!q) return base;
    return base.filter((c) => titleOf(c).toLowerCase().includes(q));
  }, [allConvs, tab, query]);

  const grouped = useMemo(() => {
    const g: Record<Bucket, ConversationSummary[]> = {
      today: [],
      yesterday: [],
      last7: [],
      older: [],
    };
    for (const c of filtered) g[dayKeyOf(c.last_activity_at)].push(c);
    return g;
  }, [filtered]);

  // ── Esc 关闭 ──
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // ── body scroll lock ──
  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  // ── infinite scroll sentinel ──
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = list;
  useEffect(() => {
    if (!open) return;
    const el = sentinelRef.current;
    if (!el) return;
    if (!hasNextPage) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && !isFetchingNextPage) {
            fetchNextPage();
          }
        }
      },
      { rootMargin: "200px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [open, fetchNextPage, hasNextPage, isFetchingNextPage]);

  const handleSelect = useCallback(
    async (conv: ConversationSummary) => {
      if (conv.id !== currentConvId) {
        setCurrentConv(conv.id);
        try {
          await loadHistoricalMessages(conv.id);
        } catch (err) {
          logWarn("mobile_drawer.load_historical_messages_failed", {
            scope: "mobile-drawer",
            extra: { convId: conv.id, err: String(err) },
          });
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

  const hasResults = filtered.length > 0;
  const isInitialLoading = list.isLoading && allConvs.length === 0;

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
            transition={{ duration: 0.18 }}
            onClick={onClose}
            aria-label="关闭会话列表"
            className="fixed inset-0 z-[60] bg-black/55 backdrop-blur-[3px]"
          />

          {/* drawer */}
          <motion.aside
            key="conv-drawer-panel"
            role="dialog"
            aria-modal="true"
            aria-label="会话列表"
            initial={{ x: "-100%" }}
            animate={{ x: 0 }}
            exit={{ x: "-100%" }}
            transition={{ type: "spring", stiffness: 380, damping: 34 }}
            className={cn(
              "fixed top-0 left-0 bottom-0 z-[61] flex flex-col",
              "w-[min(360px,92vw)] bg-[var(--bg-1)]",
              "border-r border-[var(--border-subtle)] shadow-[var(--shadow-3)]",
              "overflow-hidden",
            )}
            style={{
              paddingTop: "env(safe-area-inset-top, 0px)",
              paddingBottom: "env(safe-area-inset-bottom, 0px)",
            }}
          >
            {/* Header */}
            <div className="px-4 pt-3 pb-2 flex items-center justify-between">
              <div className="flex items-center gap-2 min-w-0">
                <span className="relative w-6 h-6 rounded-full bg-gradient-to-br from-[var(--amber-400)] to-[var(--amber-600)] flex items-center justify-center shadow-[0_0_10px_var(--amber-glow)]">
                  <Sparkles
                    className="w-3 h-3 text-black/80"
                    strokeWidth={2.5}
                  />
                </span>
                <span className="text-[16px] font-semibold tracking-tight text-[var(--fg-0)]">
                  会话
                </span>
                <span className="ml-1.5 text-[12px] font-mono text-[var(--fg-2)]">
                  {activeTotal + archivedTotal || ""}
                </span>
              </div>
              <button
                type="button"
                onClick={onClose}
                aria-label="关闭"
                className={cn(
                  "inline-flex items-center justify-center w-9 h-9 rounded-full",
                  "text-[var(--fg-1)] active:bg-[var(--bg-2)] active:scale-[0.94]",
                  "transition-[background-color,transform] duration-150",
                )}
              >
                <X className="w-[18px] h-[18px]" />
              </button>
            </div>

            {/* New conversation CTA */}
            <div className="px-4 pb-3">
              <button
                type="button"
                onClick={handleCreate}
                disabled={createMut.isPending}
                className={cn(
                  "w-full inline-flex items-center justify-center gap-2 h-12 rounded-2xl",
                  "bg-gradient-to-br from-[var(--amber-400)] to-[var(--amber-600)]",
                  "text-black text-[15px] font-medium",
                  "shadow-[0_8px_22px_-8px_var(--amber-glow-strong)]",
                  "active:scale-[0.985] transition-transform",
                  "disabled:opacity-60 disabled:cursor-wait",
                )}
              >
                {createMut.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Plus className="w-[18px] h-[18px]" strokeWidth={2.4} />
                )}
                {createMut.isPending ? "正在新建…" : "新建会话"}
              </button>
            </div>

            {/* Search */}
            <div className="px-4 pb-3">
              <div
                className={cn(
                  "flex items-center gap-2 h-10 px-3 rounded-full",
                  "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
                  "focus-within:border-[var(--amber-400)]/50",
                  "transition-colors",
                )}
              >
                <Search className="w-4 h-4 text-[var(--fg-2)] shrink-0" />
                <input
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="搜索会话标题"
                  aria-label="搜索会话"
                  className={cn(
                    "flex-1 bg-transparent text-[14px] text-[var(--fg-0)]",
                    "placeholder:text-[var(--fg-2)] outline-none",
                  )}
                />
                {query && (
                  <button
                    type="button"
                    onClick={() => setQuery("")}
                    aria-label="清除搜索"
                    className="inline-flex items-center justify-center w-6 h-6 -mr-1 rounded-full text-[var(--fg-2)] active:bg-[var(--bg-3)]"
                  >
                    <X className="w-3 h-3" />
                  </button>
                )}
              </div>
            </div>

            {/* Segmented */}
            <div className="px-4 pb-2">
              <SegmentedControl<TabKind>
                value={tab}
                onChange={setTab}
                ariaLabel="会话类型"
                items={[
                  {
                    value: "active",
                    label: "对话",
                    badge: activeTotal || undefined,
                  },
                  {
                    value: "archived",
                    label: "归档",
                    badge: archivedTotal || undefined,
                  },
                ]}
              />
            </div>

            {/* List */}
            <div className="flex-1 overflow-y-auto overscroll-contain">
              {isInitialLoading && <ListSkeleton />}

              {!isInitialLoading && list.isError && (
                <div className="mx-4 my-4 px-3 py-3 rounded-xl bg-[var(--danger)]/10 border border-[var(--danger)]/25 text-[12.5px] text-[var(--danger)]">
                  加载失败
                  <button
                    type="button"
                    onClick={() => list.refetch()}
                    className="ml-2 underline"
                  >
                    重试
                  </button>
                </div>
              )}

              {!isInitialLoading && !list.isError && !hasResults && (
                <EmptyState
                  query={query}
                  tab={tab}
                  onClearQuery={() => setQuery("")}
                  onCreate={handleCreate}
                />
              )}

              {tab === "archived" && hasResults && (
                <ul>
                  {filtered.map((conv) => (
                    <li key={conv.id}>
                      <ConversationRowMobile
                        conv={conv}
                        active={conv.id === currentConvId}
                        onSelect={() => void handleSelect(conv)}
                        onRename={(t) => handleRename(conv, t)}
                        onArchive={() => handleArchive(conv)}
                        onDelete={() => handleDelete(conv)}
                      />
                    </li>
                  ))}
                </ul>
              )}

              {tab === "active" &&
                hasResults &&
                BUCKET_ORDER.map((bucket) => {
                  const items = grouped[bucket];
                  if (items.length === 0) return null;
                  return (
                    <section key={bucket} aria-label={BUCKET_LABEL[bucket]}>
                      <h3
                        className={cn(
                          "px-4 pt-5 pb-2 text-[11px] font-semibold",
                          "tracking-[0.1em] uppercase text-[var(--fg-2)]",
                        )}
                      >
                        {BUCKET_LABEL[bucket]}
                      </h3>
                      <ul>
                        {items.map((conv) => (
                          <li key={conv.id}>
                            <ConversationRowMobile
                              conv={conv}
                              active={conv.id === currentConvId}
                              onSelect={() => void handleSelect(conv)}
                              onRename={(t) => handleRename(conv, t)}
                              onArchive={() => handleArchive(conv)}
                              onDelete={() => handleDelete(conv)}
                            />
                          </li>
                        ))}
                      </ul>
                    </section>
                  );
                })}

              {hasNextPage && (
                <div
                  ref={sentinelRef}
                  className="flex items-center justify-center py-5"
                >
                  {isFetchingNextPage && <Spinner />}
                </div>
              )}

              <div className="h-3 shrink-0" />
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>,
    document.body,
  );
}

function ListSkeleton() {
  return (
    <ul aria-hidden>
      {SKELETON_ROWS.map((row) => (
        <li
          key={row.id}
          className="flex items-center gap-3.5 min-h-[68px] pl-4 pr-3 border-b border-[var(--border-subtle)]"
        >
          <div className="w-11 h-11 rounded-xl bg-[var(--bg-2)] animate-pulse" />
          <div className="flex-1 space-y-1.5">
            <div
              className="h-3 rounded bg-[var(--bg-2)] animate-pulse"
              style={{ width: `${row.titleWidth}%` }}
            />
            <div className="h-2.5 w-24 rounded bg-[var(--bg-2)] animate-pulse" />
          </div>
        </li>
      ))}
    </ul>
  );
}

function EmptyState({
  query,
  tab,
  onClearQuery,
  onCreate,
}: {
  query: string;
  tab: TabKind;
  onClearQuery: () => void;
  onCreate: () => void;
}) {
  if (query) {
    return (
      <div className="px-6 py-12 text-center">
        <div className="mx-auto w-12 h-12 rounded-2xl bg-[var(--bg-2)] flex items-center justify-center mb-3">
          <Search className="w-5 h-5 text-[var(--fg-2)]" />
        </div>
        <p className="text-[14px] text-[var(--fg-1)]">没有匹配的会话</p>
        <p className="text-[12px] text-[var(--fg-2)] mt-1">
          换个关键词试试
        </p>
        <button
          type="button"
          onClick={onClearQuery}
          className="mt-3 text-[12.5px] text-[var(--amber-400)] active:opacity-70"
        >
          清除搜索
        </button>
      </div>
    );
  }
  if (tab === "archived") {
    return (
      <div className="px-6 py-12 text-center">
        <div className="mx-auto w-12 h-12 rounded-2xl bg-[var(--bg-2)] flex items-center justify-center mb-3">
          <Inbox className="w-5 h-5 text-[var(--fg-2)]" />
        </div>
        <p className="text-[14px] text-[var(--fg-1)]">归档为空</p>
        <p className="text-[12px] text-[var(--fg-2)] mt-1">
          长按或左滑会话可归档
        </p>
      </div>
    );
  }
  return (
    <div className="px-6 py-12 text-center">
      <div className="mx-auto w-12 h-12 rounded-2xl bg-[var(--bg-2)] flex items-center justify-center mb-3">
        <Sparkles className="w-5 h-5 text-[var(--amber-400)]" />
      </div>
      <p className="text-[14px] text-[var(--fg-1)]">还没有会话</p>
      <p className="text-[12px] text-[var(--fg-2)] mt-1 mb-4">
        从这里开始你的第一次对话
      </p>
      <button
        type="button"
        onClick={onCreate}
        className={cn(
          "inline-flex items-center gap-1.5 h-9 px-4 rounded-full",
          "bg-[var(--amber-400)] text-black text-[13px] font-medium",
          "active:scale-95 transition-transform",
        )}
      >
        <Plus className="w-3.5 h-3.5" />
        新建会话
      </button>
    </div>
  );
}
