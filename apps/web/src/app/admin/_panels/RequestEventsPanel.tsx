"use client";

import { Fragment, useDeferredValue, useMemo, useState } from "react";
import { format } from "date-fns";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock3,
  Eye,
  Image as ImageIcon,
  Loader2,
  RefreshCw,
  Search,
  TimerReset,
  type LucideIcon,
  X,
} from "lucide-react";

import { useAdminRequestEventsQuery } from "@/lib/queries";
import type {
  AdminRequestEventImageOut,
  AdminRequestEventLiveLane,
  AdminRequestEventOut,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  OPEN_EVENT,
  type LightboxItem,
} from "@/components/ui/lightbox/types";
import { EmptyBlock, ErrorBlock, ListSkeleton } from "../page";

type EventKindFilter = "all" | "generation" | "completion";
type TimeRangeFilter = "24h" | "7d" | "30d";
type StatusFilter =
  | "all"
  | "queued"
  | "running"
  | "streaming"
  | "succeeded"
  | "failed"
  | "canceled";

const KIND_OPTIONS: Array<{ value: EventKindFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "generation", label: "图片" },
  { value: "completion", label: "对话" },
];

const STATUS_OPTIONS: Array<{ value: StatusFilter; label: string }> = [
  { value: "all", label: "全部状态" },
  { value: "queued", label: "排队" },
  { value: "running", label: "生成中" },
  { value: "streaming", label: "回复中" },
  { value: "succeeded", label: "成功" },
  { value: "failed", label: "失败" },
  { value: "canceled", label: "已取消" },
];

const RANGE_OPTIONS: Array<{ value: TimeRangeFilter; label: string }> = [
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
  { value: "30d", label: "30d" },
];

const EMPTY_REQUEST_EVENTS: AdminRequestEventOut[] = [];
const EMPTY_MODEL_STATS: RequestEventModelStat[] = [];
const FILTERED_SEARCH_PLACEHOLDER = "搜索用户、模型、上游、请求 ID、错误或提示词";
const MAX_MODEL_STATS = 6;

interface RequestEventModelStat {
  model: string;
  count: number;
  share: number;
}

const STATUS_META: Record<
  string,
  { label: string; badge: string; dot: string; row: string }
> = {
  queued: {
    label: "排队",
    badge: "bg-white/[0.06] text-neutral-300 border-white/10",
    dot: "bg-neutral-400",
    row: "border-l-white/10",
  },
  running: {
    label: "生成中",
    badge: "bg-sky-400/10 text-sky-200 border-sky-300/20",
    dot: "bg-sky-300",
    row: "border-l-sky-300/55",
  },
  streaming: {
    label: "回复中",
    badge: "bg-sky-400/10 text-sky-200 border-sky-300/20",
    dot: "bg-sky-300",
    row: "border-l-sky-300/55",
  },
  succeeded: {
    label: "成功",
    badge: "bg-emerald-400/10 text-emerald-200 border-emerald-300/20",
    dot: "bg-emerald-300",
    row: "border-l-emerald-300/55",
  },
  failed: {
    label: "失败",
    badge: "bg-red-400/10 text-red-200 border-red-300/25",
    dot: "bg-red-300",
    row: "border-l-red-300/55",
  },
  canceled: {
    label: "已取消",
    badge: "bg-neutral-400/10 text-neutral-300 border-white/10",
    dot: "bg-neutral-500",
    row: "border-l-neutral-500/50",
  },
};

function getStatusMeta(status: string) {
  return (
    STATUS_META[status] ?? {
      label: status || "未知",
      badge: "bg-white/[0.06] text-neutral-300 border-white/10",
      dot: "bg-neutral-500",
      row: "border-l-white/10",
    }
  );
}

function formatDateTime(value: string | null): string {
  if (!value) return "—";
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return format(date, "yyyy-MM-dd HH:mm:ss");
  } catch {
    return value;
  }
}

function formatAge(value: string | null): string {
  if (!value) return "时间未知";
  const created = new Date(value).getTime();
  if (!Number.isFinite(created)) return "时间未知";
  const diff = Math.max(0, Date.now() - created);
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return `${Math.floor(diff / 86_400_000)} 天前`;
}

