"use client";

import { memo, useCallback, useEffect, useMemo, useRef } from "react";
import { isThisWeek, isToday, isYesterday } from "date-fns";
import { GenerationTile } from "./GenerationTile";
import type { GenerationSummary } from "@/lib/queries/stream";
import { openStreamLightbox } from "./lightbox";

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
type MasonryEntry = {
  item: GenerationSummary;
  index: number;
  estimatedHeight: number;
};

const BUCKET_LABEL: Record<Bucket, string> = {
  today: "今天",
  yesterday: "昨天",
  week: "本周",
  older: "更早",
};

const BUCKET_ORDER: Bucket[] = ["today", "yesterday", "week", "older"];

function bucketOf(iso: string): Bucket {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "older";
  if (isToday(d)) return "today";
  if (isYesterday(d)) return "yesterday";
  if (isThisWeek(d, { weekStartsOn: 1 })) return "week";
  return "older";
}

function compareFeedOrder(a: GenerationSummary, b: GenerationSummary): number {
  const aTs = new Date(a.created_at).getTime();
  const bTs = new Date(b.created_at).getTime();
  const aValid = !isNaN(aTs);
  const bValid = !isNaN(bTs);
  if (aValid && bValid && aTs !== bTs) return bTs - aTs;
  if (aValid !== bValid) return aValid ? -1 : 1;
  return b.id.localeCompare(a.id);
}

function estimateTileHeight(item: GenerationSummary): number {
  const w = Math.max(1, item.image.width || 1);
  const h = Math.max(1, item.image.height || 1);
  const mediaRatio = h / w;
  const promptRows = item.prompt.length > 34 ? 2 : 1;
  return mediaRatio * 1000 + 72 + promptRows * 18;
}

function distributeByEstimatedHeight(
  arr: GenerationSummary[],
  columnCount: number,
): MasonryEntry[][] {
  const cols = Array.from(
    { length: columnCount },
    () => [] as MasonryEntry[],
  );
  const heights = Array.from({ length: columnCount }, () => 0);
  arr.forEach((item, index) => {
    const estimatedHeight = estimateTileHeight(item);
    let target = 0;
    for (let i = 1; i < columnCount; i += 1) {
      if (heights[i] < heights[target]) target = i;
    }
    cols[target].push({ item, index, estimatedHeight });
    heights[target] += estimatedHeight;
  });
  return cols;
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
  const orderedFeed = useMemo(
    () => feed.slice().sort(compareFeedOrder),
    [feed],
  );
  const lightboxItemsRef = useRef(orderedFeed);
  const tileRefs = useRef(new Map<string, HTMLDivElement>());

  useEffect(() => {
    lightboxItemsRef.current = orderedFeed;
  }, [orderedFeed]);

  useEffect(() => {
    const target = highlightId?.trim();
    if (!target) return;
    const node = tileRefs.current.get(target);
    if (!node) return;
    const timer = window.setTimeout(() => {
      node.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [highlightId, items]);

  const onOpenItem = useCallback((itemId: string, rect: DOMRect) => {
    openStreamLightbox(lightboxItemsRef.current, itemId, rect);
  }, []);

  const grouped = useMemo(() => {
    const map = new Map<Bucket, GenerationSummary[]>();
    for (const it of items) {
      const b = bucketOf(it.created_at);
      const arr = map.get(b) ?? [];
      arr.push(it);
      map.set(b, arr);
    }
    for (const [bucket, arr] of map) {
      map.set(bucket, arr.slice().sort(compareFeedOrder));
    }
    return map;
  }, [items]);
  const groupedColumns = useMemo(() => {
    const map = new Map<Bucket, MasonryEntry[][]>();
    for (const bucket of BUCKET_ORDER) {
      const bucketItems = grouped.get(bucket);
      if (bucketItems?.length) {
        map.set(
          bucket,
          distributeByEstimatedHeight(bucketItems, columnCount),
        );
      }
    }
    return map;
  }, [columnCount, grouped]);

  return (
    <div
      id="stream-masonry"
      className="pb-[calc(env(safe-area-inset-bottom,0px)+1rem)]"
      style={{
        scrollMarginTop: "calc(var(--mobile-topbar-h) + var(--space-4))",
      }}
      aria-live="polite"
    >
      {BUCKET_ORDER.map((b) => {
        const arr = grouped.get(b);
        if (!arr || arr.length === 0) return null;
        const masonryColumns = groupedColumns.get(b) ?? [];
        return (
          <section key={b} aria-label={BUCKET_LABEL[b]} className="pt-6 first:pt-3 md:pt-7 md:first:pt-4">
            <div className="mb-2.5 flex items-center gap-2.5 px-3 md:mb-3.5 md:px-0">
              <span className="flex h-6 items-center rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2.5 text-[11px] font-medium text-[var(--fg-1)] shadow-[var(--shadow-1)]">
                {BUCKET_LABEL[b]}
              </span>
              <span className="text-[11px] tabular-nums text-[var(--fg-2)]" aria-label={`${arr.length} 张作品`}>
                {arr.length} 张
              </span>
              <span className="h-px flex-1 bg-gradient-to-r from-[var(--border-subtle)] to-transparent" />
            </div>
            <div
              className="grid px-2 md:px-0"
              style={{
                gridTemplateColumns: `repeat(${columnCount}, minmax(0, 1fr))`,
                gap,
              }}
            >
              {masonryColumns.map((col, colIndex) => (
                <div
                  key={`${b}-${colIndex}`}
                  className="flex min-w-0 flex-col"
                  style={{ gap }}
                >
                  {col.map(({ item, index, estimatedHeight }) => {
                    const highlighted = Boolean(
                      highlightId &&
                        (item.id === highlightId || item.image.id === highlightId),
                    );
                    return (
                      <div
                        key={item.id}
                        ref={(node) => {
                          const keys = [item.id, item.image.id];
                          for (const key of keys) {
                            if (node) tileRefs.current.set(key, node);
                            else tileRefs.current.delete(key);
                          }
                        }}
                        data-highlighted={highlighted ? "true" : undefined}
                        className={[
                          "stream-tile-shell animate-stream-tile-in",
                          highlighted
                            ? "ring-2 ring-[var(--accent)] ring-offset-2 ring-offset-[var(--bg-0)]"
                            : "",
                        ].join(" ")}
                        style={{
                          animationDelay: `${Math.min(index * 26, 360)}ms`,
                          contentVisibility: "auto",
                          containIntrinsicSize: `1px ${Math.max(240, Math.min(760, estimatedHeight / 2))}px`,
                        }}
                      >
                        <GenerationTile
                          item={item}
                          onOpen={onOpenItem}
                          selectionMode={selectionMode}
                          selected={Boolean(selectedIds?.has(item.image.id))}
                          onToggleSelect={onToggleSelect}
                        />
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

export const GenerationMasonry = memo(GenerationMasonryComponent);
