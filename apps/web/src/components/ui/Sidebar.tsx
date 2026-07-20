"use client";

// Lumen V1 侧栏：品牌栏 + 搜索 + 分组列表 + 归档 tab。
// InfiniteQuery 语义不变；搜索只 client-filter 已加载页。

import { useVirtualizer } from "@tanstack/react-virtual";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  Archive,
  Inbox,
  Loader2,
  MessageSquarePlus,
  MoreHorizontal,
  PanelLeftClose,
  Plus,
} from "lucide-react";
import {
  type KeyboardEventHandler,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
} from "react";

import { LumenMark } from "@/components/ui/brand/LumenMark";
import type { ConversationSummary } from "@/lib/apiClient";
import { copy } from "@/lib/copy";
import { DURATION, resolveDrawerMotion } from "@/lib/motion";
import { cn } from "@/lib/utils";
import { Button, IconButton } from "./primitives";
import { ConversationItem, titleOf } from "./sidebar/ConversationItem";
import { SearchBox } from "./sidebar/SearchBox";
import {
  SIDEBAR_BUCKET_LABEL,
  SIDEBAR_BUCKET_ORDER,
  type SidebarController,
  type SidebarTab,
  useSidebarController,
} from "./sidebar/useSidebarController";

// Active 会话按时间分桶；archived 数量较大时才启用虚拟化。
const VIRTUALIZE_AFTER = 60;
const ARCHIVED_ROW_HEIGHT = 56;
const ARCHIVED_ROW_OVERSCAN = 8;

const SIDEBAR_SKELETON_ROWS = [
  { id: "wide", width: 72 },
  { id: "medium", width: 58 },
  { id: "large", width: 66 },
  { id: "small", width: 50 },
] as const;

function sidebarPrimaryActionClass(showBrand: boolean): string {
  return cn("px-4 pb-3", showBrand ? "" : "pt-3");
}

export function Sidebar({
  embedded = false,
  showBrand = !embedded,
  onNavigate,
}: {
  embedded?: boolean;
  showBrand?: boolean;
  onNavigate?: () => void;
} = {}) {
  const controller = useSidebarController({ embedded, onNavigate });
  const reduceMotion = useReducedMotion();
  const drawerMotion = resolveDrawerMotion(reduceMotion, DURATION.quick);
  const handleListKey = useSidebarListKeyNavigation();

  useArchiveMenuDismiss(
    controller.archiveMenuOpen,
    controller.closeArchiveMenu,
  );

  const innerChrome = (
    <SidebarChrome
      controller={controller}
      showBrand={showBrand}
      onListKeyDown={handleListKey}
    />
  );

  if (embedded) return innerChrome;

  return (
    <SidebarShell
      sidebarOpen={controller.sidebarOpen}
      drawerMotion={drawerMotion}
      onToggle={controller.toggleSidebar}
    >
      {innerChrome}
    </SidebarShell>
  );
}

function useArchiveMenuDismiss(open: boolean, onClose: () => void) {
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      const clickedInsideMenu =
        target instanceof Element &&
        Boolean(target.closest("[data-sidebar-archive-menu]"));
      if (!clickedInsideMenu) onClose();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };

    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose, open]);
}

function useSidebarListKeyNavigation(): KeyboardEventHandler<HTMLDivElement> {
  return useCallback((e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = Array.from(
      e.currentTarget.querySelectorAll<HTMLElement>("[data-conv-id]"),
    );
    if (rows.length === 0) return;

    const activeElement = document.activeElement as HTMLElement | null;
    const currentIndex = rows.findIndex(
      (row) => row.contains(activeElement) || row === activeElement,
    );
    const nextIndex =
      e.key === "ArrowDown"
        ? Math.min(rows.length - 1, (currentIndex < 0 ? -1 : currentIndex) + 1)
        : Math.max(0, (currentIndex < 0 ? 1 : currentIndex) - 1);
    const target = rows[nextIndex]?.querySelector<HTMLElement>(
      'button[aria-current], button:first-of-type',
    );
    if (!target) return;

    e.preventDefault();
    target.focus();
  }, []);
}