function formatDuration(ms: number | null): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${rest}s`;
}

function formatPercent(share: number): string {
  if (!Number.isFinite(share) || share <= 0) return "0%";
  const percent = share * 100;
  if (percent > 0 && percent < 1) return "<1%";
  return `${Math.round(percent)}%`;
}

function eventKindLabel(event: AdminRequestEventOut): string {
  if (event.kind === "generation") {
    return event.action === "edit" ? "图生图" : "文生图";
  }
  return event.intent === "vision_qa" ? "视觉问答" : "对话";
}

function statusLabel(status: string): string {
  return getStatusMeta(status).label;
}

function imageRoleLabel(image: AdminRequestEventImageOut): string {
  if (image.roles.includes("output") && image.roles.includes("input")) {
    return "输入/输出";
  }
  if (image.roles.includes("output")) return "输出";
  return "参考";
}

function toLightboxItem(
  image: AdminRequestEventImageOut,
  event: AdminRequestEventOut,
): LightboxItem {
  const previewUrl = image.display_url || image.preview_url || image.url;
  const thumbUrl =
    image.thumb_url || image.preview_url || image.display_url || image.url;
  const width =
    Number.isFinite(image.width) && image.width > 0 ? image.width : undefined;
  const height =
    Number.isFinite(image.height) && image.height > 0
      ? image.height
      : undefined;
  return {
    id: image.id,
    url: image.url,
    previewUrl,
    thumbUrl,
    prompt: event.prompt || event.conversation_title || event.id,
    width,
    height,
    size_actual: width && height ? `${width}x${height}` : undefined,
    model: event.model || undefined,
    mime: image.mime || undefined,
    type: image.source,
    created_at: event.created_at,
    metadata: {
      role: imageRoleLabel(image),
      request_id: event.id,
      user: event.user_email,
      upstream: event.upstream_provider ?? undefined,
    },
  };
}

function lightboxItemsForEvent(event: AdminRequestEventOut): LightboxItem[] {
  const seen = new Set<string>();
  const items: LightboxItem[] = [];
  for (const image of event.images) {
    if (!image.id || !image.url) continue;
    const key = `${image.id}:${image.url}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push(toLightboxItem(image, event));
  }
  return items;
}

function openEventImages(event: AdminRequestEventOut, initialImageId?: string) {
  if (typeof window === "undefined" || event.images.length === 0) return;
  const items = lightboxItemsForEvent(event);
  if (items.length === 0) return;
  const initialId =
    initialImageId && items.some((item) => item.id === initialImageId)
      ? initialImageId
      : items[0].id;
  window.dispatchEvent(
    new CustomEvent(OPEN_EVENT, {
      detail: { items, initialId },
    }),
  );
}

function truncateMiddle(value: string, max = 16): string {
  if (value.length <= max) return value;
  const head = Math.ceil((max - 1) / 2);
  const tail = Math.floor((max - 1) / 2);
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

function liveLanes(event: AdminRequestEventOut): AdminRequestEventLiveLane[] {
  return event.live_lanes ?? [];
}

function matchesSearch(event: AdminRequestEventOut, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [
    event.id,
    event.message_id,
    event.conversation_id,
    event.user_email,
    event.model,
    event.upstream_provider,
    event.upstream_route,
    event.upstream_endpoint,
    event.conversation_title,
    event.prompt,
    event.error_code,
    event.error_message,
    eventKindLabel(event),
    statusLabel(event.status),
    event.live_provider,
    ...liveLanes(event).flatMap((lane) => [lane.provider, lane.last_failed]),
    ...event.images.map((image) => image.id),
  ]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(q));
}

function displayValue(value: string | null | undefined, fallback = "—"): string {
  const text = typeof value === "string" ? value.trim() : "";
  return text || fallback;
}

function isActiveStatus(status: string): boolean {
  return status === "queued" || status === "running" || status === "streaming";
}

function providerDisplayValue(event: AdminRequestEventOut): string {
  // 优先用 worker 实时写入 Redis 的 live_provider 快照——in-flight 期间能看到当前
  // 真在请求的 provider；dual_race 形如 "A vs B"；切号瞬间显示 "切换中"。
  if (isActiveStatus(event.status)) {
    const live = displayValue(event.live_provider ?? null, "");
    if (live) return live;
  }
  const provider = displayValue(event.upstream_provider, "");
  if (provider) return provider;
  if (event.upstream_route === "dual_race") {
    return isActiveStatus(event.status) ? "等待上游结果" : "历史未记录";
  }
  return isActiveStatus(event.status) ? "等待上游结果" : "未记录";
}

function formatUnknownValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string") return value || "—";
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function outputImageCount(event: AdminRequestEventOut): number {
  return event.images.filter((image) => image.roles.includes("output")).length;
}

