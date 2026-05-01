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
}: GenerationMasonryProps) {
  const columnCount = Math.max(1, Math.floor(columns));
  const gap = columnCount > 2 ? 16 : 12;
  const orderedFeed = useMemo(
    () => feed.slice().sort(compareFeedOrder),
    [feed],
  );
  const lightboxItemsRef = useRef(orderedFeed);

  useEffect(() => {
    lightboxItemsRef.current = orderedFeed;
  }, [orderedFeed]);

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

  return (
    <div
      id="stream-masonry"
      className="pb-3"
      style={{ scrollMarginTop: 72 }}
      aria-live="polite"
    >
      {BUCKET_ORDER.map((b) => {
        const arr = grouped.get(b);
        if (!arr || arr.length === 0) return null;
        const masonryColumns = distributeByEstimatedHeight(arr, columnCount);
        return (
          <section key={b} aria-label={BUCKET_LABEL[b]} className="pt-7 first:pt-4">
            <div className="mb-3.5 flex items-center gap-3 px-3 md:px-0">
              <span className="flex h-6 items-center rounded-md border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2.5 text-[11px] font-medium text-[var(--fg-1)] shadow-[var(--shadow-1)]">
                {BUCKET_LABEL[b]}
              </span>
              <span className="text-[11px] tabular-nums text-[var(--fg-2)]" aria-label={`${arr.length} 张作品`}>
                {arr.length} 张
              </span>
              <span className="h-px flex-1 bg-gradient-to-r from-[var(--border-subtle)] to-transparent" />
            </div>
            <div
              className="grid px-3 md:px-0"
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
                  {col.map(({ item, index, estimatedHeight }) => (
                    <div
                      key={item.id}
                      className="stream-tile-shell animate-stream-tile-in"
                      style={{
                        animationDelay: `${Math.min(index * 26, 360)}ms`,
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
                  ))}
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
