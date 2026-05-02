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
import { apiFetch } from "@/lib/apiClient";

// ---------- types ----------

export interface GenerationSummary {
  id: string;
  created_at: string;
  prompt: string;
  aspect_ratio: string;
  has_ref: boolean;
  fast: boolean;
  quality?: string | null;
  output_format?: string | null;
  size_actual: string;
  image: {
    id: string;
    url: string;
    mime?: string;
    display_url?: string;
    thumb_url: string;
    width: number;
    height: number;
  };
  message_id: string;
  conversation_id: string;
}

export interface StreamFeedFilters {
  ratio?: string;
  has_ref?: boolean;
  fast?: boolean;
  q?: string;
}

export interface StreamFeedPage {
  items: GenerationSummary[];
  next_cursor?: string | null;
  total: number;
}

// ---------- helpers ----------

function buildQuery(
  filters: StreamFeedFilters,
  limit: number,
  cursor: string | undefined,
): string {
  const p = new URLSearchParams();
  p.set("limit", String(limit));
  if (cursor) p.set("cursor", cursor);
  if (filters.ratio) p.set("ratio", filters.ratio);
  if (filters.has_ref) p.set("has_ref", "1");
  if (filters.fast) p.set("fast", "1");
  if (filters.q && filters.q.trim()) p.set("q", filters.q.trim());
  return p.toString();
}

// queryKey 规范化：把 undefined / false / "" 归一，避免缓存碎片。
function normalizeFilters(filters: StreamFeedFilters) {
  return {
    ratio: filters.ratio ?? null,
    has_ref: Boolean(filters.has_ref),
    fast: Boolean(filters.fast),
    // 注意：客户端搜索按 spec §6.9 做——不把 q 发给后端做分页。
    // 这里 q 也不进 queryKey，避免每次打字都 refetch。
  };
}

// ---------- hook ----------

export function useStreamFeedQuery(
  filters: StreamFeedFilters,
  limit = 30,
) {
  const key = normalizeFilters(filters);
  return useInfiniteQuery<
    StreamFeedPage,
    Error,
    InfiniteData<StreamFeedPage, string | undefined>,
    readonly ["stream", "feed", typeof key, number],
    string | undefined
  >({
    queryKey: ["stream", "feed", key, limit] as const,
    queryFn: ({ pageParam }) => {
      // 注意：这里不把 filters.q 传后端，按 spec 是纯客户端过滤。
      const qs = buildQuery(
        { ratio: filters.ratio, has_ref: filters.has_ref, fast: filters.fast },
        limit,
        pageParam,
      );
      return apiFetch<StreamFeedPage>(`/generations/feed?${qs}`);
    },
    initialPageParam: undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
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