function modelStatLabel(model: string): string {
  const normalized = model.trim();
  if (
    normalized === "5.4" ||
    normalized === "5.4 mini" ||
    normalized === "5.4mini" ||
    normalized === "gpt-5.4" ||
    normalized === "gpt-5.4-mini"
  ) {
    return "Codex 原生";
  }
  if (normalized === "image2" || normalized === "gpt-image-2") {
    return "image2 直连";
  }
  return normalized || "未记录";
}

function summarizeModelStats(
  events: AdminRequestEventOut[],
): RequestEventModelStat[] {
  const counts = new Map<string, number>();
  for (const event of events) {
    const model = modelStatLabel(displayValue(event.model, "未记录"));
    counts.set(model, (counts.get(model) ?? 0) + 1);
  }

  const total = events.length;
  if (total === 0) return [];

  return Array.from(counts.entries())
    .map(([model, count]) => ({
      model,
      count,
      share: count / total,
    }))
    .sort((a, b) => b.count - a.count || a.model.localeCompare(b.model));
}

function summarizeEvents(events: AdminRequestEventOut[]) {
  let active = 0;
  let failed = 0;
  let succeeded = 0;
  let images = 0;
  let latestMs = 0;
  let completedDurationTotal = 0;
  let completedDurationCount = 0;

  for (const event of events) {
    if (
      event.status === "running" ||
      event.status === "streaming" ||
      event.status === "queued"
    ) {
      active += 1;
    }
    if (event.status === "failed") failed += 1;
    if (event.status === "succeeded") succeeded += 1;
    images += event.images.length;

    const latestValue = event.finished_at ?? event.created_at;
    const latestEventMs = new Date(latestValue).getTime();
    if (Number.isFinite(latestEventMs)) {
      latestMs = Math.max(latestMs, latestEventMs);
    }
    if (
      event.duration_ms != null &&
      Number.isFinite(event.duration_ms) &&
      event.duration_ms >= 0 &&
      event.status === "succeeded"
    ) {
      completedDurationTotal += event.duration_ms;
      completedDurationCount += 1;
    }
  }

  return {
    active,
    failed,
    succeeded,
    images,
    latestAt: latestMs > 0 ? new Date(latestMs).toISOString() : null,
    avgDurationMs:
      completedDurationCount > 0
        ? Math.round(completedDurationTotal / completedDurationCount)
        : null,
  };
}