function SidebarChrome({
  controller,
  showBrand,
  onListKeyDown,
}: {
  controller: SidebarController;
  showBrand: boolean;
  onListKeyDown: KeyboardEventHandler<HTMLDivElement>;
}) {
  return (
    <div className="flex min-h-0 w-full flex-1 flex-col">
      <SidebarBrand
        visible={showBrand}
        onClose={controller.toggleSidebar}
      />
      <SidebarPrimaryAction controller={controller} showBrand={showBrand} />
      <div className="px-4 pb-3">
        <SearchBox value={controller.query} onChange={controller.setQuery} />
      </div>
      <SidebarListHeader controller={controller} />
      <SidebarConversationList
        controller={controller}
        onListKeyDown={onListKeyDown}
      />
      <div className="h-[max(var(--space-4),env(safe-area-inset-bottom,0px))] shrink-0" />
    </div>
  );
}

function SidebarPrimaryAction({
  controller,
  showBrand,
}: {
  controller: SidebarController;
  showBrand: boolean;
}) {
  return (
    <div className={sidebarPrimaryActionClass(showBrand)}>
      <motion.button
        type="button"
        onClick={controller.createConversation}
        disabled={controller.createPending}
        className={cn(
          "group flex h-10 w-full items-center gap-2 rounded-[var(--radius-control)] px-3",
          "border border-[var(--fg-0)] bg-[var(--fg-0)] font-medium text-[var(--bg-0)]",
          "transition-opacity duration-[var(--dur-quick)]",
          "hover:opacity-90 active:opacity-[var(--op-press)]",
          "outline-none focus-visible:shadow-[var(--ring)]",
          "disabled:cursor-wait disabled:opacity-60",
        )}
      >
        {controller.createPending ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : (
          <Plus
            className="h-4 w-4 text-[var(--accent)]"
            strokeWidth={2.5}
          />
        )}
        <span className="flex-1 text-left text-sm">新建会话</span>
        <kbd
          aria-hidden
          className="hidden text-[10px] font-mono tracking-wide opacity-55 sm:inline-flex"
        >
          ⌘N
        </kbd>
      </motion.button>
      {controller.createError && (
        <p role="alert" className="mt-2 text-[11px] leading-snug text-danger">
          新建失败：{controller.createErrorMessage}
        </p>
      )}
    </div>
  );
}

