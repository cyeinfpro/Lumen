"use client";

import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Clock3,
  Image as ImageIcon,
  Loader2,
  RefreshCw,
  Search,
  TimerReset,
  type LucideIcon,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export type EventKindFilter = "all" | "generation" | "completion";
export type TimeRangeFilter = "24h" | "7d" | "30d";
export type StatusFilter =
  | "all"
  | "queued"
  | "running"
  | "streaming"
  | "succeeded"
  | "failed"
  | "canceled";

export interface RequestEventModelStat {
  model: string;
  count: number;
  share: number;
}

export interface RequestEventSummary {
  active: number;
  failed: number;
  succeeded: number;
  images: number;
  latestAt: string | null;
  avgDurationMs: number | null;
}

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

const FILTERED_SEARCH_PLACEHOLDER =
  "搜索用户、模型、上游、请求 ID、错误或提示词";
const MAX_MODEL_STATS = 6;

function formatPercent(share: number): string {
  if (!Number.isFinite(share) || share <= 0) return "0%";
  const percent = share * 100;
  if (percent > 0 && percent < 1) return "<1%";
  return `${Math.round(percent)}%`;
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
    amber:
      "text-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)]/12 border-[var(--color-lumen-amber)]/20",
    emerald: "text-success bg-success-soft border-success-border",
    red: "text-danger bg-danger-soft border-danger-border",
    sky: "text-info bg-info-soft border-info-border",
  }[tone];

  return (
    <div className="min-w-0 rounded-[var(--radius-panel)] border border-[var(--border-subtle)] bg-white/[0.035] px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[11px] text-[var(--fg-2)]">{label}</span>
        <span
          className={cn(
            "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-[var(--radius-card)] border",
            toneClass,
          )}
        >
          <Icon className="h-3.5 w-3.5" />
        </span>
      </div>
      <div className="mt-1 font-mono text-lg font-semibold leading-tight tabular-nums text-[var(--fg-0)]">
        {value}
      </div>
    </div>
  );
}