export function RequestEventsPanel() {
  const [kind, setKind] = useState<EventKindFilter>("all");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [range, setRange] = useState<TimeRangeFilter>("24h");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [search, setSearch] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const deferredSearch = useDeferredValue(search);

  const q = useAdminRequestEventsQuery(
    {
      limit: 120,
      kind,
      status: status === "all" ? undefined : status,
      range,
    },
    {
      refetchInterval: autoRefresh ? 10_000 : false,
    },
  );

  const rows = q.data?.items ?? EMPTY_REQUEST_EVENTS;
  const filtered = useMemo(
    () => rows.filter((event) => matchesSearch(event, deferredSearch)),
    [rows, deferredSearch],
  );
  const summary = useMemo(() => summarizeEvents(filtered), [filtered]);
  const fetchedSummary = useMemo(() => summarizeEvents(rows), [rows]);
  const hasSearch = deferredSearch.trim().length > 0;
  const modelStats = useMemo(
    () =>
      hasSearch
        ? summarizeModelStats(filtered)
        : q.data?.model_stats ?? EMPTY_MODEL_STATS,
    [filtered, hasSearch, q.data?.model_stats],
  );
  const modelStatsTotal = useMemo(
    () => modelStats.reduce((total, stat) => total + stat.count, 0),
    [modelStats],
  );
  const hasActiveFilters =
    kind !== "all" ||
    status !== "all" ||
    range !== "24h" ||
    search.trim().length > 0;
  const resetFilters = () => {
    setKind("all");
    setStatus("all");
    setRange("24h");
    setSearch("");
  };

  return (
    <section className="space-y-4" aria-labelledby="request-events-title">
      <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/70 p-4 backdrop-blur-sm md:p-5">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-start">
          <div className="min-w-0">
            <h2
              id="request-events-title"
              className="text-lg font-semibold tracking-tight text-neutral-100"
            >
              请求事件
            </h2>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-neutral-500">
              <span className="inline-flex items-center gap-1.5">
                <Clock3 className="h-3.5 w-3.5" />
                最新 {formatAge(fetchedSummary.latestAt)}
              </span>
              <span className="font-mono tabular-nums">
                拉取 {rows.length} · 显示 {filtered.length}
              </span>
              <span className="font-mono tabular-nums">
                平均 {formatDuration(summary.avgDurationMs)}
              </span>
              {q.isFetching && !q.isLoading && (
                <span className="inline-flex items-center gap-1.5 text-neutral-400">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  同步中
                </span>
              )}
              {autoRefresh && (
                <span className="inline-flex items-center gap-1.5 text-neutral-500">
                  <TimerReset className="h-3.5 w-3.5" />
                  自动刷新 10s
                </span>
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:w-[520px]">
            <StatTile
              icon={Activity}
              label="进行中"
              value={summary.active}
              tone="sky"
            />
            <StatTile
              icon={CheckCircle2}
              label="成功"
              value={summary.succeeded}
              tone="emerald"
            />
            <StatTile
              icon={AlertTriangle}
              label="失败"
              value={summary.failed}
              tone="red"
            />
            <StatTile
              icon={ImageIcon}
              label="图片"
              value={summary.images}
              tone="amber"
            />
          </div>
        </div>

        <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(240px,1fr)_auto] lg:items-center">
          <div className="flex h-10 min-w-0 items-center gap-2 rounded-xl border border-white/10 bg-[var(--bg-0)]/70 px-3 transition-colors focus-within:border-[var(--color-lumen-amber)]/50 focus-within:ring-2 focus-within:ring-[var(--color-lumen-amber)]/25">
            <Search className="h-3.5 w-3.5 shrink-0 text-neutral-500" />
            <label htmlFor="search-request-events" className="sr-only">
              搜索请求事件
            </label>
            <input
              id="search-request-events"
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder={FILTERED_SEARCH_PLACEHOLDER}
              className="min-w-0 flex-1 bg-transparent text-sm text-neutral-200 placeholder:text-neutral-600 focus:outline-none"
            />
            {search.trim() && (
              <button
                type="button"
                onClick={() => setSearch("")}
                className="inline-flex h-7 w-7 items-center justify-center rounded-lg text-neutral-500 transition-colors hover:bg-white/10 hover:text-neutral-200 focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
                aria-label="清空搜索"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>

          <div className="flex min-w-0 flex-wrap items-center gap-2 lg:justify-end">
            <SegmentedControl
              value={kind}
              options={KIND_OPTIONS}
              onChange={setKind}
            />
            <label htmlFor="request-event-status" className="sr-only">
              请求状态
            </label>
            <select
              id="request-event-status"
              value={status}
              onChange={(event) => setStatus(event.target.value as StatusFilter)}
              className="h-10 min-w-28 rounded-xl border border-white/10 bg-white/[0.04] px-3 text-xs text-neutral-200 transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
            >
              {STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <label htmlFor="request-event-range" className="sr-only">
              查询时间
            </label>
            <select
              id="request-event-range"
              value={range}
              onChange={(event) =>
                setRange(event.target.value as TimeRangeFilter)
              }
              className="h-10 min-w-24 rounded-xl border border-white/10 bg-white/[0.04] px-3 text-xs text-neutral-200 transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
            >
              {RANGE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => setAutoRefresh((value) => !value)}
              aria-pressed={autoRefresh}
              className={cn(
                "inline-flex min-h-11 items-center justify-center gap-1.5 rounded-xl border px-3 sm:h-10 sm:min-h-0 text-xs transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25",
                autoRefresh
                  ? "border-[var(--color-lumen-amber)]/30 bg-[var(--color-lumen-amber)]/12 text-[var(--color-lumen-amber)]"
                  : "border-white/10 bg-white/[0.04] text-neutral-300 hover:bg-white/[0.08] hover:text-neutral-100",
              )}
            >
              <TimerReset className="h-3.5 w-3.5" />
              自动刷新
            </button>
            {hasActiveFilters && (
              <button
                type="button"
                onClick={resetFilters}
                className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-xl border border-white/10 bg-white/[0.04] px-3 sm:h-10 sm:min-h-0 text-xs text-neutral-300 transition-colors hover:bg-white/[0.08] hover:text-neutral-100 focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
              >
                <X className="h-3.5 w-3.5" />
                重置
              </button>
            )}
            <button
              type="button"
              onClick={() => void q.refetch()}
              disabled={q.isFetching}
              className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-xl border border-white/10 bg-white/[0.04] px-3 sm:h-10 sm:min-h-0 text-xs text-neutral-300 transition-colors hover:bg-white/[0.08] hover:text-neutral-100 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
            >
              {q.isFetching ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <RefreshCw className="h-3.5 w-3.5" />
              )}
              刷新
            </button>
          </div>
        </div>

        {modelStats.length > 0 && (
          <div className="mt-4 border-t border-white/8 pt-4">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
              <span className="inline-flex items-center gap-1.5 font-medium text-neutral-300">
                <BarChart3 className="h-3.5 w-3.5 text-[var(--color-lumen-amber)]" />
                路径统计
              </span>
              <span className="font-mono tabular-nums text-neutral-500">
                {hasSearch ? "基于当前显示" : "基于当前筛选"}{" "}
                {hasSearch ? filtered.length : modelStatsTotal}
              </span>
            </div>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {modelStats.slice(0, MAX_MODEL_STATS).map((stat) => (
                <ModelStatBar key={stat.model} stat={stat} />
              ))}
            </div>
          </div>
        )}
      </div>

      <div
        className="overflow-hidden rounded-2xl border border-white/10 bg-[var(--bg-1)]/70 backdrop-blur-sm"
        aria-busy={q.isFetching}
        aria-live="polite"
      >
        {q.isLoading ? (
          <ListSkeleton rows={7} />
        ) : q.isError ? (
          <ErrorBlock
            message={q.error?.message ?? "未知错误"}
            onRetry={() => void q.refetch()}
          />
        ) : filtered.length === 0 ? (
          <EmptyBlock
            title={rows.length === 0 ? "暂无请求事件" : "没有匹配结果"}
            description={
              rows.length === 0
                ? "用户发起图片或对话请求后会出现在这里"
                : "试试切换过滤条件或换个关键词"
            }
          />
        ) : (
          <>
            <div className="hidden">
              <table className="w-full min-w-[1040px] text-sm">
                <thead className="sticky top-0 z-10 border-b border-white/10 bg-[var(--bg-1)]/95 text-xs uppercase tracking-wider text-[var(--fg-1)] backdrop-blur">
                  <tr>
                    <th className="w-9 py-3 px-3" />
                    <th className="text-left py-3 px-3 font-medium">结束时间</th>
                    <th className="text-left py-3 px-3 font-medium">事件</th>
                    <th className="text-left py-3 px-3 font-medium">模型</th>
                    <th className="text-left py-3 px-3 font-medium">上游</th>
                    <th className="text-left py-3 px-3 font-medium">用户</th>
                    <th className="text-left py-3 px-3 font-medium">图片</th>
                    <th className="text-right py-3 px-3 font-medium">耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((event) => {
                    const expanded = expandedId === event.id;
                    const detailId = `request-event-detail-${event.id}`;
                    const statusMeta = getStatusMeta(event.status);
                    return (
                      <Fragment key={event.id}>
                        <tr
                          className={cn(
                            "border-t border-l-2 border-white/5 align-top transition-colors hover:bg-white/[0.035]",
                            statusMeta.row,
                            expanded && "bg-white/[0.025]",
                          )}
                        >
                          <td className="py-3 px-3">
                            <button
                              type="button"
                              onClick={() =>
                                setExpandedId(expanded ? null : event.id)
                              }
                              className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-lg text-neutral-400 transition-colors hover:bg-white/10 hover:text-neutral-100 sm:h-8 sm:w-8 sm:min-h-0 sm:min-w-0 focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
                              aria-label={expanded ? "收起详情" : "展开详情"}
                              aria-expanded={expanded}
                              aria-controls={detailId}
                            >
                              {expanded ? (
                                <ChevronDown className="w-4 h-4" />
                              ) : (
                                <ChevronRight className="w-4 h-4" />
                              )}
                            </button>
                          </td>
                          <td className="py-3 px-3 font-mono text-xs tabular-nums whitespace-nowrap text-neutral-300">
                            {formatDateTime(event.finished_at)}
                          </td>
                          <td className="py-3 px-3">
                            <div className="flex flex-col gap-1">
                              <span className="text-neutral-100">
                                {eventKindLabel(event)}
                              </span>
                              <StatusBadge status={event.status} />
                            </div>
                          </td>
                          <td className="py-3 px-3 text-neutral-300 max-w-[220px]">
                            <span
                              className="line-clamp-2 break-words"
                              title={event.model}
                            >
                              {displayValue(event.model)}
                            </span>
                          </td>
                          <td className="py-3 px-3">
                            <ProviderCell event={event} />
                          </td>
                          <td className="py-3 px-3 text-neutral-300 max-w-[210px]">
                            <span
                              className="line-clamp-2 break-all"
                              title={event.user_email}
                            >
                              {displayValue(event.user_email)}
                            </span>
                          </td>
                          <td className="py-3 px-3">
                            <ImagesButton event={event} />
                          </td>
                          <td className="py-3 px-3 text-right font-mono text-xs tabular-nums text-neutral-300 whitespace-nowrap">
                            {formatDuration(event.duration_ms)}
                          </td>
                        </tr>
                        {expanded && (
                          <tr className="border-t border-white/5" id={detailId}>
                            <td colSpan={8} className="px-4 pb-5 pt-2">
                              <EventDetails event={event} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <ul className="divide-y divide-white/5">
              {filtered.map((event) => {
                const expanded = expandedId === event.id;
                const detailId = `request-event-mobile-detail-${event.id}`;
                const statusMeta = getStatusMeta(event.status);
                return (
                  <li
                    key={event.id}
                    className={cn(
                      "space-y-3 border-l-2 p-4 lg:grid lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start lg:gap-4 lg:space-y-0",
                      statusMeta.row,
                      expanded && "bg-white/[0.025]",
                    )}
                  >
                    <button
                      type="button"
                      onClick={() => setExpandedId(expanded ? null : event.id)}
                      className="w-full min-w-0 space-y-3 rounded-xl text-left focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
                      aria-expanded={expanded}
                      aria-controls={detailId}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-sm text-neutral-100">
                              {eventKindLabel(event)}
                            </span>
                            <StatusBadge status={event.status} />
                          </div>
                          <p className="mt-1 font-mono text-xs text-neutral-500 tabular-nums">
                            结束 {formatDateTime(event.finished_at)}
                          </p>
                        </div>
                        {expanded ? (
                          <ChevronDown className="w-4 h-4 text-neutral-500 shrink-0" />
                        ) : (
                          <ChevronRight className="w-4 h-4 text-neutral-500 shrink-0" />
                        )}
                      </div>
                      <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
                        <MiniField label="模型" value={displayValue(event.model)} />
                        <MiniField
                          label="上游"
                          value={providerDisplayValue(event)}
                        />
                        <MiniField label="用户" value={displayValue(event.user_email)} />
                        <MiniField
                          label="耗时"
                          value={formatDuration(event.duration_ms)}
                        />
                      </div>
                    </button>
                    <div className="lg:self-center lg:justify-self-end">
                      <ImagesButton event={event} />
                    </div>
                    {expanded && (
                      <div id={detailId} className="lg:col-span-2">
                        <EventDetails event={event} />
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </section>
  );
}

function StatTile({
  icon: Icon,
  label,
  value,
  tone,
}: {
  icon: LucideIcon;
  label: string;
  value: number;
  tone: "amber" | "emerald" | "red" | "sky";
}) {
  const toneClass = {
    amber: "text-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)]/12 border-[var(--color-lumen-amber)]/20",
    emerald: "text-emerald-200 bg-emerald-400/10 border-emerald-300/20",
    red: "text-red-200 bg-red-400/10 border-red-300/20",
    sky: "text-sky-200 bg-sky-400/10 border-sky-300/20",
  }[tone];

  return (
    <div className="min-w-0 rounded-xl border border-white/8 bg-white/[0.035] px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11px] text-neutral-500">{label}</span>
        <span
          className={cn(
            "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-lg border",
            toneClass,
          )}
        >
          <Icon className="h-3.5 w-3.5" />
        </span>
      </div>
      <div className="mt-1 font-mono text-lg font-semibold leading-tight tabular-nums text-neutral-100">
        {value}
      </div>
    </div>
  );
}

function ModelStatBar({ stat }: { stat: RequestEventModelStat }) {
  const width = `${Math.max(2, Math.min(100, Math.round(stat.share * 100)))}%`;

  return (
    <div className="min-w-0 rounded-xl border border-white/8 bg-white/[0.03] px-3 py-2.5">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <span
          className="min-w-0 truncate font-mono text-xs text-neutral-200"
          title={stat.model}
        >
          {stat.model}
        </span>
        <span className="shrink-0 font-mono text-xs tabular-nums text-neutral-500">
          {stat.count} · {formatPercent(stat.share)}
        </span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
        <div
          className="h-full rounded-full bg-[var(--color-lumen-amber)]"
          style={{ width }}
        />
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const meta = getStatusMeta(status);
  return (
    <span
      className={cn(
        "inline-flex w-fit items-center gap-1.5 rounded-md border px-1.5 py-0.5 text-[11px]",
        meta.badge,
      )}
    >
      <span className={cn("h-1.5 w-1.5 rounded-full", meta.dot)} />
      {meta.label}
    </span>
  );
}

function ProviderCell({ event }: { event: AdminRequestEventOut }) {
  const route = displayValue(event.upstream_route || event.upstream_endpoint);
  const lanes = liveLanes(event);
  const isLive = isActiveStatus(event.status);
  const hasLiveLaneDetail =
    isLive && lanes.length > 0 && lanes.some((lane) => lane.provider || lane.last_failed);

  if (hasLiveLaneDetail) {
    return (
      <div className="flex max-w-[220px] flex-col gap-1 text-xs">
        {lanes.map((lane, idx) => (
          <LiveLaneRow key={`${lane.label}-${idx}`} lane={lane} />
        ))}
        <span className="truncate text-neutral-500" title={route}>
          {route}
        </span>
      </div>
    );
  }

  const provider = providerDisplayValue(event);
  return (
    <div className="flex max-w-[190px] flex-col gap-1 text-xs">
      <span className="truncate text-neutral-200" title={provider}>
        {provider}
      </span>
      <span className="truncate text-neutral-500" title={route}>
        {route}
      </span>
    </div>
  );
}

function LiveLaneRow({ lane }: { lane: AdminRequestEventLiveLane }) {
  const isFailover = lane.status === "failover";
  const provider = lane.provider?.trim();
  const dotClass = isFailover
    ? "bg-amber-400 animate-pulse"
    : provider
      ? "bg-emerald-400 animate-pulse"
      : "bg-neutral-500";
  const labelText = lane.label || "lane";
  let providerText: string;
  if (provider) {
    providerText = provider;
  } else if (isFailover && lane.last_failed) {
    providerText = `切换中 (上一个 ${lane.last_failed})`;
  } else {
    providerText = "等待中";
  }
  const tip = [
    `${labelText}: ${providerText}`,
    lane.route ? `route=${lane.route}` : null,
    lane.endpoint ? `endpoint=${lane.endpoint}` : null,
  ]
    .filter(Boolean)
    .join(" • ");
  return (
    <span
      className="flex items-center gap-1.5 truncate text-neutral-200"
      title={tip}
    >
      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", dotClass)} />
      <span className="shrink-0 text-[10px] uppercase tracking-wide text-neutral-500">
        {labelText}
      </span>
      <span
        className={cn(
          "truncate",
          isFailover ? "text-amber-200" : "text-neutral-200",
        )}
      >
        {providerText}
      </span>
    </span>
  );
}

function SegmentedControl<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
}) {
  return (
    <div
      role="tablist"
      className="inline-flex shrink-0 items-center gap-0.5 rounded-xl border border-white/10 bg-white/[0.04] p-0.5 text-xs"
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(option.value)}
            className={cn(
              "h-9 rounded-lg px-3 transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25",
              active
                ? "bg-white/10 text-neutral-100"
                : "text-neutral-400 hover:text-neutral-100",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function ImagesButton({ event }: { event: AdminRequestEventOut }) {
  if (event.images.length === 0) {
    return <span className="text-xs text-neutral-600">—</span>;
  }
  const outputCount = outputImageCount(event);
  const canOpen = lightboxItemsForEvent(event).length > 0;
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        openEventImages(event);
      }}
      disabled={!canOpen}
      className="inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-2.5 text-xs text-neutral-200 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
      aria-label={`查看 ${event.images.length} 张事件图片`}
    >
      <Eye className="w-3.5 h-3.5" />
      查看图片
      <span className="rounded bg-white/10 px-1.5 py-0.5 font-mono text-[10px] text-neutral-300">
        {event.images.length}
      </span>
      {outputCount > 0 && (
        <span className="rounded bg-[var(--color-lumen-amber)]/15 px-1.5 py-0.5 text-[10px] text-[var(--color-lumen-amber)]">
          输出 {outputCount}
        </span>
      )}
    </button>
  );
}

function EventDetails({ event }: { event: AdminRequestEventOut }) {
  const upstreamEntries = Object.entries(event.upstream ?? {});
  const conversationLabel = event.conversation_title
    ? `${event.conversation_title} (${truncateMiddle(event.conversation_id ?? "")})`
    : displayValue(event.conversation_id);
  const outputCount = outputImageCount(event);

  return (
    <div className="space-y-4 rounded-xl border border-white/10 bg-black/20 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-white/8 pb-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-neutral-100">
            {eventKindLabel(event)}
          </span>
          <StatusBadge status={event.status} />
          {outputCount > 0 && (
            <span className="inline-flex items-center gap-1 rounded-md border border-[var(--color-lumen-amber)]/20 bg-[var(--color-lumen-amber)]/10 px-1.5 py-0.5 text-[11px] text-[var(--color-lumen-amber)]">
              <ImageIcon className="h-3 w-3" />
              输出 {outputCount}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-neutral-500">
          <span className="font-mono tabular-nums">
            {formatDuration(event.duration_ms)}
          </span>
          <span className="font-mono tabular-nums">
            {formatAge(event.finished_at ?? event.created_at)}
          </span>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <Detail label="请求 ID" value={event.id} mono />
        <Detail label="消息 ID" value={event.message_id} mono />
        <Detail label="会话" value={conversationLabel} />
        <Detail label="阶段" value={displayValue(event.progress_stage)} />
        <Detail label="创建时间" value={formatDateTime(event.created_at)} mono />
        <Detail label="开始时间" value={formatDateTime(event.started_at)} mono />
        <Detail label="结束时间" value={formatDateTime(event.finished_at)} mono />
        <Detail
          label="上游端点"
          value={displayValue(event.upstream_endpoint)}
        />
        <Detail
          label="上游路由"
          value={displayValue(event.upstream_route, "未记录")}
        />
        <Detail
          label="上游"
          value={providerDisplayValue(event)}
        />
        <Detail label="尝试次数" value={String(event.attempt)} mono />
        {event.tokens_in != null && (
          <Detail label="输入 tokens" value={String(event.tokens_in)} mono />
        )}
        {event.tokens_out != null && (
          <Detail label="输出 tokens" value={String(event.tokens_out)} mono />
        )}
        {event.error_code && (
          <Detail label="错误码" value={event.error_code} />
        )}
      </div>

      {isActiveStatus(event.status) && liveLanes(event).length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-neutral-500">
            <Activity className="w-3.5 h-3.5" />
            实时 provider（worker 心跳）
          </div>
          <div className="flex flex-col gap-1.5 rounded-lg border border-white/8 bg-white/[0.03] p-3">
            {liveLanes(event).map((lane, idx) => (
              <LiveLaneRow key={`detail-${lane.label}-${idx}`} lane={lane} />
            ))}
          </div>
        </div>
      )}

      {event.prompt && (
        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-wider text-neutral-500">
            提示词
          </div>
          <p className="max-h-32 overflow-auto whitespace-pre-wrap rounded-lg border border-white/8 bg-white/[0.03] p-3 text-xs leading-relaxed text-neutral-300">
            {event.prompt}
          </p>
        </div>
      )}

      {event.error_message && (
        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-wider text-red-300/80">
            错误信息
          </div>
          <p className="max-h-28 overflow-auto whitespace-pre-wrap rounded-lg border border-red-400/20 bg-red-500/5 p-3 text-xs leading-relaxed text-red-100">
            {event.error_message}
          </p>
        </div>
      )}

      {event.images.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-2 text-[11px] uppercase tracking-wider text-neutral-500">
            <ImageIcon className="w-3.5 h-3.5" />
            图片文件
          </div>
          <div className="flex flex-wrap gap-2">
            {event.images.map((image, index) => (
              <button
                key={`${image.id}:${index}`}
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  openEventImages(event, image.id);
                }}
                disabled={!image.url}
                className="inline-flex min-h-[36px] max-w-full items-center gap-2 rounded-lg border border-white/10 bg-white/[0.04] px-2.5 py-2 text-xs text-neutral-200 transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
              >
                <ImageIcon className="w-3.5 h-3.5 text-neutral-400" />
                <span className="shrink-0">{imageRoleLabel(image)}</span>
                <span className="truncate font-mono text-neutral-500">
                  {image.width > 0 && image.height > 0
                    ? `${image.width}x${image.height}`
                    : "尺寸未知"}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}

      {upstreamEntries.length > 0 && (
        <div>
          <div className="mb-2 text-[11px] uppercase tracking-wider text-neutral-500">
            上游参数
          </div>
          <div className="flex flex-wrap gap-2">
            {upstreamEntries.map(([key, value]) => (
              <span
                key={key}
                title={formatUnknownValue(value)}
                className="inline-flex max-w-full items-center gap-1 rounded-lg border border-white/8 bg-white/[0.03] px-2 py-1 text-xs text-neutral-300"
              >
                <span className="shrink-0 text-neutral-500">{key}</span>
                <span className="truncate font-mono text-neutral-300">
                  {formatUnknownValue(value)}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Detail({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] uppercase tracking-wider text-neutral-500">
        {label}
      </div>
      <div
        className={cn(
          "mt-1 break-words text-xs text-neutral-300",
          mono && "font-mono tabular-nums",
        )}
        title={value || "—"}
      >
        {value || "—"}
      </div>
    </div>
  );
}

function MiniField({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-white/5 bg-white/[0.03] px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-neutral-500">
        {label}
      </div>
      <div
        className="mt-0.5 line-clamp-2 break-words text-xs text-neutral-300"
        title={value || "—"}
      >
        {value || "—"}
      </div>
    </div>
  );
}