function SidebarListHeader({
  controller,
}: {
  controller: SidebarController;
}) {
  const viewingActive = controller.tab === "active";

  return (
    <div className="px-4 pb-2">
      <div className="flex h-8 items-center justify-between border-b border-[var(--border-subtle)]">
        <span className="text-[11px] font-medium text-[var(--fg-2)]">
          {viewingActive ? "会话" : "已归档"}
        </span>
        <div data-sidebar-archive-menu className="relative">
          <IconButton
            variant="ghost"
            size="sm"
            aria-label="会话列表选项"
            aria-haspopup="menu"
            aria-expanded={controller.archiveMenuOpen}
            onClick={controller.toggleArchiveMenu}
            className="relative h-7 w-7 rounded-[var(--radius-control)] text-[var(--fg-2)] hover:text-[var(--fg-0)]"
          >
            <MoreHorizontal className="h-4 w-4" aria-hidden />
            {controller.archivedTotal > 0 && viewingActive && (
              <span className="absolute right-0.5 top-0.5 h-1.5 w-1.5 rounded-full bg-[var(--accent)]" />
            )}
          </IconButton>
          <AnimatePresence>
            {controller.archiveMenuOpen && (
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
                  onClick={controller.toggleArchiveTab}
                  className={cn(
                    "flex h-10 w-full items-center gap-2 px-3 text-left type-caption",
                    "text-[var(--fg-0)] transition-colors hover:bg-[var(--bg-2)]",
                    "focus-visible:outline-none focus-visible:bg-[var(--bg-2)]",
                  )}
                >
                  {viewingActive ? (
                    <Archive className="h-3.5 w-3.5 text-[var(--fg-2)]" />
                  ) : (
                    <Inbox className="h-3.5 w-3.5 text-[var(--fg-2)]" />
                  )}
                  <span className="flex-1">
                    {viewingActive ? "查看归档" : "返回会话"}
                  </span>
                  {viewingActive && controller.archivedTotal > 0 && (
                    <span className="font-mono text-[10px] text-[var(--fg-3)]">
                      {controller.archivedTotal}
                    </span>
                  )}
                </button>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}

function SidebarConversationList({
  controller,
  onListKeyDown,
}: {
  controller: SidebarController;
  onListKeyDown: KeyboardEventHandler<HTMLDivElement>;
}) {
  return (
    <div
      data-sidebar-scroll
      onKeyDown={onListKeyDown}
      role="region"
      aria-label={
        controller.tab === "active" ? "会话列表" : "归档会话列表"
      }
      className="scrollbar-thin flex-1 space-y-5 overflow-y-auto overscroll-contain px-2 pb-2"
    >
      {controller.isInitialLoading && <ListSkeleton />}

      {!controller.listLoading && controller.listError && (
        <div className="mx-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-2 py-2 type-caption text-danger">
          加载失败
          <Button
            variant="link"
            onClick={controller.retryList}
            className="ml-2 text-danger no-underline underline hover:opacity-80"
          >
            {copy.action.retry}
          </Button>
        </div>
      )}

      {!controller.isInitialLoading &&
        !controller.listError &&
        !controller.hasResults && (
          <EmptyState
            query={controller.query}
            tab={controller.tab}
            onClearQuery={controller.clearQuery}
            onCreate={controller.createConversation}
            creating={controller.createPending}
          />
        )}

      {controller.tab === "archived" && controller.hasResults && (
        <ArchivedList
          items={controller.filteredConversations}
          currentConvId={controller.currentConversationId}
          deletePendingId={controller.deletePendingId}
          patchPendingId={controller.patchPendingId}
          onSelect={controller.selectConversation}
          onRename={controller.renameConversation}
          onArchive={controller.archiveConversation}
          onDelete={controller.deleteConversation}
        />
      )}

      {controller.tab === "active" && controller.hasResults && (
        <ActiveConversationGroups controller={controller} />
      )}

      <SidebarPagination
        hasNextPage={controller.hasNextPage}
        hasError={controller.nextPageError}
        loading={controller.nextPageLoading}
        onLoadMore={controller.loadMore}
      />
    </div>
  );
}

function ActiveConversationGroups({
  controller,
}: {
  controller: SidebarController;
}) {
  return SIDEBAR_BUCKET_ORDER.map((bucket) => {
    const items = controller.groupedConversations[bucket];
    if (items.length === 0) return null;

    return (
      <div key={bucket}>
        <h3 className="mb-1.5 px-3 text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--fg-1)]">
          {SIDEBAR_BUCKET_LABEL[bucket]}
        </h3>
        <ul className="space-y-0.5 px-2">
          {items.map((conversation) => (
            <ConversationItem
              key={conversation.id}
              conv={conversation}
              active={conversation.id === controller.currentConversationId}
              deleting={controller.deletePendingId === conversation.id}
              archiving={controller.patchPendingId === conversation.id}
              renaming={controller.patchPendingId === conversation.id}
              onSelect={() => controller.selectConversation(conversation)}
              onRename={(title) =>
                controller.renameConversation(conversation, title)
              }
              onArchive={(archived) =>
                controller.archiveConversation(conversation, archived)
              }
              onDelete={() => controller.deleteConversation(conversation)}
            />
          ))}
        </ul>
      </div>
    );
  });
}

function SidebarShell({
  sidebarOpen,
  drawerMotion,
  onToggle,
  children,
}: {
  sidebarOpen: boolean;
  drawerMotion: ReturnType<typeof resolveDrawerMotion>;
  onToggle: () => void;
  children: ReactNode;
}) {
  const ariaCommon = { "aria-label": "会话侧栏" } as const;

  return (
    <>
      <AnimatePresence>
        {sidebarOpen && (
          <motion.button
            type="button"
            key="sidebar-scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={drawerMotion.scrimTransition}
            onClick={onToggle}
            aria-label="关闭侧栏"
            className="fixed inset-0 z-30 bg-black/45 backdrop-blur-[2px] md:hidden"
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
              "flex max-h-[100dvh] shrink-0 flex-col overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-1)]",
              "pl-[env(safe-area-inset-left,0px)] pt-[env(safe-area-inset-top,0px)]",
              "[@media(orientation:landscape)_and_(max-height:520px)]:w-[min(360px,55vw)]",
            )}
          >
            {children}
          </motion.aside>
        )}
      </AnimatePresence>

      <aside
        {...ariaCommon}
        aria-hidden={!sidebarOpen}
        inert={!sidebarOpen ? true : undefined}
        className={cn(
          "relative hidden h-[100dvh] shrink-0 flex-col overflow-hidden md:flex",
          "border-r border-[var(--border-subtle)] bg-[var(--bg-1)]",
          "transition-[width,border-color] duration-200 ease-out",
          sidebarOpen
            ? "w-72"
            : "w-0 border-r-0 pointer-events-none",
        )}
      >
        {children}
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
            className="border-danger-border bg-danger-soft text-danger hover:opacity-90"
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
            onRename={(title) => onRename(conv, title)}
            onArchive={(archived) => onArchive(conv, archived)}
            onDelete={() => onDelete(conv)}
          />
        ))}
      </ul>
    );
  }

  const virtualRows = rowVirtualizer.getVirtualItems();

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
                onRename={(title) => onRename(conv, title)}
                onArchive={(archived) => onArchive(conv, archived)}
                onDelete={() => onDelete(conv)}
              />
            </ul>
          </div>
        );
      })}
    </div>
  );
}

