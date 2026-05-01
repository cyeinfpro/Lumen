"use client";

// Lumen V1 侧栏：品牌栏 + 搜索 + 分组列表 + 归档 tab。
// 关键交互：
//  - ⌘/Ctrl+K 聚焦搜索；Esc 清空搜索（SearchBox 内部处理）
//  - ↑/↓ 在列表里走焦点；Enter 打开；Delete 触发删除 popover
//  - 重命名 / 归档 / 删除 全部走内嵌 popover，不弹 window.confirm
//  - InfiniteQuery 语义不变；搜索只 client-filter 已加载页

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Archive,
  Inbox,
  Loader2,
  MessageSquarePlus,
  MoreHorizontal,
  PanelLeftClose,
  Plus,
  Sparkles,
} from "lucide-react";

import { useUiStore } from "@/store/useUiStore";
import { useChatStore } from "@/store/useChatStore";
import {
  useCreateConversationMutation,
  useDeleteConversationMutation,
  useListConversationsInfiniteQuery,
  usePatchConversationMutation,
} from "@/lib/queries";
import type { ConversationSummary } from "@/lib/apiClient";
import { logWarn } from "@/lib/logger";
import { cn } from "@/lib/utils";
import { ConversationItem, titleOf } from "./sidebar/ConversationItem";
import { SearchBox } from "./sidebar/SearchBox";

// 虚拟化阈值：当列表超过此数量时启用本地窗口渲染（archived tab 总是平铺,
// active tab 走分桶,只在 archived tab 数量极多时虚拟化以避免重渲全部 row）。
const VIRTUALIZE_AFTER = 60;
const ARCHIVED_ROW_HEIGHT = 44;
const ARCHIVED_ROW_OVERSCAN = 8;

type Bucket = "today" | "yesterday" | "last7" | "older";

