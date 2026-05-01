"use client";

// /me 的会话列表：
// - Segmented active / archived（带 badge 数字）
// - active tab 按 dayKeyOf 分桶：今天 / 昨天 / 本周 / 更早
// - archived tab 扁平展示
// - 客户端 title 过滤（query）
// - IntersectionObserver sentinel 触发 fetchNextPage

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Inbox, Search } from "lucide-react";

import { SegmentedControl } from "@/components/ui/primitives/mobile";
import { Spinner } from "@/components/ui/primitives";
import type { ConversationSummary } from "@/lib/apiClient";
import {
  useDeleteConversationMutation,
  useListConversationsInfiniteQuery,
  usePatchConversationMutation,
} from "@/lib/queries";
import { useChatStore } from "@/store/useChatStore";
import { cn } from "@/lib/utils";
import { logWarn } from "@/lib/logger";

import { ConversationRowMobile } from "./ConversationRowMobile";

const VIRTUALIZE_AFTER = 60;

type Bucket = "today" | "yesterday" | "last7" | "older";

const BUCKET_ORDER: Bucket[] = ["today", "yesterday", "last7", "older"];
const BUCKET_LABEL: Record<Bucket, string> = {
  today: "今天",
  yesterday: "昨天",
  last7: "本周",
  older: "更早",
};
const CONVERSATION_LIST_SKELETON_ROWS = [
  { id: "first", titleWidth: 60 },
  { id: "second", titleWidth: 67 },
  { id: "third", titleWidth: 74 },
  { id: "fourth", titleWidth: 81 },
  { id: "fifth", titleWidth: 88 },
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
  const t = c.title?.trim();
  return t || "New Canvas";
}

type TabKind = "active" | "archived";

export interface ConversationListProps {
  query: string;
}

