"use client";

import {
  Eraser,
  Gauge,
  CheckSquare,
  Image as ImageIcon,
  Layers3,
  RefreshCw,
  Search,
  Share2,
  WandSparkles,
  X,
} from "lucide-react";
import { useRouter } from "next/navigation";

import type { StreamFeedFilters } from "@/lib/queries/stream";
import { cn } from "@/lib/utils";

export interface StreamOverviewProps {
  total: number;
  loaded: number;
  visible: number;
  promptCount: number;
  filters: StreamFeedFilters;
  searchValue: string;
  refreshing?: boolean;
  selectionMode?: boolean;
  selectedCount?: number;
  sharingSelected?: boolean;
  onRefresh: () => void;
  onClearFilters: () => void;
  onToggleReferenceFilter: () => void;
  onToggleFastFilter: () => void;
  onToggleSelectionMode?: () => void;
  onClearSelection?: () => void;
  onShareSelected?: () => void;
}

export function StreamOverview({
  total,
  loaded,
  visible,
  promptCount,
  filters,
  searchValue,
  refreshing = false,
  selectionMode = false,
  selectedCount = 0,
  sharingSelected = false,
  onRefresh,
  onClearFilters,
  onToggleReferenceFilter,
  onToggleFastFilter,
  onToggleSelectionMode,
  onClearSelection,
  onShareSelected,
}: StreamOverviewProps) {
  const router = useRouter();
  const hasFilter = Boolean(filters.ratio || filters.has_ref || filters.fast);
  const hasSearch = searchValue.trim().length > 0;
  const hasControls = hasFilter || hasSearch;
  const visibleLabel = hasControls ? `${visible}/${loaded}` : `${loaded}`;

  return (
    <section
      aria-label="图库概览"
      className="border-b border-[var(--border-subtle)] px-3 py-3 md:px-0 md:py-4"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[12px]">
          <span className="inline-flex items-center gap-1.5 text-[var(--fg-1)]">
            <ImageIcon className="h-3.5 w-3.5 text-[var(--amber-300)]" />
            <span className="tabular-nums">{visibleLabel} 张</span>
          </span>
          <span className="inline-flex items-center gap-1.5 text-[var(--fg-2)]">
            <Layers3 className="h-3.5 w-3.5" />
            <span className="tabular-nums">{promptCount} prompt</span>
          </span>
          {total > loaded && (
            <span className="text-[11px] tabular-nums text-[var(--fg-2)]">
              共 {total}，继续下滑加载
            </span>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {selectedCount > 0 ? (
            <>
              <button
                type="button"
                onClick={onShareSelected}
                disabled={sharingSelected}
                className="inline-flex min-h-11 cursor-pointer items-center gap-1.5 rounded-full border border-[rgba(242,169,58,0.32)] bg-[rgba(242,169,58,0.16)] px-2.5 sm:h-8 sm:min-h-0 text-[11px] font-medium text-[var(--amber-300)] transition-colors hover:bg-[rgba(242,169,58,0.22)] disabled:opacity-60 focus-visible:outline-none"
              >
                <Share2 className="h-3 w-3" />
                {sharingSelected ? "分享中" : `分享 ${selectedCount} 张`}
              </button>
              <button
                type="button"
                onClick={onClearSelection}
                aria-label="取消选择"
                className="inline-flex min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] sm:h-8 sm:w-8 sm:min-h-0 sm:min-w-0 focus-visible:outline-none"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </>
          ) : onToggleSelectionMode ? (
            <button
              type="button"
              onClick={onToggleSelectionMode}
              aria-pressed={selectionMode}
              className={cn(
                "inline-flex min-h-11 cursor-pointer items-center gap-1.5 rounded-full border px-2.5 sm:h-8 sm:min-h-0 text-[11px] transition-colors focus-visible:outline-none",
                selectionMode
                  ? "border-[rgba(242,169,58,0.32)] bg-[rgba(242,169,58,0.14)] text-[var(--amber-300)]"
                  : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:text-[var(--fg-0)]",
              )}
            >
              <CheckSquare className="h-3 w-3" />
              多选
            </button>
          ) : null}
          {hasControls && (
            <button
              type="button"
              onClick={onClearFilters}
              className="inline-flex min-h-11 cursor-pointer items-center gap-1.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2.5 sm:h-8 sm:min-h-0 text-[11px] text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] focus-visible:outline-none"
            >
              <Eraser className="h-3 w-3" />
              清除
            </button>
          )}
          <button
            type="button"
            onClick={onRefresh}
            disabled={refreshing}
            aria-label="刷新"
            className="inline-flex min-h-11 min-w-11 shrink-0 cursor-pointer items-center justify-center rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] disabled:opacity-50 sm:h-8 sm:w-8 sm:min-h-0 sm:min-w-0 focus-visible:outline-none"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
          </button>
          <button
            type="button"
            onClick={() => router.push("/")}
            className="inline-flex h-8 shrink-0 cursor-pointer items-center gap-1.5 rounded-full bg-[var(--amber-400)] px-3 text-[12px] font-medium text-[var(--bg-0)] shadow-amber transition-opacity hover:opacity-90 focus-visible:outline-none"
          >
            <WandSparkles className="h-3.5 w-3.5" />
            创作
          </button>
        </div>
      </div>

      {hasControls && (
        <div className="mt-2 flex min-w-0 flex-wrap items-center gap-1.5">
          {filters.ratio && (
            <span className="inline-flex h-7 items-center rounded-md border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2 text-[11px] text-[var(--fg-1)]">
              {filters.ratio}
            </span>
          )}
          {filters.has_ref && (
            <button
              type="button"
              onClick={onToggleReferenceFilter}
              className="inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2 text-[11px] text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] focus-visible:outline-none"
            >
              <ImageIcon className="h-3 w-3" />
              参考图
            </button>
          )}
          {filters.fast && (
            <button
              type="button"
              onClick={onToggleFastFilter}
              className="inline-flex h-7 cursor-pointer items-center gap-1.5 rounded-md border border-[rgba(242,169,58,0.22)] bg-[rgba(242,169,58,0.10)] px-2 text-[11px] text-[var(--amber-300)] transition-colors hover:bg-[rgba(242,169,58,0.14)] focus-visible:outline-none"
            >
              <Gauge className="h-3 w-3" />
              Fast
            </button>
          )}
          {hasSearch && (
            <span className="inline-flex h-7 max-w-full items-center gap-1.5 rounded-md border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2 text-[11px] text-[var(--fg-1)]">
              <Search className="h-3 w-3 shrink-0" />
              <span className="min-w-0 truncate">{searchValue.trim()}</span>
            </span>
          )}
        </div>
      )}
    </section>
  );
}
