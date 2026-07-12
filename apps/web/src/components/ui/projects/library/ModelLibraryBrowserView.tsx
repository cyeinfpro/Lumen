"use client";

import { motion } from "framer-motion";
import {
  CheckSquare,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Square,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import type {
  ApparelModelLibraryItem,
  ModelLibraryAgeSegment,
  ModelLibraryAppearance,
} from "@/lib/apiClient";
import { cn } from "@/lib/utils";

import { ModelLibraryCard } from "./ModelLibraryCard";
import {
  AGE_TABS,
  APPEARANCE_TABS,
  SOURCE_FILTERS,
  type BrowserSource,
} from "./modelLibraryBrowserOptions";

export interface ModelLibraryBrowserLayoutProps {
  activeFilterCount: number;
  ageSegment: ModelLibraryAgeSegment;
  allVisibleSelected: boolean;
  appearance: ModelLibraryAppearance;
  batchDeletePending: boolean;
  deletableIds: string[];
  deleting: boolean;
  error: unknown;
  headerExtra?: ReactNode;
  isLoadingItems: boolean;
  isLoserView: boolean;
  items: ApparelModelLibraryItem[];
  lastUploadedId: string | null;
  mode: "page" | "dialog";
  query: string;
  selectedDeletableIds: string[];
  selectedSet: ReadonlySet<string>;
  selectActionLabel: string;
  showHeader: boolean;
  showSourceSidebar: boolean;
  source: BrowserSource;
  syncCanRun: boolean;
  syncPending: boolean;
  syncSummary: string;
  onAgeChange: (value: ModelLibraryAgeSegment) => void;
  onAppearanceChange: (value: ModelLibraryAppearance) => void;
  onBatchDelete: () => void;
  onClearSelection: () => void;
  onDelete: (id: string) => void;
  onOpenFilter: () => void;
  onOpenLightbox: (item: ApparelModelLibraryItem) => void;
  onOpenUpload: () => void;
  onQueryChange: (value: string) => void;
  onRetry: () => void;
  onSelectAll: () => void;
  onSelectItem?: (item: ApparelModelLibraryItem) => void;
  onSourceChange: (value: BrowserSource) => void;
  onSync: () => void;
  onToggleSelected: (id: string) => void;
}

export function ModelLibraryBrowserLayout(
  props: ModelLibraryBrowserLayoutProps,
) {
  return (
    <>
      <ModelLibraryMobileHeader {...props} />
      <div
        className={cn(
          "grid min-h-0 flex-1 gap-4",
          props.showSourceSidebar
            ? "md:grid-cols-[116px_minmax(0,1fr)] xl:grid-cols-[124px_minmax(0,1fr)]"
            : "",
        )}
      >
        <ModelLibrarySourceSidebar
          show={props.showSourceSidebar}
          source={props.source}
          onSourceChange={props.onSourceChange}
        />
        <main className="flex min-h-0 min-w-0 flex-col gap-3">
          <ModelLibraryMobileFilters {...props} />
          <ModelLibraryDesktopFilters {...props} />
          <ModelLibraryResults {...props} />
        </main>
      </div>
    </>
  );
}

function ModelLibraryBrowserActions({
  headerExtra,
  syncCanRun,
  syncPending,
  onOpenUpload,
  onSync,
}: Pick<
  ModelLibraryBrowserLayoutProps,
  "headerExtra" | "syncCanRun" | "syncPending" | "onOpenUpload" | "onSync"
>) {
  return (
    <>
      {syncCanRun ? (
        <button
          type="button"
          onClick={onSync}
          disabled={syncPending}
          className="inline-flex min-h-11 cursor-pointer items-center gap-1.5 border border-[var(--border)] px-2.5 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] disabled:cursor-default disabled:opacity-50 md:h-8 md:min-h-0"
        >
          {syncPending ? (
            <Spinner size={12} />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
          同步
        </button>
      ) : null}
      <Button
        size="sm"
        variant="primary"
        onClick={onOpenUpload}
        leftIcon={<Upload className="h-3.5 w-3.5" />}
      >
        上传
      </Button>
      {headerExtra}
    </>
  );
}

function ModelLibraryMobileHeader(props: ModelLibraryBrowserLayoutProps) {
  if (!props.showHeader) return null;
  return (
    <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-[var(--border)] pb-2 md:hidden">
      <div className="min-w-0 flex-1 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
        <p className="min-w-0 truncate">{props.syncSummary}</p>
      </div>
      <div className="flex max-w-full shrink-0 flex-wrap items-center justify-end gap-2">
        <ModelLibraryBrowserActions {...props} />
      </div>
    </header>
  );
}

function ModelLibrarySourceSidebar({
  show,
  source,
  onSourceChange,
}: {
  show: boolean;
  source: BrowserSource;
  onSourceChange: (value: BrowserSource) => void;
}) {
  if (!show) return null;
  return (
    <aside className="hidden border-r border-[var(--border)] pr-3 md:block">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        来源
      </p>
      <div className="mt-2 grid">
        {SOURCE_FILTERS.map(([value, label]) => {
          const active = source === value;
          return (
            <button
              key={value}
              type="button"
              onClick={() => onSourceChange(value)}
              className={cn(
                "group relative flex min-h-9 cursor-pointer items-center justify-between border-b border-[var(--border)] py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] transition-colors",
                active
                  ? "text-[var(--fg-0)]"
                  : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
              )}
            >
              <span>{label}</span>
              {active ? (
                <span
                  aria-hidden
                  className="h-1.5 w-1.5 rounded-full bg-[var(--amber-400)]"
                />
              ) : null}
            </button>
          );
        })}
      </div>
    </aside>
  );
}

function ModelLibraryMobileFilters(props: ModelLibraryBrowserLayoutProps) {
  const filtersActive = props.activeFilterCount > 0;
  return (
    <div className="sticky top-0 z-20 -mx-3 flex items-center gap-2 bg-[var(--bg-0)]/95 px-3 py-2 shadow-[var(--shadow-1)] backdrop-blur-xl md:hidden">
      <div className="relative min-w-0 flex-1">
        <Search className="pointer-events-none absolute left-0 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
        <input
          value={props.query}
          onChange={(event) => props.onQueryChange(event.target.value)}
          placeholder="搜索名称、标签"
          aria-label="搜索模特"
          className="h-11 w-full min-w-0 border-b border-[var(--border)] bg-transparent pl-7 pr-2 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
        />
      </div>
      <button
        type="button"
        onClick={props.onOpenFilter}
        className={cn(
          "inline-flex min-h-11 shrink-0 cursor-pointer items-center gap-1.5 border px-3 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors",
          filtersActive
            ? "border-[var(--border-amber)] text-[var(--amber-300)]"
            : "border-[var(--border)] text-[var(--fg-1)] hover:border-[var(--border-strong)]",
        )}
      >
        <SlidersHorizontal className="h-3.5 w-3.5" />
        筛选
        {filtersActive ? (
          <span className="tabular-nums">·{props.activeFilterCount}</span>
        ) : null}
      </button>
    </div>
  );
}

function ModelLibraryDesktopFilters(props: ModelLibraryBrowserLayoutProps) {
  return (
    <div className="hidden md:grid md:gap-1.5 xl:grid-cols-[minmax(460px,1fr)_minmax(0,1.35fr)] xl:gap-x-4">
      <ChipRowGroup label="年龄段">
        {AGE_TABS.map(([value, label]) => (
          <Chip
            key={value}
            active={props.ageSegment === value}
            onClick={() => props.onAgeChange(value)}
          >
            {label}
          </Chip>
        ))}
      </ChipRowGroup>
      <ChipRowGroup label="外貌方向">
        {APPEARANCE_TABS.map(([value, label]) => (
          <Chip
            key={value}
            active={props.appearance === value}
            onClick={() => props.onAppearanceChange(value)}
          >
            {label}
          </Chip>
        ))}
      </ChipRowGroup>
      <div className="flex min-w-0 items-center gap-3 border-b border-[var(--border)] pb-2 xl:col-span-2">
        <div className="relative w-full min-w-0 max-w-md">
          <Search className="pointer-events-none absolute left-0 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
          <input
            value={props.query}
            onChange={(event) => props.onQueryChange(event.target.value)}
            placeholder="搜索名称、标签"
            className="h-9 w-full min-w-0 border-b border-[var(--border)] bg-transparent pl-7 pr-9 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
            aria-label="搜索模特"
          />
          {props.query ? (
            <button
              type="button"
              onClick={() => props.onQueryChange("")}
              aria-label="清除搜索"
              className="absolute right-0 top-1/2 inline-flex h-11 w-11 -translate-y-1/2 cursor-pointer items-center justify-center text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)] md:h-8 md:w-8"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          ) : null}
        </div>
        {!props.showSourceSidebar ? (
          <select
            value={props.source}
            onChange={(event) =>
              props.onSourceChange(event.target.value as BrowserSource)
            }
            className="h-10 max-w-full border-b border-[var(--border)] bg-transparent px-1 font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--fg-1)] outline-none focus:border-[var(--amber-400)]"
          >
            {SOURCE_FILTERS.map(([value, label]) => (
              <option key={value} value={value} className="bg-[var(--bg-0)]">
                {label}
              </option>
            ))}
          </select>
        ) : null}
        {props.showHeader ? (
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            <p className="hidden max-w-[180px] truncate font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-2)] xl:block">
              {props.syncSummary}
            </p>
            <ModelLibraryBrowserActions {...props} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ModelLibraryError({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry: () => void;
}) {
  const message = error instanceof Error ? error.message : "请稍后重试";
  return (
    <div role="alert" className="border-y border-[var(--danger-border)] py-12">
      <p className="type-page-kicker text-[var(--danger)]">加载失败</p>
      <p className="mt-2 max-w-xl text-sm text-[var(--fg-1)]">{message}</p>
      <Button variant="outline" size="sm" onClick={onRetry} className="mt-4">
        重试
      </Button>
    </div>
  );
}

function ModelLibrarySelectionBar(
  props: Pick<
    ModelLibraryBrowserLayoutProps,
    | "allVisibleSelected"
    | "batchDeletePending"
    | "deletableIds"
    | "isLoserView"
    | "selectedDeletableIds"
    | "onBatchDelete"
    | "onClearSelection"
    | "onSelectAll"
  >,
) {
  if (props.isLoserView || props.deletableIds.length === 0) return null;
  const hasSelection = props.selectedDeletableIds.length > 0;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 border-y border-[var(--border)] py-1.5">
      <button
        type="button"
        onClick={props.onSelectAll}
        className="inline-flex min-h-11 min-w-0 items-center gap-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] md:h-8 md:min-h-0"
      >
        {props.allVisibleSelected ? (
          <CheckSquare className="h-3.5 w-3.5 text-[var(--amber-300)]" />
        ) : (
          <Square className="h-3.5 w-3.5" />
        )}
        {hasSelection ? `已选 ${props.selectedDeletableIds.length} 个` : "选择"}
      </button>
      {hasSelection ? (
        <div className="flex max-w-full flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            onClick={props.onClearSelection}
            className="inline-flex min-h-11 items-center px-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)] md:h-8 md:min-h-0"
          >
            取消
          </button>
          <Button
            size="sm"
            variant="outline"
            loading={props.batchDeletePending}
            onClick={props.onBatchDelete}
            leftIcon={<Trash2 className="h-3 w-3" />}
          >
            批量删除
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function ModelLibraryGrid(props: ModelLibraryBrowserLayoutProps) {
  const cardOnSelect =
    props.mode === "dialog" && !props.isLoserView
      ? props.onSelectItem
      : undefined;
  return (
    <motion.div
      className={cn(
        "grid min-w-0 gap-x-3 gap-y-5 md:gap-x-4 md:gap-y-6",
        props.mode === "page"
          ? "grid-cols-2 min-[520px]:grid-cols-3 sm:grid-cols-4 md:grid-cols-5 xl:grid-cols-6 2xl:grid-cols-8"
          : "grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6",
      )}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
    >
      {props.items.map((item, index) => {
        const selectable = !item.id.startsWith("loser:");
        return (
          <ModelLibraryCard
            key={item.id}
            item={item}
            order={index}
            highlighted={props.lastUploadedId === item.id}
            selected={props.selectedSet.has(item.id)}
            onToggleSelected={
              selectable ? () => props.onToggleSelected(item.id) : undefined
            }
            onOpenLightbox={() => props.onOpenLightbox(item)}
            onDelete={() => props.onDelete(item.id)}
            deleting={props.deleting}
            onSaveLoser={props.isLoserView ? item : undefined}
            onSelect={cardOnSelect}
            selectLabel={props.selectActionLabel}
          />
        );
      })}
    </motion.div>
  );
}

function ModelLibraryResults(props: ModelLibraryBrowserLayoutProps) {
  if (props.error) {
    return <ModelLibraryError error={props.error} onRetry={props.onRetry} />;
  }
  if (props.isLoadingItems) {
    return (
      <div className="flex h-64 items-center justify-center gap-2 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
        <Spinner size={20} />
        {props.isLoserView ? "正在加载队列" : "正在加载"}
      </div>
    );
  }
  if (props.items.length === 0) return <EmptyBrowser />;
  return (
    <div className="grid min-h-0 flex-1 gap-3">
      <ModelLibrarySelectionBar {...props} />
      <ModelLibraryGrid {...props} />
    </div>
  );
}

function ChipRowGroup({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex min-w-0 items-start gap-2.5">
      <p className="mt-1.5 w-[68px] shrink-0 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
        {label}
      </p>
      <div className="-mx-1 flex min-w-0 flex-1 flex-wrap gap-x-2 gap-y-0.5 overflow-x-auto px-1 pb-0.5">
        {children}
      </div>
    </div>
  );
}

export function Chip({
  children,
  active,
  onClick,
}: {
  children: ReactNode;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "group relative inline-flex min-h-11 min-w-11 shrink-0 cursor-pointer items-center justify-center px-1 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:min-h-9 md:min-w-9",
        active
          ? "text-[var(--fg-0)]"
          : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
      )}
    >
      <span>{children}</span>
      <span
        aria-hidden
        className={cn(
          "absolute inset-x-1 -bottom-px h-px transition-colors duration-[var(--dur-base)]",
          active
            ? "bg-[var(--amber-400)]"
            : "bg-transparent group-hover:bg-[var(--border-strong)]",
        )}
      />
    </button>
  );
}

function EmptyBrowser() {
  return (
    <div className="border-y border-[var(--border)] py-16 md:py-20">
      <div className="grid gap-3">
        <p className="type-page-kicker text-[var(--amber-300)]">空</p>
        <h4 className="type-page-title md:text-[28px]">当前筛选没有模特</h4>
        <p className="type-body-sm max-w-xl text-[var(--fg-1)]">
          上传私有模特、生成新模特，或同步预设文件夹后再查看。
        </p>
      </div>
    </div>
  );
}
