"use client";

import {
  Eraser,
  CheckSquare,
  Image as ImageIcon,
  Layers3,
  RefreshCw,
  Search,
  Share2,
  SlidersHorizontal,
  WandSparkles,
  X,
} from "lucide-react";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";

import { Button, IconButton } from "@/components/ui/primitives";
import type { StreamFeedFilters } from "../model/contracts";
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
  searchActive?: boolean;
  filterActive?: boolean;
  onToggleSearch?: () => void;
  onToggleFilter?: () => void;
  children?: ReactNode;
  onRefresh: () => void;
  onClearFilters: () => void;
  onToggleReferenceFilter: () => void;
  onToggleSelectionMode?: () => void;
  onClearSelection?: () => void;
  onShareSelected?: () => void;
}

function hasStreamOverviewFilters(filters: StreamFeedFilters): boolean {
  return Boolean(filters.ratio || filters.has_ref);
}

function SelectionControls({
  selectionMode,
  selectedCount,
  sharingSelected,
  onToggleSelectionMode,
  onClearSelection,
  onShareSelected,
}: Pick<
  StreamOverviewProps,
  | "selectionMode"
  | "selectedCount"
  | "sharingSelected"
  | "onToggleSelectionMode"
  | "onClearSelection"
  | "onShareSelected"
>) {
  if (selectedCount && selectedCount > 0) {
    return (
      <>
        <button
          type="button"
          onClick={onShareSelected}
          disabled={sharingSelected}
          className="type-control inline-flex min-h-11 shrink-0 cursor-pointer items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] px-3 text-[var(--accent)] transition-colors hover:bg-[var(--bg-2)] disabled:opacity-60 focus-visible:outline-none md:h-9 md:min-h-0"
        >
          <Share2 className="h-3 w-3" />
          {sharingSelected ? "分享中" : `分享 ${selectedCount} 张`}
        </button>
        <button
          type="button"
          onClick={onClearSelection}
          aria-label="取消选择"
          className="inline-flex min-h-11 min-w-11 cursor-pointer items-center justify-center rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] focus-visible:outline-none md:h-9 md:w-9 md:min-h-0 md:min-w-0"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </>
    );
  }
  if (!onToggleSelectionMode) return null;
  return (
    <button
      type="button"
      onClick={onToggleSelectionMode}
      aria-pressed={selectionMode}
      className={cn(
        "type-control inline-flex min-h-11 cursor-pointer items-center gap-1.5 rounded-[var(--radius-control)] border px-2.5 transition-colors focus-visible:outline-none md:h-9 md:min-h-0",
        selectionMode
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-2)] text-[var(--fg-1)] hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]",
      )}
    >
      <CheckSquare className="h-3 w-3" />
      多选
    </button>
  );
}

function StreamToolbarActions({
  hasControls,
  searchActive,
  filterActive,
  selectionMode,
  selectedCount,
  sharingSelected,
  refreshing,
  onToggleSearch,
  onToggleFilter,
  onToggleSelectionMode,
  onClearSelection,
  onShareSelected,
  onClearFilters,
  onRefresh,
}: Pick<
  StreamOverviewProps,
  | "searchActive"
  | "filterActive"
  | "selectionMode"
  | "selectedCount"
  | "sharingSelected"
  | "refreshing"
  | "onToggleSearch"
  | "onToggleFilter"
  | "onToggleSelectionMode"
  | "onClearSelection"
  | "onShareSelected"
  | "onClearFilters"
  | "onRefresh"
> & { hasControls: boolean }) {
  const router = useRouter();
  return (
    <div className="flex min-w-0 items-center gap-2 overflow-x-auto pb-0.5 no-scrollbar min-[400px]:shrink-0 min-[400px]:pb-0">
      {onToggleSearch ? (
        <IconButton
          size="sm"
          variant="outline"
          aria-label="搜索素材"
          aria-pressed={searchActive}
          tooltip="搜索素材"
          tooltipSide="bottom"
          onClick={onToggleSearch}
          className={cn(
            searchActive &&
              "border-accent-border bg-[var(--accent-soft)] text-accent",
          )}
        >
          <Search className="h-3.5 w-3.5" />
        </IconButton>
      ) : null}
      {onToggleFilter ? (
        <IconButton
          size="sm"
          variant="outline"
          aria-label="筛选素材"
          aria-pressed={filterActive}
          tooltip="筛选素材"
          tooltipSide="bottom"
          onClick={onToggleFilter}
          className={cn(
            filterActive &&
              "border-accent-border bg-[var(--accent-soft)] text-accent",
          )}
        >
          <SlidersHorizontal className="h-3.5 w-3.5" />
        </IconButton>
      ) : null}
      <SelectionControls
        selectionMode={selectionMode}
        selectedCount={selectedCount}
        sharingSelected={sharingSelected}
        onToggleSelectionMode={onToggleSelectionMode}
        onClearSelection={onClearSelection}
        onShareSelected={onShareSelected}
      />
      {hasControls ? (
        <button
          type="button"
          onClick={onClearFilters}
          className="type-control inline-flex min-h-11 cursor-pointer items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-2.5 text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] focus-visible:outline-none md:h-9 md:min-h-0"
        >
          <Eraser className="h-3 w-3" />
          清除
        </button>
      ) : null}
      <IconButton
        size="sm"
        variant="outline"
        onClick={onRefresh}
        loading={refreshing}
        aria-label="刷新素材"
        tooltip="刷新素材"
        tooltipSide="bottom"
      >
        <RefreshCw className="h-3.5 w-3.5" />
      </IconButton>
      <Button
        variant="primary"
        size="sm"
        onClick={() => router.push("/")}
        className="shrink-0 md:h-9"
        leftIcon={<WandSparkles className="h-3.5 w-3.5" />}
      >
        创作
      </Button>
    </div>
  );
}

