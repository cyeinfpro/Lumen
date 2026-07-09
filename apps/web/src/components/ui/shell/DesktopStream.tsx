"use client";

import {
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { usePathname } from "next/navigation";
import { ArrowUp, Filter, Loader2, RefreshCw, Search } from "lucide-react";

import { DesktopTopNav } from "@/components/ui/shell";
import { IconButton } from "@/components/ui/primitives";
import {
  FilterBar,
  GenerationMasonry,
  StreamErrorState,
  StreamLoadingState,
  StreamNeverState,
  StreamNoResultsState,
  StreamOverview,
  StreamSearchBar,
} from "@/components/ui/stream";
import {
  flattenFeed,
  feedTotal,
  useStreamFeedQuery,
  type StreamFeedFilters,
} from "@/lib/queries/stream";
import { useCreateMultiShareMutation } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { shareOrCopyLink } from "@/lib/shareLink";

function parseFilters(sp: URLSearchParams): StreamFeedFilters {
  const ratio = sp.get("ratio") ?? undefined;
  const has_ref = sp.get("has_ref") === "1";
  const fast = sp.get("fast") === "1";
  return { ratio, has_ref, fast };
}

function filtersToQueryString(f: StreamFeedFilters): string {
  const p = new URLSearchParams();
  if (f.ratio) p.set("ratio", f.ratio);
  if (f.has_ref) p.set("has_ref", "1");
  if (f.fast) p.set("fast", "1");
  const s = p.toString();
  return s ? `?${s}` : "";
}

function hasAnyFilter(f: StreamFeedFilters): boolean {
  return Boolean(f.ratio || f.has_ref || f.fast);
}

interface ToolbarProps {
  total: number;
  searchActive: boolean;
  filterActive: boolean;
  onToggleSearch: () => void;
  onToggleFilter: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}

function StreamToolbar({
  total,
  searchActive,
  filterActive,
  onToggleSearch,
  onToggleFilter,
  onRefresh,
  refreshing,
}: ToolbarProps) {
  return (
    <div className="flex items-center gap-2">
      <span className="mr-1 type-caption tabular-nums">
        {total} 张
      </span>
      <IconButton
        size="md"
        aria-label="搜索"
        aria-pressed={searchActive}
        onClick={onToggleSearch}
        className={cn(
          "rounded-full",
          searchActive
            ? "bg-[var(--bg-3)] text-[var(--fg-0)]"
            : "bg-[var(--bg-2)]",
        )}
      >
        <Search className="w-4 h-4" />
      </IconButton>
      <IconButton
        size="md"
        aria-label="筛选"
        aria-pressed={filterActive}
        onClick={onToggleFilter}
        className={cn(
          "rounded-full",
          filterActive
            ? "bg-[var(--bg-3)] text-[var(--fg-0)]"
            : "bg-[var(--bg-2)]",
        )}
      >
        <Filter className="w-4 h-4" />
      </IconButton>
      <IconButton
        size="md"
        aria-label="刷新"
        onClick={onRefresh}
        disabled={refreshing}
        className="rounded-full bg-[var(--bg-2)]"
      >
        <RefreshCw className={cn("w-4 h-4", refreshing && "animate-spin")} />
      </IconButton>
    </div>
  );
}

export function DesktopStream() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const isWide = useMediaQuery("(min-width: 1180px)");
  const desktopCols = isWide ? 4 : 3;

  const queryString = searchParams.toString();
  const queryFilters = useMemo(
    () => parseFilters(new URLSearchParams(queryString)),
    [queryString],
  );
  const highlightId = useMemo(
    () => new URLSearchParams(queryString).get("highlight")?.trim() ?? "",
    [queryString],
  );
  const filters = queryFilters;
  const [filterOpen, setFilterOpen] = useState(() => hasAnyFilter(queryFilters));

  const applyFilters = useCallback(
    (next: StreamFeedFilters) => {
      const qs = filtersToQueryString(next);
      router.replace(`${pathname || "/stream"}${qs}`, { scroll: false });
    },
    [pathname, router],
  );

  const clearFilters = useCallback(() => {
    applyFilters({});
  }, [applyFilters]);

  const query = useStreamFeedQuery(filters);
  const hasNextPage = query.hasNextPage;
  const isFetchingNextPage = query.isFetchingNextPage;
  const fetchNextPage = query.fetchNextPage;
  const items = useMemo(() => flattenFeed(query.data), [query.data]);
  const total = feedTotal(query.data);

  const [searchOpen, setSearchOpen] = useState(false);
  const [q, setQ] = useState("");
  const deferredQ = useDeferredValue(q);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const createMultiShareMutation = useCreateMultiShareMutation();

  const clearAllControls = useCallback(() => {
    setQ("");
    setSearchOpen(false);
    setFilterOpen(false);
    applyFilters({});
  }, [applyFilters]);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const [showScrollTop, setShowScrollTop] = useState(false);
  const showScrollTopRef = useRef(false);
  const scrollRafRef = useRef<number | null>(null);
  const fetchingNextRef = useRef(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      if (scrollRafRef.current !== null) return;
      scrollRafRef.current = window.requestAnimationFrame(() => {
        scrollRafRef.current = null;
        const nextShowScrollTop = el.scrollTop > 400;
        if (nextShowScrollTop !== showScrollTopRef.current) {
          showScrollTopRef.current = nextShowScrollTop;
          setShowScrollTop(nextShowScrollTop);
        }
      });
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
      if (scrollRafRef.current !== null) {
        window.cancelAnimationFrame(scrollRafRef.current);
        scrollRafRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    const sent = sentinelRef.current;
    const root = scrollRef.current;
    if (!sent || !root) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (
            e.isIntersecting &&
            hasNextPage &&
            !isFetchingNextPage &&
            !fetchingNextRef.current
          ) {
            fetchingNextRef.current = true;
            void fetchNextPage().finally(() => {
              fetchingNextRef.current = false;
            });
          }
        }
      },
      { root, rootMargin: "0px 0px 800px 0px", threshold: 0 },
    );
    io.observe(sent);
    return () => io.disconnect();
  }, [fetchNextPage, hasNextPage, isFetchingNextPage]);

  const filteredItems = useMemo(() => {
    const needle = deferredQ.trim().toLowerCase();
    if (!needle) return items;
    return items.filter((it) => it.prompt.toLowerCase().includes(needle));
  }, [items, deferredQ]);

  const selectedImageIds = useMemo(() => {
    if (selectedIds.size === 0) return [];
    return filteredItems
      .map((it) => it.image.id)
      .filter((imageId) => selectedIds.has(imageId));
  }, [filteredItems, selectedIds]);

  const selectionActive = selectionMode || selectedImageIds.length > 0;
  const toggleSelectedImage = useCallback((imageId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(imageId)) next.delete(imageId);
      else next.add(imageId);
      return next;
    });
  }, []);
  const clearSelection = useCallback(() => {
    setSelectedIds(new Set());
    setSelectionMode(false);
  }, []);
  const toggleSelectionMode = useCallback(() => {
    setSelectionMode((value) => !value);
  }, []);
  const shareSelectedImages = useCallback(async () => {
    if (selectedImageIds.length === 0 || createMultiShareMutation.isPending) return;
    try {
      const share = await createMultiShareMutation.mutateAsync({
        imageIds: selectedImageIds,
      });
      const result = await shareOrCopyLink(share.url, "Lumen 图片分享");
      if (result === "failed") {
        pushMobileToast("分享链接复制失败", "danger");
      } else if (result !== "cancelled") {
        pushMobileToast(result === "shared" ? "已打开分享菜单" : "分享链接已复制", "success");
        clearSelection();
      }
    } catch {
      pushMobileToast("分享链接生成失败", "danger");
    }
  }, [clearSelection, createMultiShareMutation, selectedImageIds]);

  const promptCount = useMemo(() => {
    const prompts = new Set<string>();
    for (const it of items) prompts.add(it.prompt);
    return prompts.size;
  }, [items]);

  const isLoading = query.isPending;
  const isEmptyAll = !isLoading && items.length === 0;
  const isEmptyFiltered =
    !isLoading && items.length > 0 && filteredItems.length === 0;

  const onToggleSearch = useCallback(() => {
    setSearchOpen((v) => {
      const next = !v;
      if (!next) setQ("");
      return next;
    });
  }, []);
  const onToggleFilter = useCallback(() => setFilterOpen((v) => !v), []);
  const onToggleReferenceFilter = useCallback(() => {
    applyFilters({ ...filters, has_ref: !filters.has_ref });
  }, [applyFilters, filters]);
  const onToggleFastFilter = useCallback(() => {
    applyFilters({ ...filters, fast: !filters.fast });
  }, [applyFilters, filters]);

  const onRefresh = useCallback(() => {
    void query.refetch();
  }, [query]);

  const scrollToTop = useCallback(() => {
    scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full flex-col bg-[var(--bg-0)]">
      <DesktopTopNav
        active="assets"
        right={
          <StreamToolbar
            total={total}
            searchActive={searchOpen}
            filterActive={filterOpen || hasAnyFilter(filters)}
            onToggleSearch={onToggleSearch}
            onToggleFilter={onToggleFilter}
            onRefresh={onRefresh}
            refreshing={query.isRefetching}
          />
        }
      />

      <main
        ref={scrollRef}
        className="min-h-0 flex-1 overflow-y-auto"
      >
        <div className="mx-auto max-w-[1280px] px-8 py-6">
          <StreamSearchBar
            open={searchOpen}
            value={q}
            onChange={setQ}
            resultCount={filteredItems.length}
            loadedCount={items.length}
            onClose={() => {
              setSearchOpen(false);
              setQ("");
            }}
          />
          <FilterBar
            open={filterOpen || hasAnyFilter(filters)}
            filters={filters}
            onChange={(next) => applyFilters(next)}
            onClear={clearFilters}
          />

          {!isLoading && !query.isError && (items.length > 0 || hasAnyFilter(filters) || q.trim()) && (
            <StreamOverview
              total={total}
              loaded={items.length}
              visible={filteredItems.length}
              promptCount={promptCount}
              filters={filters}
              searchValue={deferredQ}
              refreshing={query.isRefetching}
              onRefresh={onRefresh}
              onClearFilters={clearAllControls}
              onToggleReferenceFilter={onToggleReferenceFilter}
              onToggleFastFilter={onToggleFastFilter}
              selectionMode={selectionActive}
              selectedCount={selectedImageIds.length}
              sharingSelected={createMultiShareMutation.isPending}
              onToggleSelectionMode={toggleSelectionMode}
              onClearSelection={clearSelection}
              onShareSelected={shareSelectedImages}
            />
          )}

          {query.isError ? (
            <StreamErrorState
              message={query.error?.message}
              onRetry={() => {
                void query.refetch();
              }}
            />
          ) : isLoading ? (
            <StreamLoadingState columns={desktopCols} />
          ) : isEmptyAll ? (
            hasAnyFilter(filters) ? (
              <StreamNoResultsState onClear={clearAllControls} />
            ) : (
              <StreamNeverState />
            )
          ) : isEmptyFiltered ? (
            <StreamNoResultsState searchValue={q} onClear={clearAllControls} />
          ) : (
            <GenerationMasonry
              items={filteredItems}
              feed={filteredItems}
              columns={desktopCols}
              selectionMode={selectionActive}
              selectedIds={selectedIds}
              onToggleSelect={toggleSelectedImage}
              highlightId={highlightId}
            />
          )}

          <div ref={sentinelRef} aria-hidden className="h-8" />
          {isFetchingNextPage && (
            <div className="flex items-center justify-center gap-2 py-4 text-[var(--fg-2)]">
              <Loader2 className="h-4 w-4 animate-spin" />
              <span className="type-caption">加载更多</span>
            </div>
          )}
        </div>
      </main>

      <IconButton
        size="lg"
        aria-label="回到顶部"
        onClick={scrollToTop}
        className={cn(
          "fixed bottom-8 right-8 z-30 rounded-full",
          "bg-[var(--bg-1)]/80 backdrop-blur-md shadow-[var(--shadow-2)]",
          "border border-[var(--border-subtle)]",
          "transition-[opacity,transform] duration-200",
          showScrollTop
            ? "pointer-events-auto translate-y-0 opacity-100"
            : "pointer-events-none translate-y-2 opacity-0",
        )}
      >
        <ArrowUp className="h-4 w-4" />
      </IconButton>
    </div>
  );
}