export function ConversationList({ query }: ConversationListProps) {
  const router = useRouter();
  const [tab, setTab] = useState<TabKind>("active");

  const currentConvId = useChatStore((s) => s.currentConvId);
  const setCurrentConv = useChatStore((s) => s.setCurrentConv);
  const loadHistoricalMessages = useChatStore(
    (s) => s.loadHistoricalMessages,
  );

  const list = useListConversationsInfiniteQuery({ limit: 30 });
  const patchMut = usePatchConversationMutation();
  const deleteMut = useDeleteConversationMutation();
  const {
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
  } = list;

  const allConvs: ConversationSummary[] = useMemo(() => {
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

  // Infinite scroll sentinel
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
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
      { rootMargin: "160px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [fetchNextPage, hasNextPage, isFetchingNextPage]);

  const handleSelect = async (conv: ConversationSummary) => {
    setCurrentConv(conv.id);
    try {
      await loadHistoricalMessages(conv.id);
    } catch (err) {
      logWarn("mobile_me.load_historical_messages_failed", {
        scope: "mobile-me",
        extra: { convId: conv.id, err: String(err) },
      });
    }
    router.push("/");
  };

  const handleRename = (conv: ConversationSummary, title: string) => {
    patchMut.mutate({ id: conv.id, title });
  };

  const handleArchive = (conv: ConversationSummary) => {
    patchMut.mutate({ id: conv.id, archived: !conv.archived });
  };

  const handleDelete = (conv: ConversationSummary) => {
    deleteMut.mutate(conv.id, {
      onSuccess: () => {
        if (currentConvId === conv.id) setCurrentConv(null);
      },
    });
  };

  const hasResults = filtered.length > 0;
  const isInitialLoading = list.isLoading && allConvs.length === 0;

  return (
    <div className="flex flex-col">
      {/* Segmented */}
      <div className="px-4 pt-2 pb-3 flex justify-center">
        <SegmentedControl<TabKind>
          value={tab}
          onChange={setTab}
          ariaLabel="会话类型"
          items={[
            { value: "active", label: "对话", badge: activeTotal || undefined },
            {
              value: "archived",
              label: "归档",
              badge: archivedTotal || undefined,
            },
          ]}
        />
      </div>

      {/* 列表 */}
      <div className="flex flex-col">
        {isInitialLoading && <ListSkeleton />}

        {!isInitialLoading && list.isError && (
          <div className="mx-4 my-4 px-3 py-2 rounded-lg bg-[var(--danger)]/10 border border-[var(--danger)]/20 text-[12px] text-[var(--danger)]">
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
          <EmptyState query={query} tab={tab} />
        )}

        {tab === "archived" && hasResults && (
          <ArchivedRows
            items={filtered}
            currentConvId={currentConvId}
            onSelect={(conv) => void handleSelect(conv)}
            onRename={handleRename}
            onArchive={handleArchive}
            onDelete={handleDelete}
          />
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
                    "px-4 pt-6 pb-2.5 text-[11px] font-semibold",
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

        {/* 无限滚动 sentinel */}
        {hasNextPage && (
          <div
            ref={sentinelRef}
            className="flex items-center justify-center py-4"
          >
            {isFetchingNextPage && <Spinner />}
          </div>
        )}
      </div>
    </div>
  );
}

function ArchivedRows({
  items,
  currentConvId,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: {
  items: ConversationSummary[];
  currentConvId: string | null;
  onSelect: (conv: ConversationSummary) => void;
  onRename: (conv: ConversationSummary, title: string) => void;
  onArchive: (conv: ConversationSummary) => void;
  onDelete: (conv: ConversationSummary) => void;
}) {
  const scrollRef = useRef<HTMLUListElement | null>(null);
  const shouldVirtualize = items.length > VIRTUALIZE_AFTER;
  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirtualizer = useVirtualizer({
    count: items.length,
    // /me 页面顶层就是 window 滚动；这里给一个本地容器引用，react-virtual 会按需 fallback。
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 64,
    overscan: 8,
    enabled: shouldVirtualize,
  });

  if (!shouldVirtualize) {
    return (
      <ul ref={scrollRef}>
        {items.map((conv) => (
          <li key={conv.id}>
            <ConversationRowMobile
              conv={conv}
              active={conv.id === currentConvId}
              onSelect={() => onSelect(conv)}
              onRename={(t) => onRename(conv, t)}
              onArchive={() => onArchive(conv)}
              onDelete={() => onDelete(conv)}
            />
          </li>
        ))}
      </ul>
    );
  }

  return (
    <ul
      ref={scrollRef}
      className="relative"
      style={{ height: rowVirtualizer.getTotalSize() }}
    >
      {rowVirtualizer.getVirtualItems().map((virtualRow) => {
        const conv = items[virtualRow.index];
        return (
          <li
            key={conv.id}
            ref={rowVirtualizer.measureElement}
            data-index={virtualRow.index}
            className="absolute left-0 right-0"
            style={{ transform: `translateY(${virtualRow.start}px)` }}
          >
            <ConversationRowMobile
              conv={conv}
              active={conv.id === currentConvId}
              onSelect={() => onSelect(conv)}
              onRename={(t) => onRename(conv, t)}
              onArchive={() => onArchive(conv)}
              onDelete={() => onDelete(conv)}
            />
          </li>
        );
      })}
    </ul>
  );
}

function ListSkeleton() {
  return (
    <ul>
      {CONVERSATION_LIST_SKELETON_ROWS.map((row) => (
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

function EmptyState({ query, tab }: { query: string; tab: TabKind }) {
  if (query) {
    return (
      <div className="px-6 py-14 text-center">
        <div className="mx-auto w-11 h-11 rounded-2xl bg-[var(--bg-2)] flex items-center justify-center mb-3">
          <Search className="w-4.5 h-4.5 text-[var(--fg-2)]" />
        </div>
        <p className="text-[14px] text-[var(--fg-1)]">
          没有匹配的会话
        </p>
        <p className="text-[12px] text-[var(--fg-2)] mt-1">
          尝试不同的关键词搜索
        </p>
      </div>
    );
  }
  if (tab === "archived") {
    return (
      <div className="px-6 py-14 text-center">
        <div className="mx-auto w-11 h-11 rounded-2xl bg-[var(--bg-2)] flex items-center justify-center mb-3">
          <Inbox className="w-4.5 h-4.5 text-[var(--fg-2)]" />
        </div>
        <p className="text-[14px] text-[var(--fg-1)]">暂无归档会话</p>
        <p className="text-[12px] text-[var(--fg-2)] mt-1">
          左滑会话可归档
        </p>
      </div>
    );
  }
  return (
    <div className="px-6 py-14 text-center">
      <div className="mx-auto w-11 h-11 rounded-2xl bg-[var(--bg-2)] flex items-center justify-center mb-3">
        <Inbox className="w-4.5 h-4.5 text-[var(--fg-2)]" />
      </div>
      <p className="text-[14px] text-[var(--fg-1)]">还没有会话</p>
      <p className="text-[12px] text-[var(--fg-2)] mt-1">
        去创作页开始你的第一次对话
      </p>
    </div>
  );
}