function ModelStatBar({ stat }: { stat: RequestEventModelStat }) {
  const width = `${Math.max(2, Math.min(100, Math.round(stat.share * 100)))}%`;

  return (
    <div className="min-w-0 rounded-[var(--radius-panel)] border border-[var(--border-subtle)] bg-white/[0.03] px-3 py-2.5">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <span
          className="min-w-0 truncate font-mono text-xs text-[var(--fg-0)]"
          title={stat.model}
        >
          {stat.model}
        </span>
        <span className="shrink-0 font-mono text-xs tabular-nums text-[var(--fg-2)]">
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
      className="inline-flex shrink-0 items-center gap-0.5 rounded-[var(--radius-panel)] border border-[var(--border)] bg-white/[0.04] p-0.5 text-xs"
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
              "h-9 rounded-[var(--radius-card)] px-3 transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25",
              active
                ? "bg-white/10 text-[var(--fg-0)]"
                : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function RequestEventsHeader({
  rowsCount,
  filteredCount,
  latestLabel,
  averageDurationLabel,
  summary,
  kind,
  status,
  range,
  autoRefresh,
  search,
  hasActiveFilters,
  modelStats,
  modelStatsTotal,
  hasSearch,
  fetching,
  loading,
  onSearch,
  onClearSearch,
  onKindChange,
  onStatusChange,
  onRangeChange,
  onToggleAutoRefresh,
  onReset,
  onRefresh,
}: {
  rowsCount: number;
  filteredCount: number;
  latestLabel: string;
  averageDurationLabel: string;
  summary: RequestEventSummary;
  kind: EventKindFilter;
  status: StatusFilter;
  range: TimeRangeFilter;
  autoRefresh: boolean;
  search: string;
  hasActiveFilters: boolean;
  modelStats: RequestEventModelStat[];
  modelStatsTotal: number;
  hasSearch: boolean;
  fetching: boolean;
  loading: boolean;
  onSearch: (value: string) => void;
  onClearSearch: () => void;
  onKindChange: (value: EventKindFilter) => void;
  onStatusChange: (value: StatusFilter) => void;
  onRangeChange: (value: TimeRangeFilter) => void;
  onToggleAutoRefresh: () => void;
  onReset: () => void;
  onRefresh: () => void;
}) {
  return (
    <div className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/70 p-4 backdrop-blur-sm md:p-5">
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-start">
        <div className="min-w-0">
          <h2
            id="request-events-title"
            className="text-lg font-semibold tracking-tight text-[var(--fg-0)]"
          >
            请求事件
          </h2>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--fg-2)]">
            <span className="inline-flex items-center gap-1.5">
              <Clock3 className="h-3.5 w-3.5" />
              最新 {latestLabel}
            </span>
            <span className="font-mono tabular-nums">
              拉取 {rowsCount} · 显示 {filteredCount}
            </span>
            <span className="font-mono tabular-nums">
              平均 {averageDurationLabel}
            </span>
            {fetching && !loading && (
              <span className="inline-flex items-center gap-1.5 text-[var(--fg-1)]">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                同步中
              </span>
            )}
            {autoRefresh && (
              <span className="inline-flex items-center gap-1.5 text-[var(--fg-2)]">
                <TimerReset className="h-3.5 w-3.5" />
                自动刷新 10s
              </span>
            )}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:w-[520px]">
          <StatTile icon={Activity} label="进行中" value={summary.active} tone="sky" />
          <StatTile icon={CheckCircle2} label="成功" value={summary.succeeded} tone="emerald" />
          <StatTile icon={AlertTriangle} label="失败" value={summary.failed} tone="red" />
          <StatTile icon={ImageIcon} label="图片" value={summary.images} tone="amber" />
        </div>
      </div>

      <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(240px,1fr)_auto] lg:items-center">
        <div className="flex h-10 min-w-0 items-center gap-2 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-0)]/70 px-3 transition-colors focus-within:border-[var(--color-lumen-amber)]/50 focus-within:ring-2 focus-within:ring-[var(--color-lumen-amber)]/25">
          <Search className="h-3.5 w-3.5 shrink-0 text-[var(--fg-2)]" />
          <label htmlFor="search-request-events" className="sr-only">
            搜索请求事件
          </label>
          <input
            id="search-request-events"
            type="search"
            value={search}
            onChange={(event) => onSearch(event.target.value)}
            placeholder={FILTERED_SEARCH_PLACEHOLDER}
            className="min-w-0 flex-1 bg-transparent text-sm text-[var(--fg-0)] placeholder:text-[var(--fg-2)] focus:outline-none"
          />
          {search.trim() && (
            <button
              type="button"
              onClick={onClearSearch}
              className="inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-card)] text-[var(--fg-2)] transition-colors hover:bg-white/10 hover:text-[var(--fg-0)] focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
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
            onChange={onKindChange}
          />
          <label htmlFor="request-event-status" className="sr-only">
            请求状态
          </label>
          <select
            id="request-event-status"
            value={status}
            onChange={(event) => onStatusChange(event.target.value as StatusFilter)}
            className="h-10 min-w-28 rounded-[var(--radius-panel)] border border-[var(--border)] bg-white/[0.04] px-3 text-xs text-[var(--fg-0)] transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
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
            onChange={(event) => onRangeChange(event.target.value as TimeRangeFilter)}
            className="h-10 min-w-24 rounded-[var(--radius-panel)] border border-[var(--border)] bg-white/[0.04] px-3 text-xs text-[var(--fg-0)] transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25"
          >
            {RANGE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <Button
            variant={autoRefresh ? "primary" : "secondary"}
            size="sm"
            onClick={onToggleAutoRefresh}
            aria-pressed={autoRefresh}
            leftIcon={<TimerReset className="h-3.5 w-3.5" />}
          >
            自动刷新
          </Button>
          {hasActiveFilters && (
            <Button
              variant="secondary"
              size="sm"
              onClick={onReset}
              leftIcon={<X className="h-3.5 w-3.5" />}
            >
              重置
            </Button>
          )}
          <Button
            variant="secondary"
            size="sm"
            onClick={onRefresh}
            disabled={fetching}
            loading={fetching}
            leftIcon={!fetching ? <RefreshCw className="h-3.5 w-3.5" /> : undefined}
          >
            刷新
          </Button>
        </div>
      </div>

      {modelStats.length > 0 && (
        <div className="mt-4 border-t border-[var(--border-subtle)] pt-4">
          <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="inline-flex items-center gap-1.5 font-medium text-[var(--fg-1)]">
              <BarChart3 className="h-3.5 w-3.5 text-[var(--color-lumen-amber)]" />
              路径统计
            </span>
            <span className="font-mono tabular-nums text-[var(--fg-2)]">
              {hasSearch ? "基于当前显示" : "基于当前筛选"}{" "}
              {hasSearch ? filteredCount : modelStatsTotal}
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
  );
}
