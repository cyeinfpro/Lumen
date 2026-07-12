"use client";

// 海报风格库浏览器（蓝本：ModelLibraryBrowser）。
// 与模特库差异：
// - 顶部 tab = category（全部/收藏/插画/3D/极简/复古/中式/摄影/其他）
// - 没有"外貌方向 / 性别"chip 行；改为 palette / mood 展示在卡片下方
// - 卡片点击 → 详情抽屉（PosterStyleDetailDrawer）而非直接 Lightbox
// - "新建风格"按钮跳到 ?tab=create
//
// 关键约束（apps/web/AGENTS.md）：
// - 禁止 render 阶段访问 ref / Date.now()
// - 禁止 effect 中无依赖控制地 setState

import { AnimatePresence, motion } from "framer-motion";
import {
  CheckSquare,
  Plus,
  RefreshCw,
  Search,
  SlidersHorizontal,
  Square,
  Trash2,
  X,
} from "lucide-react";
import Image from "next/image";
import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { toast } from "@/components/ui/primitives/Toast";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import type {
  PosterStyleCategoryFilter,
  PosterStyleItem,
  PosterStyleSourceFilter,
} from "@/lib/apiClient";
import {
  POSTER_STYLE_CATEGORY_LABEL,
  POSTER_STYLE_SOURCE_LABEL,
} from "@/lib/apiClient";
import {
  useBatchDeletePosterStylesMutation,
  useDeletePosterStyleMutation,
  usePosterStylesQuery,
  useSyncPosterStylePresetsMutation,
} from "@/lib/queries";
import { cn } from "@/lib/utils";
import { formatShortDate } from "../projects/utils";
import { PosterStyleDetailDrawer } from "./PosterStyleDetailDrawer";

// category 顺序（含 all 与 user_favorites）
const CATEGORY_TABS: Array<[PosterStyleCategoryFilter, string]> = [
  ["all", POSTER_STYLE_CATEGORY_LABEL.all],
  ["user_favorites", POSTER_STYLE_CATEGORY_LABEL.user_favorites],
  ["illustration", POSTER_STYLE_CATEGORY_LABEL.illustration],
  ["3d", POSTER_STYLE_CATEGORY_LABEL["3d"]],
  ["minimal", POSTER_STYLE_CATEGORY_LABEL.minimal],
  ["retro", POSTER_STYLE_CATEGORY_LABEL.retro],
  ["traditional", POSTER_STYLE_CATEGORY_LABEL.traditional],
  ["photo", POSTER_STYLE_CATEGORY_LABEL.photo],
  ["other", POSTER_STYLE_CATEGORY_LABEL.other],
];

const SOURCE_FILTERS: Array<[PosterStyleSourceFilter, string]> = [
  ["all", POSTER_STYLE_SOURCE_LABEL.all],
  ["preset", POSTER_STYLE_SOURCE_LABEL.preset],
  ["favorite", POSTER_STYLE_SOURCE_LABEL.favorite],
  ["user_upload", POSTER_STYLE_SOURCE_LABEL.user_upload],
  ["generated", POSTER_STYLE_SOURCE_LABEL.generated],
];

const SOURCE_LABEL_SHORT: Record<
  Exclude<PosterStyleSourceFilter, "all">,
  string
> = {
  preset: "预设",
  favorite: "收藏",
  user_upload: "上传",
  generated: "生成",
};

export interface PosterStyleBrowserProps {
  /** "新建风格"按钮的回调（跳到 create tab） */
  onCreateClick?: () => void;
  className?: string;
}

