// Phase 4 · 图库 Tab 数据层
// 对接后端 GET /api/generations/feed（apps/api/app/routes/generations.py）。
//
// 为什么用 useInfiniteQuery 而不是手写分页：
// - 后端约定 cursor 翻页 + total 总数；下拉刷新需要 refetch 语义
// - key 中带 filters，切换 ratio / has_ref / q 时走独立缓存
// - React 19 规则：不在 effect 里 setState 累加页数；让 TanStack 自管

"use client";

import {
  type InfiniteData,
  useInfiniteQuery,
} from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api/http";
import type {
  GenerationSummary,
  StreamFeedFilters,
  StreamFeedPage,
} from "../model/contracts";

export type {
  GenerationSummary,
  StreamFeedFilters,
  StreamFeedPage,
} from "../model/contracts";

// ---------- helpers ----------

export function normalizeStreamSearchQuery(
  value: string | null | undefined,
): string | null {
  const normalized = value?.trim().replace(/\s+/g, " ");
  return normalized ? normalized : null;
}

export function buildStreamFeedQuery(
  filters: StreamFeedFilters,
  limit: number,
  cursor: string | undefined,
): string {
  const p = new URLSearchParams();
  p.set("limit", String(limit));
  if (cursor) p.set("cursor", cursor);
  if (filters.ratio) p.set("ratio", filters.ratio);
  if (filters.has_ref) p.set("has_ref", "1");
  const q = normalizeStreamSearchQuery(filters.q);
  if (q) p.set("q", q);
  return p.toString();
}

// queryKey 规范化：把 undefined / false / "" 归一，避免缓存碎片。
export function normalizeStreamFeedFilters(filters: StreamFeedFilters) {
  return {
    ratio: filters.ratio ?? null,
    has_ref: Boolean(filters.has_ref),
    q: normalizeStreamSearchQuery(filters.q),
  };
}

export function useDebouncedStreamSearch(
  value: string,
  delayMs = 300,
): string | null {
  const normalized = normalizeStreamSearchQuery(value);
  const [debounced, setDebounced] = useState<string | null>(normalized);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(normalized), delayMs);
    return () => window.clearTimeout(timer);
  }, [delayMs, normalized]);
  return debounced;
}

// ---------- hook ----------

export function useStreamFeedQuery(
  filters: StreamFeedFilters,
  limit = 30,
) {
  const key = normalizeStreamFeedFilters(filters);
  return useInfiniteQuery<
    StreamFeedPage,
    Error,
    InfiniteData<StreamFeedPage, string | undefined>,
    readonly ["stream", "feed", typeof key, number],
    string | undefined
  >({
    queryKey: ["stream", "feed", key, limit] as const,
    queryFn: ({ pageParam, signal }) => {
      const qs = buildStreamFeedQuery(
        {
          ratio: filters.ratio,
          has_ref: filters.has_ref,
          q: filters.q,
        },
        limit,
        pageParam,
      );
      return apiFetch<StreamFeedPage>(`/generations/feed?${qs}`, { signal });
    },
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    staleTime: 20_000,
    gcTime: 5 * 60_000,
  });
}

// 工具：把 pages 摊平成 items 数组。
export function flattenFeed(
  data: InfiniteData<StreamFeedPage, string | undefined> | undefined,
): GenerationSummary[] {
  if (!data) return [];
  const out: GenerationSummary[] = [];
  for (const p of data.pages) {
    for (const it of p.items) out.push(it);
  }
  return out;
}

export function feedTotal(
  data: InfiniteData<StreamFeedPage, string | undefined> | undefined,
): number {
  if (!data || data.pages.length === 0) return 0;
  // 取最后一页的 total（后端每页返回同一总数语义）
  return data.pages[data.pages.length - 1]?.total ?? 0;
}
