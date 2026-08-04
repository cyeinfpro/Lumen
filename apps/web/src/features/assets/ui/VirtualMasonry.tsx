"use client";

import { isThisWeek, isToday, isYesterday } from "date-fns";
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  createPrewarmScheduler,
  type PrewarmScheduler,
} from "../model/prewarmScheduler";
import type { GenerationSummary } from "../model/contracts";
import { GenerationTile } from "./AssetTile";
import { createGenerationTileModel } from "../model/tileModel";
import {
  openStreamLightbox,
  streamLightboxWindow,
} from "../model/lightbox";
import {
  layoutVirtualMasonry,
  mountedTileBudget,
  selectVirtualMasonryTiles,
  virtualTileForId,
  type VirtualMasonryGroup,
} from "../model/virtualMasonry";

export interface GenerationMasonryProps {
  items: GenerationSummary[];
  feed: GenerationSummary[];
  columns?: number;
  selectionMode?: boolean;
  selectedIds?: Set<string>;
  onToggleSelect?: (imageId: string) => void;
  highlightId?: string | null;
}

type Bucket = "today" | "yesterday" | "week" | "older";

const BUCKET_LABEL: Record<Bucket, string> = {
  today: "今天",
  yesterday: "昨天",
  week: "本周",
  older: "更早",
};

const BUCKET_ORDER: Bucket[] = ["today", "yesterday", "week", "older"];

function bucketOf(iso: string): Bucket {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "older";
  if (isToday(date)) return "today";
  if (isYesterday(date)) return "yesterday";
  if (isThisWeek(date, { weekStartsOn: 1 })) return "week";
  return "older";
}

function compareFeedOrder(
  a: GenerationSummary,
  b: GenerationSummary,
): number {
  const aTimestamp = new Date(a.created_at).getTime();
  const bTimestamp = new Date(b.created_at).getTime();
  const aValid = !Number.isNaN(aTimestamp);
  const bValid = !Number.isNaN(bTimestamp);
  if (aValid && bValid && aTimestamp !== bTimestamp) {
    return bTimestamp - aTimestamp;
  }
  if (aValid !== bValid) return aValid ? -1 : 1;
  return b.id.localeCompare(a.id);
}

function findScrollContainer(root: HTMLElement): HTMLElement | Window {
  let current = root.parentElement;
  while (current) {
    const style = window.getComputedStyle(current);
    if (/(auto|scroll)/.test(style.overflowY)) return current;
    current = current.parentElement;
  }
  return window;
}

function scrollViewport(
  root: HTMLElement,
  scroller: HTMLElement | Window,
): { start: number; end: number } {
  const rootRect = root.getBoundingClientRect();
  if (scroller === window) {
    const start = Math.max(0, -rootRect.top);
    return { start, end: start + window.innerHeight };
  }
  const element = scroller as HTMLElement;
  const scrollerRect = element.getBoundingClientRect();
  const start = Math.max(0, scrollerRect.top - rootRect.top);
  return { start, end: start + element.clientHeight };
}

function imageSizesFor(columnCount: number): string {
  if (columnCount <= 2) return "(max-width: 767px) 50vw, 50vw";
  if (columnCount === 3) return "(max-width: 1179px) 33vw, 33vw";
  return "25vw";
}

function prewarmLightboxWindow(
  scheduler: PrewarmScheduler,
  items: GenerationSummary[],
  initialGenerationId: string,
): void {
  const windowItems = streamLightboxWindow(items, initialGenerationId, 2);
  const currentIndex = windowItems.findIndex(
    (item) => item.id === initialGenerationId,
  );
  for (const [index, item] of windowItems.entries()) {
    const model = createGenerationTileModel(item);
    scheduler.scheduleImages(
      model.openPrewarmSources,
      {
        priority: index === currentIndex ? "open-intent" : "neighbor",
        assetKind: "display",
      },
      index === currentIndex ? 2 : 1,
    );
  }
}