export function PosterStyleBrowser({
  onCreateClick,
  className,
}: PosterStyleBrowserProps) {
  const [category, setCategory] = useState<PosterStyleCategoryFilter>("all");
  const [source, setSource] = useState<PosterStyleSourceFilter>("all");
  const [query, setQuery] = useState("");
  const [mobileFilterOpen, setMobileFilterOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [detailItemId, setDetailItemId] = useState<string | null>(null);

  const libraryQuery = usePosterStylesQuery({
    category,
    source,
    q: query,
    limit: 200,
  });
  const items = useMemo(
    () => libraryQuery.data?.items ?? [],
    [libraryQuery.data?.items],
  );
  const syncInfo = libraryQuery.data?.sync;

  // 用户条目 id 才能删（preset 也允许删，后端实际是隐藏；保留全部可选）
  const deletableIds = useMemo(() => items.map((item) => item.id), [items]);
  const selectedDeletableIds = useMemo(
    () => selectedIds.filter((id) => deletableIds.includes(id)),
    [selectedIds, deletableIds],
  );
  const selectedSet = useMemo(
    () => new Set(selectedDeletableIds),
    [selectedDeletableIds],
  );
  const allVisibleSelected =
    deletableIds.length > 0 && deletableIds.every((id) => selectedSet.has(id));

  const sync = useSyncPosterStylePresetsMutation({
    onSuccess: (result) => {
      if (result.status === "skipped") {
        toast.info("预设库刚同步过", { description: "已返回最近一次同步结果" });
      } else {
        toast.success("预设库已同步", {
          description: `新增 ${result.added}，更新 ${result.updated}，跳过 ${result.skipped}`,
        });
      }
    },
    onError: (err) =>
      toast.error("同步预设失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const deleteItem = useDeletePosterStyleMutation({
    onSuccess: () => toast.success("已从当前视图移除"),
    onError: (err) =>
      toast.error("移除失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const batchDelete = useBatchDeletePosterStylesMutation({
    onSuccess: (result) => {
      setSelectedIds([]);
      toast.success("已批量删除", {
        description: `删除 ${result.deleted} 个${
          result.not_found.length ? `，${result.not_found.length} 个未找到` : ""
        }`,
      });
    },
    onError: (err) =>
      toast.error("批量删除失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  const activeFilterCount = useMemo(() => {
    let n = 0;
    if (category !== "all") n += 1;
    if (source !== "all") n += 1;
    return n;
  }, [category, source]);

  const syncSummary = syncInfo?.last_success_at
    ? `同步 ${formatShortDate(syncInfo.last_success_at)}`
    : "预设 / 收藏 / 上传 / 生成";

  const renderBrowserActions = () => (
    <>
      {syncInfo?.can_sync ? (
        <button
          type="button"
          onClick={() => sync.mutate()}
          disabled={sync.isPending}
          className="inline-flex min-h-11 cursor-pointer items-center gap-1.5 border border-[var(--border)] px-2.5 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] disabled:cursor-default disabled:opacity-50 md:h-8 md:min-h-0"
        >
          {sync.isPending ? <Spinner size={12} /> : <RefreshCw className="h-3 w-3" />}
          同步
        </button>
      ) : null}
      {onCreateClick ? (
        <Button
          size="sm"
          variant="primary"
          onClick={onCreateClick}
          leftIcon={<Plus className="h-3.5 w-3.5" />}
        >
          新建风格
        </Button>
      ) : null}
    </>
  );

  return (
    <div className={cn("flex min-h-0 flex-1 flex-col gap-3", className)}>
      {/* mobile header */}
      <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-[var(--border)] pb-2 md:hidden">
        <div className="min-w-0 flex-1 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
          <p className="min-w-0 truncate">{syncSummary}</p>
        </div>
        <div className="flex max-w-full shrink-0 flex-wrap items-center justify-end gap-2">
          {renderBrowserActions()}
        </div>
      </header>

      <div className="grid min-h-0 flex-1 gap-4 md:grid-cols-[116px_minmax(0,1fr)] xl:grid-cols-[124px_minmax(0,1fr)]">
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
                  onClick={() => setSource(value)}
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

        <main className="flex min-h-0 min-w-0 flex-col gap-3">
          {/* mobile: search + filter sheet */}
          <div className="sticky top-0 z-20 -mx-3 flex items-center gap-2 bg-[var(--bg-0)]/95 px-3 py-2 shadow-[var(--shadow-1)] backdrop-blur-xl md:hidden">
            <div className="relative flex-1 min-w-0">
              <Search className="pointer-events-none absolute left-0 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索名称、标签"
                className="h-11 w-full min-w-0 border-b border-[var(--border)] bg-transparent pl-7 pr-2 text-[15px] text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
              />
            </div>
            <button
              type="button"
              onClick={() => setMobileFilterOpen(true)}
              className={cn(
                "inline-flex min-h-11 shrink-0 cursor-pointer items-center gap-1.5 border px-3 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors",
                activeFilterCount > 0
                  ? "border-[var(--border-amber)] text-[var(--amber-300)]"
                  : "border-[var(--border)] text-[var(--fg-1)] hover:border-[var(--border-strong)]",
              )}
            >
              <SlidersHorizontal className="h-3.5 w-3.5" />
              筛选
              {activeFilterCount > 0 ? (
                <span className="tabular-nums">·{activeFilterCount}</span>
              ) : null}
            </button>
          </div>

          {/* desktop: full filter strip */}
          <div className="hidden md:grid md:gap-1.5 xl:grid-cols-[minmax(460px,1fr)_minmax(0,1.35fr)] xl:gap-x-4">
            <ChipRowGroup label="类目">
              {CATEGORY_TABS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={category === value}
                  onClick={() => setCategory(value)}
                >
                  {label}
                </Chip>
              ))}
            </ChipRowGroup>
            <div className="flex min-w-0 items-center gap-3 border-b border-[var(--border)] pb-2 xl:col-span-2">
              <div className="relative w-full min-w-0 max-w-md">
                <Search className="pointer-events-none absolute left-0 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索名称、标签"
                  className="h-9 w-full min-w-0 border-b border-[var(--border)] bg-transparent pl-7 pr-9 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
                  aria-label="搜索风格"
                />
                {query ? (
                  <button
                    type="button"
                    onClick={() => setQuery("")}
                    aria-label="清除搜索"
                    className="absolute right-0 top-1/2 inline-flex h-11 w-11 -translate-y-1/2 cursor-pointer items-center justify-center text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)] md:h-8 md:w-8"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                ) : null}
              </div>
              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                <p className="hidden max-w-[180px] truncate font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-2)] xl:block">
                  {syncSummary}
                </p>
                {renderBrowserActions()}
              </div>
            </div>
          </div>

          <div className="min-h-0 flex-1">
            {libraryQuery.isPending ? (
              <div className="flex h-64 items-center justify-center gap-2 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
                <Spinner size={20} />
                正在加载
              </div>
            ) : items.length === 0 ? (
              <EmptyBrowser />
            ) : (
              <div className="grid gap-3">
                {deletableIds.length > 0 ? (
                  <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 border-y border-[var(--border)] py-1.5">
                    <button
                      type="button"
                      onClick={() =>
                        setSelectedIds(allVisibleSelected ? [] : deletableIds)
                      }
                      className="inline-flex min-h-11 min-w-0 items-center gap-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] md:h-8 md:min-h-0"
                    >
                      {allVisibleSelected ? (
                        <CheckSquare className="h-3.5 w-3.5 text-[var(--amber-300)]" />
                      ) : (
                        <Square className="h-3.5 w-3.5" />
                      )}
                      {selectedDeletableIds.length > 0
                        ? `已选 ${selectedDeletableIds.length} 个`
                        : "选择"}
                    </button>
                    {selectedDeletableIds.length > 0 ? (
                      <div className="flex max-w-full flex-wrap items-center justify-end gap-2">
                        <button
                          type="button"
                          onClick={() => setSelectedIds([])}
                          className="inline-flex min-h-11 items-center px-2 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)] md:h-8 md:min-h-0"
                        >
                          取消
                        </button>
                        <Button
                          size="sm"
                          variant="outline"
                          loading={batchDelete.isPending}
                          onClick={() =>
                            batchDelete.mutate(selectedDeletableIds)
                          }
                          leftIcon={<Trash2 className="h-3 w-3" />}
                        >
                          批量删除
                        </Button>
                      </div>
                    ) : null}
                  </div>
                ) : null}
                <motion.div
                  className="grid min-w-0 grid-cols-2 gap-x-3 gap-y-5 sm:grid-cols-3 md:grid-cols-4 md:gap-x-4 md:gap-y-6 xl:grid-cols-5 2xl:grid-cols-6"
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.18 }}
                >
                  {items.map((item, index) => (
                    <PosterStyleCard
                      key={item.id}
                      item={item}
                      order={index}
                      selected={selectedSet.has(item.id)}
                      onToggleSelected={() =>
                        setSelectedIds((prev) =>
                          prev.includes(item.id)
                            ? prev.filter((id) => id !== item.id)
                            : [...prev, item.id],
                        )
                      }
                      onOpenDetail={() => setDetailItemId(item.id)}
                      onDelete={() => deleteItem.mutate(item.id)}
                      deleting={deleteItem.isPending || batchDelete.isPending}
                    />
                  ))}
                </motion.div>
              </div>
            )}
          </div>
        </main>
      </div>

      <PosterStyleBrowserOverlays
        category={category}
        detailItemId={detailItemId}
        mobileFilterOpen={mobileFilterOpen}
        source={source}
        onCategoryChange={setCategory}
        onCloseDetail={() => setDetailItemId(null)}
        onCloseMobileFilter={() => setMobileFilterOpen(false)}
        onSourceChange={setSource}
      />
    </div>
  );
}

