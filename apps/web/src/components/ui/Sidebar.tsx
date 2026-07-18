"use client";

// Lumen V1 侧栏：品牌栏 + 搜索 + 分组列表 + 归档 tab。
// 关键交互：
//  - 顶部按钮或 ⌘/Ctrl+B 切换侧栏；Esc 清空搜索（SearchBox 内部处理）
//  - ↑/↓ 在列表里走焦点；Enter 打开；Delete 触发删除 popover
//  - 重命名 / 归档 / 删除 全部走内嵌 popover，不弹 window.confirm
//  - InfiniteQuery 语义不变；搜索只 client-filter 已加载页

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Archive,
  Inbox,
  Loader2,
  MessageSquarePlus,
  MoreHorizontal,
  PanelLeftClose,
  Plus,
} from "lucide-react";

import { LumenMark } from "@/components/ui/brand/LumenMark";
import { useUiStore } from "@/store/useUiStore";
import { useChatStore } from "@/store/useChatStore";
import { acquireBodyScrollLock } from "@/hooks/useBodyScrollLock";
import {
  useCreateConversationMutation,
  useDeleteConversationMutation,
  useListConversationsInfiniteQuery,
  usePatchConversationMutation,
} from "@/lib/queries";
import type { ConversationSummary } from "@/lib/apiClient";
import { logWarn } from "@/lib/logger";
import { cn } from "@/lib/utils";
import { copy } from "@/lib/copy";
import { DURATION, resolveDrawerMotion } from "@/lib/motion";
import { Button, IconButton } from "./primitives";
import { ConversationItem, titleOf } from "./sidebar/ConversationItem";
import { SearchBox } from "./sidebar/SearchBox";

// 虚拟化阈值：当列表超过此数量时启用本地窗口渲染（archived tab 总是平铺,
// active tab 走分桶,只在 archived tab 数量极多时虚拟化以避免重渲全部 row）。
const VIRTUALIZE_AFTER = 60;
const ARCHIVED_ROW_HEIGHT = 56;
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

function sidebarPrimaryActionClass(showBrand: boolean): string {
  return cn("px-4 pb-3", showBrand ? "" : "pt-3");
}

type TabKind = "active" | "archived";