function EmptyState({
  query,
  tab,
  onClearQuery,
  onCreate,
  creating,
}: {
  query: string;
  tab: SidebarTab;
  onClearQuery: () => void;
  onCreate: () => void;
  creating: boolean;
}) {
  if (query) {
    return (
      <div className="px-4 py-6 text-center">
        <p className="mb-3 type-caption text-[var(--fg-2)]">
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
        <div className="mx-auto mb-2.5 flex h-10 w-10 items-center justify-center rounded-full bg-white/5">
          <Inbox className="h-4 w-4 text-[var(--fg-2)]" />
        </div>
        <p className="type-caption text-[var(--fg-2)]">归档为空</p>
      </div>
    );
  }
  return (
    <div className="px-4 py-8 text-center">
      <div className="mx-auto mb-2.5 flex h-10 w-10 items-center justify-center rounded-full bg-white/5">
        <Inbox className="h-4 w-4 text-[var(--fg-2)]" />
      </div>
      <p className="mb-3 type-caption text-[var(--fg-2)]">还没有会话</p>
      <Button
        variant="link"
        onClick={onCreate}
        disabled={creating}
        className="text-[var(--accent)] no-underline hover:underline"
        leftIcon={<MessageSquarePlus className="h-3.5 w-3.5" />}
      >
        开始你的第一次对话
      </Button>
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className="space-y-1.5 px-3 py-1">
      {SIDEBAR_SKELETON_ROWS.map((row) => (
        <div
          key={row.id}
          className="flex h-10 items-center gap-2 rounded-[var(--radius-control)] px-2"
        >
          <div className="h-3.5 w-3.5 animate-pulse rounded bg-white/5" />
          <div
            className="h-3 animate-pulse rounded bg-white/5"
            style={{ width: `${row.width}%` }}
          />
        </div>
      ))}
    </div>
  );
}