const BUCKET_ORDER: Bucket[] = ["today", "yesterday", "last7", "older"];
const BUCKET_LABEL: Record<Bucket, string> = {
  today: "今天",
  yesterday: "昨天",
  last7: "本周",
  older: "更早",
};
const SIDEBAR_SKELETON_ROWS = [
  { id: "wide", width: 72 },
  { id: "medium", width: 58 },
  { id: "large", width: 66 },
  { id: "small", width: 50 },
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

type TabKind = "active" | "archived";

export function Sidebar() {
  const { sidebarOpen, toggleSidebar, setSidebarOpen } = useUiStore();
  const studioView = useUiStore((s) => s.studioView);
  const setStudioView = useUiStore((s) => s.setStudioView);
  const currentConvId = useChatStore((s) => s.currentConvId);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const loadHistoricalMessages = useChatStore((s) => s.loadHistoricalMessages);

  const [tab, setTab] = useState<TabKind>("active");
  const [query, setQuery] = useState("");
  const listRef = useRef<HTMLDivElement | null>(null);
  const archiveMenuRef = useRef<HTMLDivElement | null>(null);
  const [archiveMenuOpen, setArchiveMenuOpen] = useState(false);

  useEffect(() => {
    const query = window.matchMedia("(min-width: 768px)");
    const syncSidebarWithViewport = () => setSidebarOpen(query.matches);
    syncSidebarWithViewport();
    query.addEventListener("change", syncSidebarWithViewport);
    return () => query.removeEventListener("change", syncSidebarWithViewport);
  }, [setSidebarOpen]);

  // 移动端抽屉打开时锁定 body 滚动；viewport / open 变化时自动 rerun
  useEffect(() => {
    if (typeof window === "undefined") return;
    const mobileQuery = window.matchMedia("(max-width: 767px)");
    const apply = () => {
      const shouldLock = sidebarOpen && mobileQuery.matches;
      document.body.style.overflow = shouldLock ? "hidden" : "";
    };
    apply();
    mobileQuery.addEventListener("change", apply);
    return () => {
      mobileQuery.removeEventListener("change", apply);
      document.body.style.overflow = "";
    };
  }, [sidebarOpen]);

  // Esc 关闭移动端抽屉，并把焦点回退到打开 sidebar 之前聚焦的触发元素（a11y）
  const triggerElementRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!sidebarOpen) return;
    if (typeof window === "undefined") return;
    // 记录打开前的 active element 作为焦点回退目标
    const active = document.activeElement;
    if (active instanceof HTMLElement && active !== document.body) {
      triggerElementRef.current = active;
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (window.matchMedia("(max-width: 767px)").matches) {
        setSidebarOpen(false);
        const trigger = triggerElementRef.current;
        if (trigger && document.body.contains(trigger)) {
          // 用 rAF 让关闭动画的 unmount 先发生，再回焦
          window.requestAnimationFrame(() => trigger.focus());
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sidebarOpen, setSidebarOpen]);

  // 路由 / 会话切换时自动收起移动端抽屉（桌面端不受影响）
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!window.matchMedia("(max-width: 767px)").matches) return;
    setSidebarOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentConvId]);

  useEffect(() => {
    if (!archiveMenuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      const root = archiveMenuRef.current;
      if (root && event.target instanceof Node && !root.contains(event.target)) {
        setArchiveMenuOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setArchiveMenuOpen(false);
    };
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [archiveMenuOpen]);

  const list = useListConversationsInfiniteQuery({ limit: 30 });
  const createMut = useCreateConversationMutation({
    onSuccess: (conv) => {
      setCurrentConv(conv.id);
    },
  });
  const deleteMut = useDeleteConversationMutation();
  const patchMut = usePatchConversationMutation();

  const allConvs: ConversationSummary[] = useMemo(() => {
    const pages = list.data?.pages ?? [];
    return pages.flatMap((p) => p.items);
  }, [list.data]);

  // 先按 tab 分流，再按 query 过滤，再按时间分桶（仅 active tab）
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

  // 用于计算归档总数（显示在 tab badge 上）
  const archivedTotal = useMemo(
    () => allConvs.filter((c) => c.archived).length,
    [allConvs],
  );

  // 扁平 id 列表（按渲染顺序），键盘导航用
  const flatIds = useMemo(() => {
    if (tab === "archived") return filtered.map((c) => c.id);
    return BUCKET_ORDER.flatMap((b) => grouped[b].map((c) => c.id));
  }, [filtered, grouped, tab]);

  const handleNewCanvas = useCallback(() => {
    if (createMut.isPending) return;
    createMut.mutate({});
  }, [createMut]);

  const handleSelect = useCallback(
    async (conv: ConversationSummary) => {
      if (conv.id === currentConvId) return;
      setCurrentConv(conv.id);
      try {
        await loadHistoricalMessages(conv.id);
        // 加载成功后再关移动端抽屉，失败不关避免用户失去上下文
        if (
          typeof window !== "undefined" &&
          window.innerWidth < 768 &&
          sidebarOpen
        ) {
          toggleSidebar();
        }
      } catch (err) {
        logWarn("sidebar.load_historical_messages_failed", {
          scope: "sidebar",
          extra: { convId: conv.id, err: String(err) },
        });
      }
    },
    [currentConvId, setCurrentConv, loadHistoricalMessages, sidebarOpen, toggleSidebar],
  );

  const handleRename = useCallback(
    (conv: ConversationSummary, nextTitle: string) => {
      patchMut.mutate({ id: conv.id, title: nextTitle });
    },
    [patchMut],
  );

  const handleArchive = useCallback(
    (conv: ConversationSummary, nextArchived: boolean) => {
      patchMut.mutate({ id: conv.id, archived: nextArchived });
    },
    [patchMut],
  );

  const handleDelete = useCallback(
    (conv: ConversationSummary) => {
      deleteMut.mutate(conv.id, {
        onSuccess: () => {
          if (currentConvId === conv.id) setCurrentConv(null);
        },
      });
    },
    [deleteMut, currentConvId, setCurrentConv],
  );

  // 缓存 rows DOM 引用，避免每次 keydown 跑 querySelectorAll
  const rowsCacheRef = useRef<HTMLElement[]>([]);
  useEffect(() => {
    const root = listRef.current;
    if (!root) {
      rowsCacheRef.current = [];
      return;
    }
    rowsCacheRef.current = Array.from(
      root.querySelectorAll<HTMLElement>("[data-conv-id]"),
    );
  }, [flatIds]);

  // 键盘导航：在列表区按 ↑/↓ 遍历；Enter 打开；Delete 触发删除 popover（通过 more 按钮 focus）
  const handleListKey = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const rows = rowsCacheRef.current;
      if (rows.length === 0) return;
      const activeEl = document.activeElement as HTMLElement | null;
      const currentIndex = rows.findIndex(
        (r) => r.contains(activeEl) || r === activeEl,
      );
      const nextIdx =
        e.key === "ArrowDown"
          ? Math.min(rows.length - 1, (currentIndex < 0 ? -1 : currentIndex) + 1)
          : Math.max(0, (currentIndex < 0 ? 1 : currentIndex) - 1);
      const target = rows[nextIdx]?.querySelector<HTMLElement>(
        'button[aria-current], button:first-of-type',
      );
      if (target) {
        e.preventDefault();
        target.focus();
      }
    },
    [],
  );

  const hasResults = filtered.length > 0;
  const isInitialLoading = list.isLoading && allConvs.length === 0;

  const innerChrome = (
    <div className="flex-1 flex flex-col w-full min-h-0">
        {/* ——— 品牌栏 ——— */}
        <div className="px-4 pt-4 pb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="relative w-6 h-6 rounded-full bg-gradient-to-br from-[var(--accent)] to-orange-300 flex items-center justify-center shadow-[0_0_12px_rgba(242,169,58,0.4)]">
              <Sparkles className="w-3 h-3 text-black/70" strokeWidth={2.5} />
            </span>
            <span className="font-medium tracking-tight text-[var(--fg-0)]">
              Lumen
            </span>
          </div>
          <button
            type="button"
            onClick={toggleSidebar}
            aria-label="收起侧栏"
            title="收起侧栏"
            className="md:hidden w-7 h-7 inline-flex items-center justify-center rounded-md text-neutral-500 hover:text-white hover:bg-white/5 active:scale-[0.95] transition-all"
          >
            <PanelLeftClose className="w-4 h-4" />
          </button>
        </div>

        {/* ——— 主 CTA：新建会话 ——— */}
        <div className="px-4 pb-3">
          <motion.button
            type="button"
            onClick={handleNewCanvas}
            disabled={createMut.isPending}
            whileHover={createMut.isPending ? undefined : { scale: 1.02 }}
            whileTap={createMut.isPending ? undefined : { scale: 0.96 }}
            transition={{ type: "spring", stiffness: 400, damping: 25 }}
            className={cn(
              "group w-full flex items-center gap-2 h-10 px-3 rounded-xl",
              "bg-gradient-to-br from-[var(--accent)] to-[#D68A1E] text-black font-medium",
              "shadow-[0_6px_20px_-6px_rgba(242,169,58,0.55)]",
              "hover:shadow-[0_8px_26px_-6px_rgba(242,169,58,0.75)] hover:brightness-[1.04]",
              "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60 focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--bg-1)]",
              "disabled:opacity-60 disabled:cursor-wait",
            )}
          >
            {createMut.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Plus className="w-4 h-4" strokeWidth={2.5} />
            )}
            <span className="text-sm flex-1 text-left">新建会话</span>
            <kbd
              aria-hidden
              className="hidden sm:inline-flex px-1.5 py-0.5 rounded bg-black/15 text-[10px] font-mono tracking-wide"
            >
              ⌘N
            </kbd>
          </motion.button>
          {createMut.isError && (
            <p className="mt-2 text-[11px] text-red-300/80 leading-snug">
              新建失败：{createMut.error?.message ?? "未知错误"}
            </p>
          )}
        </div>

        {/* ——— 搜索 ——— */}
        <div className="px-4 pb-3">
          <SearchBox value={query} onChange={setQuery} />
        </div>

        {/* ——— 高频入口：对话 / 图片；归档收进更多菜单 ——— */}
        <div className="px-4 pb-2" role="tablist" aria-label="会话类型">
          <div className="flex gap-1 p-0.5 rounded-lg bg-white/[0.03] border border-white/5">
            <TabButton
              active={studioView === "chat" && tab === "active"}
              onClick={() => {
                setStudioView("chat");
                setTab("active");
              }}
              label="对话"
              controls="sidebar-tabpanel-active"
              id="sidebar-tab-active"
            />
            <TabButton
              active={studioView === "images"}
              onClick={() => {
                setStudioView("images");
                setArchiveMenuOpen(false);
                setSidebarOpen(false);
              }}
              label="图片"
              controls="conversation-image-gallery"
              id="sidebar-tab-images"
            />
            <div ref={archiveMenuRef} className="relative">
              <button
                type="button"
                aria-label="更多会话入口"
                aria-haspopup="menu"
                aria-expanded={archiveMenuOpen}
                onClick={() => setArchiveMenuOpen((open) => !open)}
                className={cn(
                  "relative h-8 w-9 rounded-md inline-flex items-center justify-center text-xs transition-colors",
                  "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
                  studioView === "chat" && tab === "archived"
                    ? "bg-white/10 text-[var(--fg-0)] shadow-sm"
                    : "text-neutral-400 hover:text-neutral-200",
                )}
              >
                <MoreHorizontal className="h-4 w-4" aria-hidden />
                {archivedTotal > 0 && (
                  <span className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-[var(--accent)]" />
                )}
              </button>
              <AnimatePresence>
                {archiveMenuOpen && (
                  <motion.div
                    role="menu"
                    aria-label="更多会话入口"
                    initial={{ opacity: 0, y: -4, scale: 0.98 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: -4, scale: 0.98 }}
                    transition={{ duration: 0.14 }}
                    className={cn(
                      "absolute right-0 top-10 z-20 min-w-40 overflow-hidden rounded-lg",
                      "border border-[var(--border-subtle)] bg-[var(--bg-1)] shadow-[var(--shadow-3)]",
                    )}
                  >
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setStudioView("chat");
                        setTab("archived");
                        setArchiveMenuOpen(false);
                      }}
                      className={cn(
                        "flex h-10 w-full items-center gap-2 px-3 text-left text-xs",
                        "text-[var(--fg-0)] hover:bg-[var(--bg-2)] transition-colors",
                        "focus-visible:outline-none focus-visible:bg-[var(--bg-2)]",
                      )}
                    >
                      <Archive className="h-3.5 w-3.5 text-[var(--fg-2)]" />
                      <span className="flex-1">查看归档</span>
                      {archivedTotal > 0 && (
                        <span className="font-mono text-[10px] text-[var(--fg-3)]">
                          {archivedTotal}
                        </span>
                      )}
                    </button>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </div>
        </div>

        {/* ——— 列表 ——— */}
        <div
          ref={listRef}
          onKeyDown={handleListKey}
          role="tabpanel"
          id={tab === "active" ? "sidebar-tabpanel-active" : "sidebar-tabpanel-archived"}
          aria-labelledby={tab === "active" ? "sidebar-tab-active" : undefined}
          className="flex-1 overflow-y-auto px-2 pb-2 space-y-5 scrollbar-thin"
        >
          {isInitialLoading && <ListSkeleton />}

          {!list.isLoading && list.isError && (
            <div className="mx-2 px-2 py-2 rounded-lg bg-red-500/10 border border-red-500/20 text-xs text-red-300">
              加载失败
              <button
                type="button"
                onClick={() => list.refetch()}
                className="ml-2 underline hover:text-red-200"
              >
                重试
              </button>
            </div>
          )}

          {/* 空态 */}
          {!isInitialLoading && !list.isError && !hasResults && (
            <EmptyState
              query={query}
              tab={tab}
              onClearQuery={() => setQuery("")}
              onCreate={handleNewCanvas}
              creating={createMut.isPending}
            />
          )}

          {/* 归档 tab：平铺（不分桶）。数量大时切换虚拟化避免全量 re-render。 */}
          {tab === "archived" && hasResults && (
            <ArchivedList
              items={filtered}
              currentConvId={currentConvId}
              deletePendingId={
                deleteMut.isPending ? (deleteMut.variables as string | undefined) : undefined
              }
              patchPendingId={patchMut.isPending ? patchMut.variables?.id : undefined}
              scrollRef={listRef}
              onSelect={handleSelect}
              onRename={handleRename}
              onArchive={handleArchive}
              onDelete={handleDelete}
            />
          )}

          {/* active tab：按时间分桶 */}
          {tab === "active" &&
            hasResults &&
            BUCKET_ORDER.map((bucket) => {
              const items = grouped[bucket];
              if (items.length === 0) return null;
              return (
                <div key={bucket}>
                  <h3 className="text-[10px] font-semibold text-[var(--fg-1)] uppercase tracking-[0.08em] mb-1.5 px-3">
                    {BUCKET_LABEL[bucket]}
                  </h3>
                  <ul className="space-y-0.5 px-2">
                    {items.map((conv) => (
                      <ConversationItem
                        key={conv.id}
                        conv={conv}
                        active={conv.id === currentConvId}
                        deleting={
                          deleteMut.isPending &&
                          deleteMut.variables === conv.id
                        }
                        archiving={
                          patchMut.isPending &&
                          patchMut.variables?.id === conv.id
                        }
                        renaming={
                          patchMut.isPending &&
                          patchMut.variables?.id === conv.id
                        }
                        onSelect={() => handleSelect(conv)}
                        onRename={(t) => handleRename(conv, t)}
                        onArchive={(next) => handleArchive(conv, next)}
                        onDelete={() => handleDelete(conv)}
                      />
                    ))}
                  </ul>
                </div>
              );
            })}

          {/* 分页 */}
          {list.hasNextPage && (
            <div className="px-4 pt-1">
              {list.isFetchNextPageError ? (
                <button
                  type="button"
                  onClick={() => list.fetchNextPage()}
                  disabled={list.isFetchingNextPage}
                  className="w-full h-8 text-xs text-red-300 hover:text-red-200 rounded-lg bg-red-500/10 border border-red-500/20 transition-colors disabled:opacity-50"
                >
                  {list.isFetchingNextPage ? "重试中…" : "加载失败,重试"}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => list.fetchNextPage()}
                  disabled={list.isFetchingNextPage}
                  className="w-full h-8 text-xs text-neutral-400 hover:text-white rounded-lg hover:bg-white/5 transition-colors disabled:opacity-50 inline-flex items-center justify-center gap-1.5"
                >
                  {list.isFetchingNextPage && (
                    <Loader2 className="w-3 h-3 animate-spin" />
                  )}
                  {list.isFetchingNextPage ? "加载中…" : "加载更多"}
                </button>
              )}
            </div>
          )}
        </div>

      {/* 分隔底栏（保持视觉节奏） */}
      <div className="h-4 shrink-0" />
    </div>
  );

  const ariaCommon = { "aria-label": "会话侧栏" } as const;

  return (
    <>
      {/* 移动端：AnimatePresence 管理 overlay + 抽屉；桌面端另起一条路径 */}
      <AnimatePresence>
        {sidebarOpen && (
          <motion.button
            type="button"
            key="sidebar-scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={toggleSidebar}
            aria-label="关闭侧栏"
            className="md:hidden fixed inset-0 bg-black/45 backdrop-blur-[2px] z-30"
          />
        )}
      </AnimatePresence>
      <AnimatePresence>
        {sidebarOpen && (
          <motion.aside
            key="sidebar-mobile"
            {...ariaCommon}
            initial={{ x: "-100%" }}
            animate={{ x: 0 }}
            exit={{ x: "-100%" }}
            transition={{ type: "spring", damping: 32, stiffness: 360 }}
            className={cn(
              "md:hidden fixed top-0 left-0 z-40 h-[100dvh]",
              "w-[min(320px,85vw)] max-[375px]:w-[85vw]",
              "bg-[var(--bg-1)] border-r border-white/5 flex flex-col overflow-hidden shrink-0",
              "pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]",
            )}
          >
            {innerChrome}
          </motion.aside>
        )}
      </AnimatePresence>

      {/* 桌面端：始终在 DOM，用 CSS width 控制展开/收起，不走 framer-motion */}
      <aside
        {...ariaCommon}
        className={cn(
          "hidden md:flex relative h-[100dvh] shrink-0 flex-col overflow-hidden",
          "bg-[var(--bg-1)] border-r border-white/5",
          "transition-[width,border-color] duration-200 ease-out",
          sidebarOpen ? "w-72" : "w-0 border-r-0 pointer-events-none",
        )}
      >
        {innerChrome}
      </aside>
    </>
  );
}

