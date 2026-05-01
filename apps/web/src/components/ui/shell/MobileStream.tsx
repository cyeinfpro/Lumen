"use client";

import {
  type Dispatch,
  type SetStateAction,
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowUp, Loader2 } from "lucide-react";
import { MobileTabBar } from "./MobileTabBar";
import { PullToRefresh, pushMobileToast } from "@/components/ui/primitives/mobile";
import {
  flattenFeed,
  feedTotal,
  useStreamFeedQuery,
  type StreamFeedFilters,
} from "@/lib/queries/stream";
import { useCreateMultiShareMutation } from "@/lib/queries";
import {
  FilterBar,
  GenerationMasonry,
  StreamErrorState,
  StreamLoadingState,
  StreamNeverState,
  StreamNoResultsState,
  StreamOverview,
  StreamSearchBar,
  StreamTopBar,
} from "@/components/ui/stream";
import { cn } from "@/lib/utils";
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

export function MobileStream() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const initialFilters = useMemo(
    () => parseFilters(new URLSearchParams(searchParams.toString())),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [filters, setFilters] =
    useState<StreamFeedFilters>(initialFilters);

  const applyFilters = useCallback(
    (next: StreamFeedFilters, setter: Dispatch<SetStateAction<StreamFeedFilters>> = setFilters) => {
      setter(next);
      const qs = filtersToQueryString(next);
      router.replace(`/stream${qs}`, { scroll: false });
    },
    [router],
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
  const [filterOpen, setFilterOpen] = useState(() => hasAnyFilter(initialFilters));
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
  const [compact, setCompact] = useState(false);
  const [showScrollTop, setShowScrollTop] = useState(false);
  const compactRef = useRef(false);
  const showScrollTopRef = useRef(false);
  const scrollRafRef = useRef<number | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const fetchingNextRef = useRef(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      if (scrollRafRef.current !== null) return;
      scrollRafRef.current = window.requestAnimationFrame(() => {
        scrollRafRef.current = null;
        const top = el.scrollTop;
        const nextCompact = top > 60;
        const nextShowScrollTop = top > 400;
        if (nextCompact !== compactRef.current) {
          compactRef.current = nextCompact;
          setCompact(nextCompact);
        }
        if (nextShowScrollTop !== showScrollTopRef.current) {
          showScrollTopRef.current = nextShowScrollTop;
          setShowScrollTop(nextShowScrollTop);
        }
      });
    };
    onScroll();
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
    const query = deferredQ.trim().toLowerCase();
    if (!query) return items;
    return items.filter((it) => it.prompt.toLowerCase().includes(query));
  }, [items, deferredQ]);

  const selectedImageIds = useMemo(() => {
    if (selectedIds.size === 0) return [];
    return items
      .map((it) => it.image.id)
      .filter((imageId) => selectedIds.has(imageId));
  }, [items, selectedIds]);

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
      if (result !== "cancelled") {
        pushMobileToast(result === "shared" ? "已打开分享菜单" : "分享链接已复制", "success");
        clearSelection();
      }
    } catch {
      pushMobileToast("分享链接生成失败", "danger");
    }
  }, [clearSelection, createMultiShareMutation, selectedImageIds]);

  const promptCount = useMemo(() => {
    const s = new Set<string>();
    for (const it of items) s.add(it.prompt);
    return s.size;
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

  const scrollToTop = useCallback(() => {
    scrollRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  return (
    <div className="relative flex h-[100dvh] w-full min-w-0 flex-col bg-[var(--bg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <StreamTopBar
        compact={compact}
        total={total}
        promptCount={promptCount}
        searchActive={searchOpen}
        filterActive={filterOpen || hasAnyFilter(filters)}
        onToggleSearch={onToggleSearch}
        onToggleFilter={onToggleFilter}
      />

      <div
        className="flex-1 min-h-0"
        style={{
          paddingBottom: "calc(56px + env(safe-area-inset-bottom, 0px))",
        }}
      >
        <PullToRefresh
          containerRef={scrollRef}
          onRefresh={async () => {
            await query.refetch();
          }}
          className="h-full"
        >
          <div className="mx-auto max-w-[640px] px-1">
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
              open={filterOpen}
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
                onRefresh={() => {
                  void query.refetch();
                }}
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
              <StreamLoadingState />
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
                selectionMode={selectionActive}
                selectedIds={selectedIds}
                onToggleSelect={toggleSelectedImage}
              />
            )}

            <div ref={sentinelRef} aria-hidden className="h-8" />
            {isFetchingNextPage && (
              <div className="flex items-center justify-center gap-2.5 py-5 text-[var(--fg-2)]">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span className="text-body-sm">加载更多</span>
              </div>
            )}
          </div>
        </PullToRefresh>
      </div>

      <button
        type="button"
        aria-label="回到顶部"
        onClick={scrollToTop}
        className={cn(
          "fixed right-4 z-30 flex h-11 w-11 cursor-pointer items-center justify-center rounded-full",
          "border border-[var(--border-subtle)] bg-[var(--bg-1)]/85 text-[var(--fg-1)] shadow-lg backdrop-blur-xl",
          "transition-[opacity,transform] duration-200",
          "hover:text-[var(--fg-0)] active:scale-95",
          showScrollTop
            ? "pointer-events-auto translate-y-0 opacity-100"
            : "pointer-events-none translate-y-3 opacity-0",
        )}
        style={{ bottom: "calc(72px + env(safe-area-inset-bottom, 0px))" }}
      >
        <ArrowUp className="h-[18px] w-[18px]" />
      </button>

      <MobileTabBar />
    </div>
  );
}