function PosterStyleBrowserOverlays({
  category,
  detailItemId,
  mobileFilterOpen,
  source,
  onCategoryChange,
  onCloseDetail,
  onCloseMobileFilter,
  onSourceChange,
}: {
  category: PosterStyleCategoryFilter;
  detailItemId: string | null;
  mobileFilterOpen: boolean;
  source: PosterStyleSourceFilter;
  onCategoryChange: (value: PosterStyleCategoryFilter) => void;
  onCloseDetail: () => void;
  onCloseMobileFilter: () => void;
  onSourceChange: (value: PosterStyleSourceFilter) => void;
}) {
  return (
    <>
      <AnimatePresence>
        {mobileFilterOpen ? (
          <MobileFilterSheet
            key="mobile-filter"
            category={category}
            source={source}
            onCategoryChange={onCategoryChange}
            onSourceChange={onSourceChange}
            onClose={onCloseMobileFilter}
          />
        ) : null}
      </AnimatePresence>

      <AnimatePresence>
        {detailItemId ? (
          <PosterStyleDetailDrawer
            key={detailItemId}
            itemId={detailItemId}
            onClose={onCloseDetail}
          />
        ) : null}
      </AnimatePresence>
    </>
  );
}

function ChipRowGroup({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 items-start gap-2.5">
      <p className="mt-1.5 w-[58px] shrink-0 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
        {label}
      </p>
      <div className="-mx-1 flex min-w-0 flex-1 flex-wrap gap-x-2 gap-y-0.5 overflow-x-auto px-1 pb-0.5">
        {children}
      </div>
    </div>
  );
}