// ——— 子组件：归档列表（数量大时虚拟化） ———
function ArchivedList({
  items,
  currentConvId,
  deletePendingId,
  patchPendingId,
  scrollRef,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: {
  items: ConversationSummary[];
  currentConvId: string | null;
  deletePendingId: string | undefined;
  patchPendingId: string | undefined;
  scrollRef: React.RefObject<HTMLDivElement | null>;
  onSelect: (conv: ConversationSummary) => void;
  onRename: (conv: ConversationSummary, nextTitle: string) => void;
  onArchive: (conv: ConversationSummary, nextArchived: boolean) => void;
  onDelete: (conv: ConversationSummary) => void;
}) {
  const shouldVirtualize = items.length > VIRTUALIZE_AFTER;
  const [viewport, setViewport] = useState({ scrollTop: 0, height: 0 });

  useEffect(() => {
    if (!shouldVirtualize) return;
    const scrollEl = scrollRef.current;
    if (!scrollEl) return;

    const updateViewport = () => {
      setViewport({
        scrollTop: scrollEl.scrollTop,
        height: scrollEl.clientHeight,
      });
    };

    updateViewport();
    scrollEl.addEventListener("scroll", updateViewport, { passive: true });
    const resizeObserver =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(updateViewport);
    resizeObserver?.observe(scrollEl);

    return () => {
      scrollEl.removeEventListener("scroll", updateViewport);
      resizeObserver?.disconnect();
    };
  }, [scrollRef, shouldVirtualize, items.length]);

  if (!shouldVirtualize) {
    return (
      <ul className="space-y-0.5 px-2">
        {items.map((conv) => (
          <ConversationItem
            key={conv.id}
            conv={conv}
            active={conv.id === currentConvId}
            deleting={deletePendingId === conv.id}
            archiving={patchPendingId === conv.id}
            renaming={patchPendingId === conv.id}
            onSelect={() => onSelect(conv)}
            onRename={(t) => onRename(conv, t)}
            onArchive={(next) => onArchive(conv, next)}
            onDelete={() => onDelete(conv)}
          />
        ))}
      </ul>
    );
  }

  const startIndex = Math.max(
    0,
    Math.floor(viewport.scrollTop / ARCHIVED_ROW_HEIGHT) - ARCHIVED_ROW_OVERSCAN,
  );
  const visibleCount =
    Math.ceil(viewport.height / ARCHIVED_ROW_HEIGHT) + ARCHIVED_ROW_OVERSCAN * 2;
  const endIndex = Math.min(items.length, startIndex + visibleCount);
  const virtualRows = items.slice(startIndex, endIndex);

  // 虚拟化模式：固定行高窗口渲染；ConversationItem 内部仍渲染 <li>。
  return (
    <div
      role="list"
      className="relative px-2"
      style={{ height: items.length * ARCHIVED_ROW_HEIGHT }}
    >
      {virtualRows.map((conv, offset) => {
        const index = startIndex + offset;
        return (
          <div
            key={conv.id}
            data-index={index}
            className="absolute left-0 right-0"
            style={{ transform: `translateY(${index * ARCHIVED_ROW_HEIGHT}px)` }}
          >
            <ul className="space-y-0.5">
              <ConversationItem
                conv={conv}
                active={conv.id === currentConvId}
                deleting={deletePendingId === conv.id}
                archiving={patchPendingId === conv.id}
                renaming={patchPendingId === conv.id}
                onSelect={() => onSelect(conv)}
                onRename={(t) => onRename(conv, t)}
                onArchive={(next) => onArchive(conv, next)}
                onDelete={() => onDelete(conv)}
              />
            </ul>
          </div>
        );
      })}
    </div>
  );
}

// ——— 子组件：tab 按钮 ———
function TabButton({
  active,
  onClick,
  label,
  badge,
  controls,
  id,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  badge?: number;
  controls?: string;
  id?: string;
}) {
  return (
    <button
      type="button"
      role="tab"
      id={id}
      aria-selected={active}
      aria-controls={controls}
      onClick={onClick}
      className={cn(
        "flex-1 h-8 inline-flex items-center justify-center gap-1.5 rounded-md text-xs transition-all",
        "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
        active
          ? "bg-white/10 text-[var(--fg-0)] shadow-sm"
          : "text-neutral-400 hover:text-neutral-200",
      )}
    >
      {label}
      {typeof badge === "number" && (
        <span
          className={cn(
            "inline-flex items-center justify-center h-4 min-w-[18px] px-1 rounded-full text-[10px] font-mono",
            active
              ? "bg-[var(--accent)]/20 text-[var(--accent)]"
              : "bg-white/5 text-neutral-500",
          )}
        >
          {badge}
        </span>
      )}
    </button>
  );
}

// ——— 空态 ———
function EmptyState({
  query,
  tab,
  onClearQuery,
  onCreate,
  creating,
}: {
  query: string;
  tab: TabKind;
  onClearQuery: () => void;
  onCreate: () => void;
  creating: boolean;
}) {
  if (query) {
    return (
      <div className="px-4 py-6 text-center">
        <p className="text-xs text-neutral-500 mb-3">
          没有匹配「{query}」的会话
        </p>
        <button
          type="button"
          onClick={onClearQuery}
          className="text-xs text-[var(--accent)] hover:underline"
        >
          清除搜索
        </button>
      </div>
    );
  }
  if (tab === "archived") {
    return (
      <div className="px-4 py-8 text-center">
        <div className="mx-auto w-10 h-10 rounded-full bg-white/5 flex items-center justify-center mb-2.5">
          <Inbox className="w-4 h-4 text-neutral-500" />
        </div>
        <p className="text-xs text-neutral-500">归档为空</p>
      </div>
    );
  }
  return (
    <div className="px-4 py-8 text-center">
      <div className="mx-auto w-10 h-10 rounded-full bg-white/5 flex items-center justify-center mb-2.5">
        <Inbox className="w-4 h-4 text-neutral-500" />
      </div>
      <p className="text-xs text-neutral-400 mb-3">还没有会话</p>
      <button
        type="button"
        onClick={onCreate}
        disabled={creating}
        className="inline-flex items-center gap-1.5 text-xs text-[var(--accent)] hover:underline disabled:opacity-60"
      >
        <MessageSquarePlus className="w-3.5 h-3.5" />
        开始你的第一次对话
      </button>
    </div>
  );
}

// ——— 骨架 ———
function ListSkeleton() {
  return (
    <div className="px-3 py-1 space-y-1.5">
      {SIDEBAR_SKELETON_ROWS.map((row) => (
        <div
          key={row.id}
          className="flex items-center gap-2 h-10 px-2 rounded-md"
        >
          <div className="w-3.5 h-3.5 rounded bg-white/5 animate-pulse" />
          <div
            className="h-3 rounded bg-white/5 animate-pulse"
            style={{ width: `${row.width}%` }}
          />
        </div>
      ))}
    </div>
  );
}