export function Sidebar({
  embedded = false,
  showBrand = !embedded,
  onNavigate,
}: {
  embedded?: boolean;
  showBrand?: boolean;
  onNavigate?: () => void;
} = {}) {
  const { sidebarOpen, toggleSidebar, setSidebarOpen } = useUiStore();
  const reduceMotion = useReducedMotion();
  const drawerMotion = resolveDrawerMotion(reduceMotion, DURATION.quick);
  const currentConvId = useChatStore((s) => s.currentConvId);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const loadHistoricalMessages = useChatStore((s) => s.loadHistoricalMessages);

  const [tab, setTab] = useState<TabKind>("active");
  const [query, setQuery] = useState("");
  const [archiveMenuOpen, setArchiveMenuOpen] = useState(false);

  // 移动端抽屉打开时锁定 body 滚动；viewport / open 变化时自动 rerun
  useEffect(() => {
    if (typeof window === "undefined") return;
    const mobileQuery = window.matchMedia("(max-width: 767px)");
    let releaseScrollLock: (() => void) | null = null;
    const apply = () => {
      const shouldLock = sidebarOpen && mobileQuery.matches;
      if (shouldLock && !releaseScrollLock) {
        releaseScrollLock = acquireBodyScrollLock();
      } else if (!shouldLock && releaseScrollLock) {
        releaseScrollLock();
        releaseScrollLock = null;
      }
    };
    apply();
    mobileQuery.addEventListener("change", apply);
    return () => {
      mobileQuery.removeEventListener("change", apply);
      releaseScrollLock?.();
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
      const target = event.target;
      const clickedInsideMenu =
        target instanceof Element &&
        Boolean(target.closest("[data-sidebar-archive-menu]"));
      if (!clickedInsideMenu) {
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
      onNavigate?.();
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

  useEffect(() => {
    if (!list.hasNextPage || list.isFetchingNextPage) return;
    if (query.trim()) return;

    const currentLoaded =
      currentConvId == null || allConvs.some((conv) => conv.id === currentConvId);
    const tabHasResults =
      tab === "archived"
        ? allConvs.some((conv) => conv.archived)
        : allConvs.some((conv) => !conv.archived);

    if (currentLoaded && tabHasResults) return;
    void list.fetchNextPage();
  }, [
    allConvs,
    currentConvId,
    list,
    list.hasNextPage,
    list.isFetchingNextPage,
    query,
    tab,
  ]);

  const handleNewCanvas = useCallback(() => {
    if (createMut.isPending) return;
    createMut.mutate({});
  }, [createMut]);

  const handleSelect = useCallback(
    async (conv: ConversationSummary) => {
      if (conv.id === currentConvId) {
        onNavigate?.();
        if (
          !embedded &&
          typeof window !== "undefined" &&
          window.matchMedia("(max-width: 767px)").matches
        ) {
          setSidebarOpen(false);
        }
        return;
      }
      setCurrentConv(conv.id);
      try {
        await loadHistoricalMessages(conv.id);
        onNavigate?.();
        // 加载成功后再关移动端抽屉，失败不关避免用户失去上下文。
        if (
          !embedded &&
          typeof window !== "undefined" &&
          window.matchMedia("(max-width: 767px)").matches
        ) {
          setSidebarOpen(false);
        }
      } catch (err) {
        logWarn("sidebar.load_historical_messages_failed", {
          scope: "sidebar",
          extra: { convId: conv.id, err: String(err) },
        });
      }
    },
    [
      currentConvId,
      embedded,
      loadHistoricalMessages,
      onNavigate,
      setCurrentConv,
      setSidebarOpen,
    ],
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

  // 键盘导航：在列表区按 ↑/↓ 遍历；Enter 打开；Delete 触发删除 popover（通过 more 按钮 focus）
  const handleListKey = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const rows = Array.from(
        e.currentTarget.querySelectorAll<HTMLElement>("[data-conv-id]"),
      );
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
    <div className="flex min-h-0 w-full flex-1 flex-col">
        {/* ——— 品牌栏 ——— */}
        <SidebarBrand visible={showBrand} onClose={toggleSidebar} />

        {/* ——— 主 CTA：新建会话 ——— */}
        <div className={sidebarPrimaryActionClass(showBrand)}>
          <motion.button
            type="button"
            onClick={handleNewCanvas}
            disabled={createMut.isPending}
            className={cn(
              "group w-full flex items-center gap-2 h-10 px-3 rounded-[var(--radius-control)]",
              "border border-[var(--fg-0)] bg-[var(--fg-0)] text-[var(--bg-0)] font-medium",
              "transition-opacity duration-[var(--dur-quick)]",
              "hover:opacity-90 active:opacity-[var(--op-press)]",
              "outline-none focus-visible:shadow-[var(--ring)]",
              "disabled:opacity-60 disabled:cursor-wait",
            )}
          >
            {createMut.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Plus
                className="w-4 h-4 text-[var(--accent)]"
                strokeWidth={2.5}
              />
            )}
            <span className="text-sm flex-1 text-left">新建会话</span>
            <kbd
              aria-hidden
              className="hidden text-[10px] font-mono tracking-wide opacity-55 sm:inline-flex"
            >
              ⌘N
            </kbd>
          </motion.button>
          {createMut.isError && (
            <p role="alert" className="mt-2 text-[11px] text-danger leading-snug">
              新建失败：{createMut.error?.message ?? "未知错误"}
            </p>
          )}
        </div>

        {/* ——— 搜索 ——— */}
        <div className="px-4 pb-3">
          <SearchBox value={query} onChange={setQuery} />
        </div>

        {/* ——— 会话列表标题；图片视图只在工作区切换，避免重复入口 ——— */}
        <div className="px-4 pb-2">
          <div className="flex h-8 items-center justify-between border-b border-[var(--border-subtle)]">
            <span className="text-[11px] font-medium text-[var(--fg-2)]">
              {tab === "active" ? "会话" : "已归档"}
            </span>
            <div data-sidebar-archive-menu className="relative">
              <IconButton
                variant="ghost"
                size="sm"
                aria-label="会话列表选项"
                aria-haspopup="menu"
                aria-expanded={archiveMenuOpen}
                onClick={() => setArchiveMenuOpen((open) => !open)}
                className="relative h-7 w-7 rounded-[var(--radius-control)] text-[var(--fg-2)] hover:text-[var(--fg-0)]"
              >
                <MoreHorizontal className="h-4 w-4" aria-hidden />
                {archivedTotal > 0 && tab === "active" && (
                  <span className="absolute right-0.5 top-0.5 h-1.5 w-1.5 rounded-full bg-[var(--accent)]" />
                )}
              </IconButton>
              <AnimatePresence>
                {archiveMenuOpen && (
                  <motion.div
                    role="menu"
                    aria-label="会话列表选项"
                    initial={{ opacity: 0, y: -4, scale: 0.98 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: -4, scale: 0.98 }}
                    transition={{ duration: 0.14 }}
                    className={cn(
                      "absolute right-0 top-9 z-20 min-w-40 overflow-hidden rounded-[var(--radius-card)]",
                      "border border-[var(--border-subtle)] bg-[var(--bg-1)] shadow-[var(--shadow-3)]",
                    )}
                  >
                    {/* @list-item-ok: menu item, 行内 a11y role + 横向布局 */}
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setTab(tab === "active" ? "archived" : "active");
                        setArchiveMenuOpen(false);
                      }}
                      className={cn(
                        "flex h-10 w-full items-center gap-2 px-3 text-left type-caption",
                        "text-[var(--fg-0)] hover:bg-[var(--bg-2)] transition-colors",
                        "focus-visible:outline-none focus-visible:bg-[var(--bg-2)]",
                      )}
                    >
                      {tab === "active" ? (
                        <Archive className="h-3.5 w-3.5 text-[var(--fg-2)]" />
                      ) : (
                        <Inbox className="h-3.5 w-3.5 text-[var(--fg-2)]" />
                      )}
                      <span className="flex-1">
                        {tab === "active" ? "查看归档" : "返回会话"}
                      </span>
                      {tab === "active" && archivedTotal > 0 && (
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
          data-sidebar-scroll
          onKeyDown={handleListKey}
          role="region"
          aria-label={tab === "active" ? "会话列表" : "归档会话列表"}
          className="scrollbar-thin flex-1 space-y-5 overflow-y-auto overscroll-contain px-2 pb-2"
        >
          {isInitialLoading && <ListSkeleton />}

          {!list.isLoading && list.isError && (
            <div className="mx-2 px-2 py-2 rounded-[var(--radius-card)] bg-danger-soft border border-danger-border type-caption text-danger">
              加载失败
              <Button
                variant="link"
                onClick={() => list.refetch()}
                className="ml-2 text-danger no-underline underline hover:opacity-80"
              >
                {copy.action.retry}
              </Button>
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
          <SidebarPagination
            hasNextPage={Boolean(list.hasNextPage)}
            hasError={list.isFetchNextPageError}
            loading={list.isFetchingNextPage}
            onLoadMore={() => list.fetchNextPage()}
          />
        </div>

      {/* 分隔底栏（保持视觉节奏） */}
      <div className="h-[max(var(--space-4),env(safe-area-inset-bottom,0px))] shrink-0" />
    </div>
  );

  const ariaCommon = { "aria-label": "会话侧栏" } as const;

  if (embedded) {
    return innerChrome;
  }

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
            transition={drawerMotion.scrimTransition}
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
            initial={drawerMotion.panelInitial}
            animate={drawerMotion.panelAnimate}
            exit={drawerMotion.panelExit}
            transition={drawerMotion.panelTransition}
            className={cn(
              "fixed inset-y-0 left-0 z-40 md:hidden",
              "w-[min(320px,calc(100vw-44px))] min-w-[min(276px,calc(100vw-44px))]",
              "bg-[var(--bg-1)] border-r border-[var(--border-subtle)] flex flex-col overflow-hidden shrink-0",
              "pl-[env(safe-area-inset-left,0px)] pt-[env(safe-area-inset-top,0px)]",
              "max-h-[100dvh] [@media(orientation:landscape)_and_(max-height:520px)]:w-[min(360px,55vw)]",
            )}
          >
            {innerChrome}
          </motion.aside>
        )}
      </AnimatePresence>

      {/* 桌面端：始终在 DOM，用 CSS width 控制展开/收起，不走 framer-motion */}
      <aside
        {...ariaCommon}
        aria-hidden={!sidebarOpen}
        inert={!sidebarOpen ? true : undefined}
        className={cn(
          "relative hidden h-[100dvh] shrink-0 flex-col overflow-hidden md:flex",
          "bg-[var(--bg-1)] border-r border-[var(--border-subtle)]",
          "transition-[width,border-color] duration-200 ease-out",
          sidebarOpen ? "w-72" : "w-0 border-r-0 pointer-events-none",
        )}
      >
        {innerChrome}
      </aside>
    </>
  );
}

function SidebarPagination({
  hasNextPage,
  hasError,
  loading,
  onLoadMore,
}: {
  hasNextPage: boolean;
  hasError: boolean;
  loading: boolean;
  onLoadMore: () => void;
}) {
  if (!hasNextPage) return null;

  return (
    <div className="px-4 pt-1">
      {hasError ? (
        <div role="alert">
          <Button
            variant="secondary"
            size="sm"
            fullWidth
            onClick={onLoadMore}
            disabled={loading}
            className="bg-danger-soft border-danger-border text-danger hover:opacity-90"
          >
            {loading ? "重试中" : "加载失败,重试"}
          </Button>
        </div>
      ) : (
        <Button
          variant="ghost"
          size="sm"
          fullWidth
          onClick={onLoadMore}
          disabled={loading}
          loading={loading}
        >
          {loading ? copy.state.loading : "加载更多"}
        </Button>
      )}
    </div>
  );
}

function SidebarBrand({
  visible,
  onClose,
}: {
  visible: boolean;
  onClose: () => void;
}) {
  if (!visible) return null;

  return (
    <div className="flex items-center justify-between px-4 pb-3 pt-4">
      <div className="flex items-center gap-2">
        <LumenMark className="h-6 w-6 text-[var(--accent)]" />
        <span className="font-medium tracking-tight text-[var(--fg-0)]">
          Lumen
        </span>
      </div>
      <IconButton
        variant="ghost"
        size="sm"
        onClick={onClose}
        aria-label="收起侧栏"
        tooltip="收起侧栏"
        className="md:hidden"
      >
        <PanelLeftClose className="h-4 w-4" />
      </IconButton>
    </div>
  );
}

// ——— 子组件：归档列表（数量大时虚拟化） ———
function ArchivedList({
  items,
  currentConvId,
  deletePendingId,
  patchPendingId,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: {
  items: ConversationSummary[];
  currentConvId: string | null;
  deletePendingId: string | undefined;
  patchPendingId: string | undefined;
  onSelect: (conv: ConversationSummary) => void;
  onRename: (conv: ConversationSummary, nextTitle: string) => void;
  onArchive: (conv: ConversationSummary, nextArchived: boolean) => void;
  onDelete: (conv: ConversationSummary) => void;
}) {
  const shouldVirtualize = items.length > VIRTUALIZE_AFTER;
  const virtualRootRef = useRef<HTMLDivElement | null>(null);
  const layoutKey = useMemo(
    () =>
      JSON.stringify(
        items.map((conv) => [conv.id, titleOf(conv), conv.archived]),
      ),
    [items],
  );
  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () =>
      virtualRootRef.current?.closest<HTMLDivElement>(
        "[data-sidebar-scroll]",
      ) ?? null,
    estimateSize: () => ARCHIVED_ROW_HEIGHT,
    getItemKey: (index) => items[index]?.id ?? index,
    overscan: ARCHIVED_ROW_OVERSCAN,
    enabled: shouldVirtualize,
  });

  useEffect(() => {
    if (!shouldVirtualize) return;
    rowVirtualizer.measure();
  }, [layoutKey, rowVirtualizer, shouldVirtualize]);

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

  const virtualRows = rowVirtualizer.getVirtualItems();

  // 虚拟化模式：按稳定会话 id 缓存并测量真实行高；ConversationItem 内部仍渲染 <li>。
  return (
    <div
      ref={virtualRootRef}
      role="list"
      className="relative px-2"
      style={{ height: rowVirtualizer.getTotalSize() }}
    >
      {virtualRows.map((virtualRow) => {
        const conv = items[virtualRow.index];
        if (!conv) return null;
        return (
          <div
            key={virtualRow.key}
            ref={rowVirtualizer.measureElement}
            data-index={virtualRow.index}
            className="absolute left-0 right-0 pb-0.5"
            style={{ transform: `translateY(${virtualRow.start}px)` }}
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
        <p className="type-caption text-[var(--fg-2)] mb-3">
          没有匹配「{query}」的会话
        </p>
        <Button
          variant="link"
          onClick={onClearQuery}
          className="text-[var(--accent)] no-underline hover:underline"
        >
          清除搜索
        </Button>
      </div>
    );
  }
  if (tab === "archived") {
    return (
      <div className="px-4 py-8 text-center">
        <div className="mx-auto w-10 h-10 rounded-full bg-white/5 flex items-center justify-center mb-2.5">
          <Inbox className="w-4 h-4 text-[var(--fg-2)]" />
        </div>
        <p className="type-caption text-[var(--fg-2)]">归档为空</p>
      </div>
    );
  }
  return (
    <div className="px-4 py-8 text-center">
      <div className="mx-auto w-10 h-10 rounded-full bg-white/5 flex items-center justify-center mb-2.5">
        <Inbox className="w-4 h-4 text-[var(--fg-2)]" />
      </div>
      <p className="type-caption text-[var(--fg-2)] mb-3">还没有会话</p>
      <Button
        variant="link"
        onClick={onCreate}
        disabled={creating}
        className="text-[var(--accent)] no-underline hover:underline"
        leftIcon={<MessageSquarePlus className="w-3.5 h-3.5" />}
      >
        开始你的第一次对话
      </Button>
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
          className="flex items-center gap-2 h-10 px-2 rounded-[var(--radius-control)]"
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