function Chip({
  children,
  active,
  onClick,
}: {
  children: React.ReactNode;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "group relative inline-flex min-h-11 min-w-11 shrink-0 cursor-pointer items-center justify-center px-1 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:min-h-9 md:min-w-9",
        active ? "text-[var(--fg-0)]" : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
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
        <h4 className="type-page-title md:text-[28px]">当前筛选没有风格</h4>
        <p className="type-body-sm max-w-xl text-[var(--fg-1)]">
          切换类目、来源筛选，或同步预设、生成一组新风格后再查看。
        </p>
      </div>
    </div>
  );
}

function PosterStyleCard({
  item,
  order,
  selected,
  deleting,
  onOpenDetail,
  onDelete,
  onToggleSelected,
}: {
  item: PosterStyleItem;
  order: number;
  selected: boolean;
  deleting: boolean;
  onOpenDetail: () => void;
  onDelete: () => void;
  onToggleSelected: () => void;
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const isPreset = item.source === "preset";
  const requestDelete = () => {
    if (confirmingDelete) {
      onDelete();
      setConfirmingDelete(false);
      return;
    }
    setConfirmingDelete(true);
    window.setTimeout(() => setConfirmingDelete(false), 3000);
  };

  const previewUrl =
    item.thumb_url || item.display_url || item.cover_image_url;
  const palette = (item.palette || []).slice(0, 5);

  return (
    <article className="group relative">
      {/* @ui-governance-allow media: selection control overlays the style thumbnail. */}
      <button
        type="button"
        onClick={onToggleSelected}
        aria-label={selected ? "取消选择" : "选择风格"}
        className={cn(
          // @ui-governance-allow media
          "absolute left-2 top-2 z-10 inline-flex h-11 w-11 items-center justify-center rounded-full border backdrop-blur transition-colors md:h-8 md:w-8",
          selected
            ? "border-[var(--border-amber)] bg-[var(--accent)] text-[var(--bg-0)]"
            : "border-white/40 bg-black/35 text-white hover:bg-black/55",
        )}
      >
        {selected ? (
          <CheckSquare className="h-4 w-4" />
        ) : (
          <Square className="h-4 w-4" />
        )}
      </button>
      {/* cover：方形（海报风格一般 1:1 预览） */}
      <button
        type="button"
        onClick={onOpenDetail}
        aria-label={`查看 ${item.title} 详情`}
        className="relative block aspect-square w-full cursor-pointer overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
      >
        <Image
          src={previewUrl}
          alt={item.title}
          fill
          unoptimized
          sizes="(max-width: 640px) 50vw, (max-width: 1024px) 33vw, 240px"
          className="object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
        />
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-transparent opacity-0 transition-opacity duration-[var(--dur-base)] group-hover:opacity-100"
        />
        <span className="absolute left-11 top-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference">
          N°{String(order + 1).padStart(2, "0")}
        </span>
        <span className="absolute right-2 top-2 inline-flex items-center font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference">
          {SOURCE_LABEL_SHORT[item.source]}
        </span>
        {/* category 徽标 */}
        <span className="absolute bottom-2 right-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference">
          {POSTER_STYLE_CATEGORY_LABEL[item.category]}
        </span>
      </button>

      <div className="mt-2 grid min-w-0 gap-0.5">
        <p className="line-clamp-1 min-w-0 break-words text-[13px] font-medium leading-[1.3] text-[var(--fg-0)] transition-colors duration-[var(--dur-base)] group-hover:text-[var(--amber-300)]">
          {item.title}
        </p>
        {item.mood ? (
          <p className="line-clamp-1 min-w-0 break-words font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-2)] min-[390px]:tracking-[0.16em]">
            {item.mood}
          </p>
        ) : null}
        {item.style_tags.length > 0 ? (
          <p className="line-clamp-1 min-w-0 break-words font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-2)] min-[390px]:tracking-[0.16em]">
            {item.style_tags.slice(0, 3).join(" · ")}
          </p>
        ) : (
          <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-3)] min-[390px]:tracking-[0.16em]">
            未打标
          </p>
        )}
        {palette.length > 0 ? (
          <div
            className="mt-1.5 flex items-center gap-1"
            aria-label={`色板：${palette.join("、")}`}
          >
            {palette.map((hex, idx) => (
              <span
                key={`${hex}-${idx}`}
                aria-hidden
                title={hex}
                className="h-3 w-3 rounded-full border border-[var(--border)]"
                style={{ backgroundColor: hex }}
              />
            ))}
          </div>
        ) : null}

        <PosterStyleCardDeleteAction
          confirmingDelete={confirmingDelete}
          deleting={deleting}
          isPreset={isPreset}
          onDelete={requestDelete}
        />
      </div>
    </article>
  );
}