function FilterChips({
  filters,
  searchValue,
  onToggleReferenceFilter,
}: Pick<
  StreamOverviewProps,
  | "filters"
  | "searchValue"
  | "onToggleReferenceFilter"
>) {
  const hasSearch = searchValue.trim().length > 0;
  return (
    <>
      {filters.ratio && (
        <span className="inline-flex min-h-8 items-center rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2 type-caption text-[var(--fg-1)]">
          {filters.ratio}
        </span>
      )}
      {filters.has_ref && (
        <button
          type="button"
          onClick={onToggleReferenceFilter}
          className="inline-flex min-h-11 cursor-pointer items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2 type-caption text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] focus-visible:outline-none md:min-h-8"
        >
          <ImageIcon className="h-3 w-3" />
          参考图
        </button>
      )}
      {hasSearch && (
        <span className="inline-flex min-h-8 max-w-full items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2 type-caption text-[var(--fg-1)]">
          <Search className="h-3 w-3 shrink-0" />
          <span className="min-w-0 truncate">{searchValue.trim()}</span>
        </span>
      )}
    </>
  );
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
  searchActive = false,
  filterActive = false,
  onToggleSearch,
  onToggleFilter,
  children,
  onRefresh,
  onClearFilters,
  onToggleReferenceFilter,
  onToggleSelectionMode,
  onClearSelection,
  onShareSelected,
}: StreamOverviewProps) {
  const hasFilter = hasStreamOverviewFilters(filters);
  const hasSearch = searchValue.trim().length > 0;
  const hasControls = hasFilter || hasSearch;
  const visibleLabel = hasControls ? `${visible}/${loaded}` : `${loaded}`;

  return (
    <section
      aria-label="图库工具栏"
      data-asset-content-toolbar
      className="toolbar-shell sticky top-0 z-[var(--z-header)] bg-[var(--bg-0)]/96 px-3 backdrop-blur-xl md:static md:bg-transparent md:px-0 md:backdrop-blur-none"
    >
      <div className="flex flex-col gap-2 min-[400px]:flex-row min-[400px]:items-center min-[400px]:justify-between md:gap-3">
        <div className="type-caption flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          <span className="inline-flex items-center gap-1.5 text-[var(--fg-1)]">
            <ImageIcon className="h-3.5 w-3.5 text-accent" />
            <span className="tabular-nums">{visibleLabel} 张</span>
          </span>
          <span className="inline-flex items-center gap-1.5 text-[var(--fg-2)]">
            <Layers3 className="h-3.5 w-3.5" />
            <span className="tabular-nums">{promptCount} 提示词</span>
          </span>
          {total > loaded && (
            <span className="type-caption tabular-nums text-[var(--fg-2)]">
              共 {total}，继续下滑加载
            </span>
          )}
        </div>

        <StreamToolbarActions
          hasControls={hasControls}
          searchActive={searchActive}
          filterActive={filterActive}
          selectionMode={selectionMode}
          selectedCount={selectedCount}
          sharingSelected={sharingSelected}
          refreshing={refreshing}
          onToggleSearch={onToggleSearch}
          onToggleFilter={onToggleFilter}
          onToggleSelectionMode={onToggleSelectionMode}
          onClearSelection={onClearSelection}
          onShareSelected={onShareSelected}
          onClearFilters={onClearFilters}
          onRefresh={onRefresh}
        />
      </div>

      {children ? <div className="mt-2 grid gap-1">{children}</div> : null}

      {hasControls && (
        <div className="mt-2 flex min-w-0 flex-wrap items-center gap-1.5">
          <FilterChips
            filters={filters}
            searchValue={searchValue}
            onToggleReferenceFilter={onToggleReferenceFilter}
          />
        </div>
      )}
    </section>
  );
}