function GenerationMasonryComponent({
  items,
  feed,
  columns = 2,
  selectionMode = false,
  selectedIds,
  onToggleSelect,
  highlightId,
}: GenerationMasonryProps) {
  const columnCount = Math.max(1, Math.floor(columns));
  const gap = columnCount > 2 ? 14 : 8;
  const rootRef = useRef<HTMLDivElement | null>(null);
  const scrollerRef = useRef<HTMLElement | Window | null>(null);
  const frameRef = useRef<number | null>(null);
  const [scheduler] = useState(() => createPrewarmScheduler());
  const [containerWidth, setContainerWidth] = useState(columnCount * 280);
  const [viewport, setViewport] = useState({ start: 0, end: 900 });

  const orderedFeed = useMemo(
    () => feed.slice().sort(compareFeedOrder),
    [feed],
  );
  const groups = useMemo(() => {
    const grouped = new Map<Bucket, GenerationSummary[]>();
    for (const item of items) {
      const bucket = bucketOf(item.created_at);
      const bucketItems = grouped.get(bucket) ?? [];
      bucketItems.push(item);
      grouped.set(bucket, bucketItems);
    }
    const result: VirtualMasonryGroup<GenerationSummary>[] = [];
    for (const bucket of BUCKET_ORDER) {
      const bucketItems = grouped.get(bucket);
      if (!bucketItems?.length) continue;
      result.push({
        key: bucket,
        label: BUCKET_LABEL[bucket],
        items: bucketItems.slice().sort(compareFeedOrder),
      });
    }
    return result;
  }, [items]);
  const layout = useMemo(
    () =>
      layoutVirtualMasonry(groups, containerWidth, columnCount, gap),
    [columnCount, containerWidth, gap, groups],
  );
  const viewportHeight = Math.max(1, viewport.end - viewport.start);
  const mountedTiles = useMemo(
    () =>
      selectVirtualMasonryTiles(
        layout.tiles,
        viewport.start,
        viewport.end,
        Math.max(600, viewportHeight * 1.5),
        mountedTileBudget(columnCount),
      ),
    [columnCount, layout.tiles, viewport.end, viewport.start, viewportHeight],
  );
  const imageSizes = imageSizesFor(columnCount);

  useEffect(() => {
    scheduler.connect();
    return () => scheduler.destroy();
  }, [scheduler]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const scroller = findScrollContainer(root);
    scrollerRef.current = scroller;
    const update = () => {
      frameRef.current = null;
      setContainerWidth(Math.max(1, root.clientWidth));
      setViewport(scrollViewport(root, scroller));
    };
    const scheduleUpdate = () => {
      if (frameRef.current !== null) return;
      frameRef.current = window.requestAnimationFrame(update);
    };
    update();
    scroller.addEventListener("scroll", scheduleUpdate, { passive: true });
    window.addEventListener("resize", scheduleUpdate, { passive: true });
    const resizeObserver =
      typeof ResizeObserver === "function"
        ? new ResizeObserver(scheduleUpdate)
        : null;
    resizeObserver?.observe(root);
    if (scroller !== window) resizeObserver?.observe(scroller as HTMLElement);
    return () => {
      scroller.removeEventListener("scroll", scheduleUpdate);
      window.removeEventListener("resize", scheduleUpdate);
      resizeObserver?.disconnect();
      if (frameRef.current !== null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
      scrollerRef.current = null;
    };
  }, []);

  useEffect(() => {
    const target = highlightId?.trim();
    const root = rootRef.current;
    const scroller = scrollerRef.current;
    if (!target || !root || !scroller) return;
    const tile = virtualTileForId(layout.tiles, target);
    if (!tile) return;
    const rootRect = root.getBoundingClientRect();
    const viewportSize =
      scroller === window
        ? window.innerHeight
        : (scroller as HTMLElement).clientHeight;
    const scrollerTop =
      scroller === window
        ? 0
        : (scroller as HTMLElement).getBoundingClientRect().top;
    const delta =
      rootRect.top -
      scrollerTop +
      tile.top -
      Math.max(0, (viewportSize - tile.height) / 2);
    scroller.scrollBy({ top: delta, behavior: "smooth" });
  }, [highlightId, layout.tiles]);

  const onOpenItem = useCallback(
    (itemId: string, rect: DOMRect) => {
      prewarmLightboxWindow(scheduler, orderedFeed, itemId);
      openStreamLightbox(orderedFeed, itemId, rect);
    },
    [orderedFeed, scheduler],
  );

  return (
    <div
      ref={rootRef}
      id="stream-masonry"
      className="relative pb-[calc(env(safe-area-inset-bottom,0px)+1rem)]"
      style={{
        height: layout.totalHeight,
        scrollMarginTop: "calc(var(--mobile-topbar-h) + var(--space-4))",
      }}
      aria-live="polite"
      data-virtual-total={layout.tiles.length}
      data-mounted-tiles={mountedTiles.length}
    >
      {layout.headers.map((header) => (
        <div
          key={header.key}
          className="absolute left-0 right-0 flex h-[42px] items-center gap-2.5 px-3 md:px-0"
          style={{ transform: `translateY(${header.top}px)` }}
        >
          <span className="flex h-6 items-center rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2.5 type-caption font-medium text-[var(--fg-1)] shadow-[var(--shadow-1)]">
            {header.label}
          </span>
          <span
            className="type-caption tabular-nums text-[var(--fg-2)]"
            aria-label={`${header.count} 张作品`}
          >
            {header.count} 张
          </span>
          <span className="h-px flex-1 bg-gradient-to-r from-[var(--border-subtle)] to-transparent" />
        </div>
      ))}

      {mountedTiles.map((tile) => {
        const item = tile.item;
        const highlighted = Boolean(
          highlightId &&
            (item.id === highlightId || item.image.id === highlightId),
        );
        return (
          <div
            key={item.id}
            data-mounted-tile
            data-highlighted={highlighted ? "true" : undefined}
            className={[
              "stream-tile-shell absolute top-0 animate-stream-tile-in",
              highlighted
                ? "ring-2 ring-[var(--accent)] ring-offset-2 ring-offset-[var(--bg-0)]"
                : "",
            ].join(" ")}
            style={{
              width: tile.width,
              minHeight: tile.height,
              transform: `translate3d(${tile.left}px, ${tile.top}px, 0)`,
              animationDelay: `${Math.min(tile.itemIndex * 18, 240)}ms`,
            }}
          >
            <GenerationTile
              item={item}
              onOpen={onOpenItem}
              selectionMode={selectionMode}
              selected={Boolean(selectedIds?.has(item.image.id))}
              onToggleSelect={onToggleSelect}
              prewarmScheduler={scheduler}
              fetchPriority={tile.itemIndex < 12 ? "high" : "low"}
              imageSizes={imageSizes}
            />
          </div>
        );
      })}
    </div>
  );
}

export const GenerationMasonry = memo(GenerationMasonryComponent);