function PosterStyleCardDeleteAction({
  confirmingDelete,
  deleting,
  isPreset,
  onDelete,
}: {
  confirmingDelete: boolean;
  deleting: boolean;
  isPreset: boolean;
  onDelete: () => void;
}) {
  const label = confirmingDelete ? "再次点击确认删除" : isPreset ? "隐藏预设" : "删除条目";
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
      <button
        type="button"
        onClick={onDelete}
        disabled={deleting}
        title={label}
        aria-label={label}
        className={cn(
          "inline-flex min-h-11 cursor-pointer items-center gap-1 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors disabled:cursor-not-allowed disabled:opacity-50 md:h-7 md:min-h-0",
          confirmingDelete
            ? "text-[var(--danger)]"
            : "text-[var(--fg-2)] hover:text-[var(--danger)]",
        )}
      >
        {deleting ? <Spinner size={12} /> : <Trash2 className="h-3 w-3" />}
        {confirmingDelete ? "确认" : isPreset ? "隐藏" : "删除"}
      </button>
    </div>
  );
}

function MobileFilterSheet({
  category,
  source,
  onCategoryChange,
  onSourceChange,
  onClose,
}: {
  category: PosterStyleCategoryFilter;
  source: PosterStyleSourceFilter;
  onCategoryChange: (value: PosterStyleCategoryFilter) => void;
  onSourceChange: (value: PosterStyleSourceFilter) => void;
  onClose: () => void;
}) {
  // ESC 关闭 + body lock（模特库等价写法）
  useBodyScrollLock(true);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[var(--z-dialog)] flex items-end bg-black/60 backdrop-blur-sm mobile-dialog-shell md:hidden"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label="筛选"
        initial={{ y: "100%" }}
        animate={{ y: 0 }}
        exit={{ y: "100%" }}
        transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
        className="mobile-dialog-sheet flex w-full flex-col overflow-hidden border-t border-[var(--border)] bg-[var(--bg-0)]"
      >
        <header className="flex items-start justify-between gap-2 border-b border-[var(--border)] px-5 pb-4 pt-5">
          <div>
            <p className="type-page-kicker">筛选</p>
            <h3 className="type-page-title-sm mt-2">筛选</h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="inline-flex h-11 w-11 cursor-pointer items-center justify-center text-[var(--fg-2)] hover:text-[var(--fg-0)]"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="mobile-dialog-scroll flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto overscroll-contain px-5 py-5">
          <div className="grid gap-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              类目
            </p>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {CATEGORY_TABS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={category === value}
                  onClick={() => onCategoryChange(value)}
                >
                  {label}
                </Chip>
              ))}
            </div>
          </div>
          <div className="grid gap-2">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              来源
            </p>
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {SOURCE_FILTERS.map(([value, label]) => (
                <Chip
                  key={value}
                  active={source === value}
                  onClick={() => onSourceChange(value)}
                >
                  {label}
                </Chip>
              ))}
            </div>
          </div>
        </div>
        <footer className="mobile-dialog-footer grid shrink-0 grid-cols-2 gap-2 border-t border-[var(--border)] px-5 py-4 md:flex md:items-center md:justify-between">
          <Button
            variant="outline"
            onClick={() => {
              onCategoryChange("all");
              onSourceChange("all");
            }}
            className="w-full md:w-auto"
          >
            清空
          </Button>
          <Button
            variant="primary"
            onClick={onClose}
            className="w-full md:w-auto"
          >
            完成
          </Button>
        </footer>
      </motion.div>
    </div>
  );
}
