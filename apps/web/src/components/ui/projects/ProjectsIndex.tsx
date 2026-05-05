"use client";

// Editorial 重构：杂志大标题 + portrait 项目卡 + hairline toolbar。
// 1) ModelLibraryEntry：堆叠 portrait avatars + 右侧 N°/总数 + 编辑入口
// 2) Hero：font-display italic + mono eyebrow + 大数字三栏
// 3) Toolbar：filter 改 underline-on-active；search 极简
// 4) ProjectCard：portrait 大图 + 信息悬浮 + hover micro scale
// 5) Empty / Error / Skeleton：editorial placeholder

import { motion, useReducedMotion } from "framer-motion";
import Image from "next/image";
import {
  AlertTriangle,
  ArrowRight,
  ChevronRight,
  Library,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Users,
  X,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { EmptyState } from "@/components/ui/primitives/EmptyState";
import { Skeleton } from "@/components/ui/primitives/Skeleton";
import { toast } from "@/components/ui/primitives/Toast";
import { BottomSheet } from "@/components/ui/primitives/mobile/BottomSheet";
import { useIsMobile } from "@/hooks/useMediaQuery";
import { EASE } from "@/lib/motion";
import {
  useApparelModelLibraryQuery,
  useDeleteWorkflowMutation,
  usePatchWorkflowMutation,
  useWorkflowsQuery,
} from "@/lib/queries";
import type { ApparelModelLibraryItem, WorkflowRunListItem } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ProjectMobileTabBar, ProjectMobileTopBar, ProjectTopBar } from "./components/ProjectTopBar";
import { OnlineBanner } from "./components/OnlineBanner";
import { STATUS_LABEL } from "./types";
import { formatRelativeTime, productThumbSrc } from "./utils";

const FILTERS = [
  { key: "all", label: "全部" },
  { key: "running", label: "进行中" },
  { key: "needs_review", label: "待确认" },
  { key: "attention", label: "需要处理" },
  { key: "completed", label: "已完成" },
] as const;

type FilterKey = (typeof FILTERS)[number]["key"];

function isAttention(item: WorkflowRunListItem): boolean {
  return !["running", "needs_review", "completed"].includes(item.status);
}

function matches(filter: FilterKey, item: WorkflowRunListItem): boolean {
  if (filter === "all") return true;
  if (filter === "attention") return isAttention(item);
  return item.status === filter;
}

export function ProjectsIndex() {
  const query = useWorkflowsQuery({ type: "apparel_model_showcase", limit: 80 });
  const items = useMemo(() => query.data?.items ?? [], [query.data?.items]);
  const [filter, setFilter] = useState<FilterKey>("all");
  const [keyword, setKeyword] = useState("");
  const deferredKeyword = useDeferredValue(keyword);

  const counts = useMemo(() => {
    const map: Record<FilterKey, number> = {
      all: items.length,
      running: 0,
      needs_review: 0,
      attention: 0,
      completed: 0,
    };
    for (const item of items) {
      if (item.status === "running") map.running += 1;
      else if (item.status === "needs_review") map.needs_review += 1;
      else if (item.status === "completed") map.completed += 1;
      else if (isAttention(item)) map.attention += 1;
    }
    return map;
  }, [items]);

  const filtered = useMemo(() => {
    const kw = deferredKeyword.trim().toLowerCase();
    return items.filter((item) => {
      if (!matches(filter, item)) return false;
      if (!kw) return true;
      return (
        item.title.toLowerCase().includes(kw) ||
        item.user_prompt.toLowerCase().includes(kw)
      );
    });
  }, [items, filter, deferredKeyword]);

  const activeCount = counts.running + counts.needs_review + counts.attention;
  const mobileSubtitle =
    counts.all > 0
      ? `${counts.all} PROJECTS · ${activeCount} ACTIVE`
      : "APPAREL · LIBRARY · NEW";

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="服饰模特图"
        subtitle={mobileSubtitle}
        backHref="/projects"
        backLabel="返回项目"
        right={
          <Link
            href="/projects/apparel-model-showcase/new"
            aria-label="新建项目"
            className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-[var(--accent)] text-black active:scale-[0.96]"
          >
            <Plus className="h-[18px] w-[18px]" />
          </Link>
        }
      />
      <ProjectTopBar />
      <main className="lumen-studio-bg mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-12 pt-3 md:mb-0 md:px-10 md:py-6">
        <div className="mx-auto grid w-full max-w-[1440px] gap-6 md:gap-12">
          <Crumb />
          <Hero counts={counts} />
          <ModelLibraryEntry />
          <Toolbar
            filter={filter}
            onFilterChange={setFilter}
            counts={counts}
            keyword={keyword}
            onKeywordChange={setKeyword}
          />

          {query.isLoading ? (
            <SkeletonGrid />
          ) : query.isError ? (
            <ErrorPanel onRetry={() => query.refetch()} />
          ) : items.length === 0 ? (
            <EmptyHero />
          ) : filtered.length === 0 ? (
            <EmptyState
              className="border-t border-[var(--border)] py-16 md:py-20"
              title="没有符合条件的项目"
              description={
                deferredKeyword
                  ? `没有项目标题或基础需求包含"${deferredKeyword}"`
                  : "试试切换其它分类，或新建一个项目。"
              }
              action={
                <Button
                  variant="secondary"
                  onClick={() => {
                    setFilter("all");
                    setKeyword("");
                  }}
                >
                  清除筛选
                </Button>
              }
            />
          ) : (
            <ProjectsGrid items={filtered} />
          )}
        </div>
      </main>
      <ProjectMobileTabBar />
    </div>
  );
}

function Crumb() {
  return (
    <nav
      aria-label="项目路径"
      className="hidden items-center gap-3 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)] md:flex"
    >
      <Link
        href="/projects"
        className="transition-colors hover:text-[var(--fg-0)]"
      >
        Projects
      </Link>
      <span aria-hidden className="text-[var(--fg-3)]">·</span>
      <span className="text-[var(--fg-0)]">Apparel</span>
    </nav>
  );
}

// Hero：杂志大标题 + 三栏数字
function Hero({ counts }: { counts: Record<FilterKey, number> }) {
  const active = counts.running + counts.needs_review + counts.attention;
  return (
    <section className="grid gap-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-end md:gap-12">
      <div className="hidden min-w-0 md:block">
        <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          N°01 — Apparel Studio
        </p>
        <h1 className="mt-3 font-display text-[40px] italic leading-[0.95] tracking-tight text-[var(--fg-0)] sm:text-[44px] md:text-[72px]">
          服饰模特图
        </h1>
        <p className="mt-4 max-w-xl text-[14px] leading-[1.7] text-[var(--fg-1)]">
          {counts.all > 0
            ? active > 0
              ? `${active} 个项目正在路上，${counts.completed} 个已交付。`
              : "所有项目都已收束，可以开启下一组棚拍。"
            : "管理模特库、商品图分析、模特候选、展示图生成与交付。"}
        </p>
      </div>
      <Link
        href="/projects/apparel-model-showcase/new"
        className="group hidden items-center gap-3 self-start rounded-full bg-[var(--accent)] px-6 py-3 font-medium text-black shadow-[var(--shadow-amber)] transition-[transform,box-shadow] duration-[var(--dur-base)] hover:scale-[1.02] active:scale-[0.98] md:inline-flex md:self-end"
      >
        <Plus className="h-4 w-4" />
        新建项目
        <ArrowRight className="h-4 w-4 -translate-x-1 opacity-0 transition-all duration-[var(--dur-base)] group-hover:translate-x-0 group-hover:opacity-100" />
      </Link>

      <div className="col-span-full grid grid-cols-3 gap-px overflow-hidden border border-[var(--border)] md:max-w-3xl md:gap-px">
        <Stat label="Total" value={counts.all} />
        <Stat label="Active" value={active} accent={active > 0} />
        <Stat label="Delivered" value={counts.completed} />
      </div>
    </section>
  );
}

function Stat({
  label,
  value,
  accent = false,
}: {
  label: string;
  value: number;
  accent?: boolean;
}) {
  return (
    <div className="bg-[var(--bg-0)] px-3 py-3 md:px-6 md:py-5">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        {label}
      </p>
      <p
        className={cn(
          "mt-1 font-display text-[26px] italic leading-none tabular-nums md:text-[44px]",
          accent ? "text-[var(--amber-300)]" : "text-[var(--fg-0)]",
        )}
      >
        {String(value).padStart(2, "0")}
      </p>
    </div>
  );
}

// 模特库入口：堆叠 portrait avatars + 右侧 N°/总数。
function ModelLibraryEntry() {
  const libraryQuery = useApparelModelLibraryQuery({ source: "all" });
  const items: ApparelModelLibraryItem[] = useMemo(
    () => libraryQuery.data?.items ?? [],
    [libraryQuery.data?.items],
  );
  const total = items.length;
  const desktopThumbs = items.slice(0, 5);
  const mobileThumbs = items.slice(0, 4);
  const hasItems = total > 0;

  return (
    <Link
      href="/library"
      aria-label="进入模特库"
      className="group relative block overflow-hidden border-y border-[var(--border)] py-4 transition-colors hover:bg-[var(--bg-1)]/40 md:py-7"
    >
      <div className="grid items-center gap-4 md:grid-cols-[auto_minmax(0,1fr)_auto] md:gap-10">
        <div className="flex md:items-center">
          <div className="flex -space-x-3 md:-space-x-4">
            {hasItems ? (
              <>
                <span className="hidden md:contents">
                  {desktopThumbs.map((item, idx) => (
                    <ModelAvatar key={item.id} item={item} size="md" zIndex={10 - idx} />
                  ))}
                </span>
                <span className="contents md:hidden">
                  {mobileThumbs.map((item, idx) => (
                    <ModelAvatar key={item.id} item={item} size="sm" zIndex={10 - idx} />
                  ))}
                </span>
              </>
            ) : (
              <>
                <PlaceholderAvatar size="md" />
                <PlaceholderAvatar size="md" />
                <PlaceholderAvatar size="md" />
              </>
            )}
          </div>
        </div>
        <div className="min-w-0">
          <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
            <Library className="mr-1.5 -mt-px inline-block h-3 w-3 text-[var(--amber-300)]" />
            Library
          </p>
          <h2 className="mt-1.5 font-display text-[22px] italic leading-[1] text-[var(--fg-0)] md:mt-2 md:text-[36px]">
            模特库
          </h2>
          <p className="mt-1.5 max-w-md truncate text-[13px] text-[var(--fg-1)]">
            {hasItems
              ? "浏览预设、收藏与生成的模特"
              : "进入并新建你的第一个模特"}
          </p>
        </div>
        <div className="flex items-center justify-between gap-4 md:justify-end md:gap-6">
          <div className="text-right">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Models
            </p>
            <p className="mt-0.5 font-display text-[24px] italic leading-none tabular-nums text-[var(--fg-0)] md:text-[36px]">
              {String(total).padStart(2, "0")}
            </p>
          </div>
          <span
            aria-hidden
            className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-[var(--border)] text-[var(--fg-1)] transition-all duration-[var(--dur-base)] group-hover:border-[var(--border-amber)] group-hover:bg-[var(--accent-soft)] group-hover:text-[var(--amber-300)] md:h-12 md:w-12"
          >
            <ChevronRight className="h-4 w-4 transition-transform duration-[var(--dur-base)] group-hover:translate-x-0.5" />
          </span>
        </div>
      </div>
    </Link>
  );
}

function ModelAvatar({
  item,
  size,
  zIndex,
}: {
  item: ApparelModelLibraryItem;
  size: "sm" | "md";
  zIndex: number;
}) {
  const dimension = size === "md" ? 56 : 44;
  const cls =
    size === "md"
      ? "h-14 w-14 ring-2 ring-[var(--bg-0)]"
      : "h-11 w-11 ring-2 ring-[var(--bg-0)]";
  const src = item.thumb_url || item.image_url;
  return (
    <span
      style={{ zIndex }}
      className={cn(
        "relative inline-block overflow-hidden rounded-full bg-[var(--bg-2)] transition-transform duration-[var(--dur-base)] group-hover:translate-x-0.5",
        cls,
      )}
    >
      {src ? (
        <Image
          src={src}
          alt={item.title || "模特"}
          width={dimension}
          height={dimension}
          unoptimized
          className="h-full w-full object-cover"
        />
      ) : (
        <Users className="absolute inset-0 m-auto h-4 w-4 text-[var(--fg-2)]" />
      )}
    </span>
  );
}

function PlaceholderAvatar({ size }: { size: "sm" | "md" }) {
  const cls =
    size === "md"
      ? "h-14 w-14 ring-2 ring-[var(--bg-0)]"
      : "h-11 w-11 ring-2 ring-[var(--bg-0)]";
  return (
    <span
      aria-hidden
      className={cn(
        "relative inline-flex items-center justify-center rounded-full bg-[var(--bg-2)]",
        cls,
      )}
    >
      <Users className="h-4 w-4 text-[var(--fg-3)]" />
    </span>
  );
}

// Toolbar：filter underline + search underline
function Toolbar({
  filter,
  onFilterChange,
  counts,
  keyword,
  onKeywordChange,
}: {
  filter: FilterKey;
  onFilterChange: (filter: FilterKey) => void;
  counts: Record<FilterKey, number>;
  keyword: string;
  onKeywordChange: (value: string) => void;
}) {
  return (
    <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,280px)] lg:items-center">
      <div className="flex min-w-0 flex-wrap gap-x-1 gap-y-1 md:gap-x-1">
        {FILTERS.map((option) => {
          const active = filter === option.key;
          const count = counts[option.key];
          return (
            <button
              key={option.key}
              type="button"
              onClick={() => onFilterChange(option.key)}
              className={cn(
                "group relative inline-flex min-h-10 shrink-0 cursor-pointer items-baseline gap-2 px-3 py-2 font-mono text-[11px] uppercase tracking-[0.16em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:min-h-9",
                active ? "text-[var(--fg-0)]" : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
              )}
            >
              <span>{option.label}</span>
              <span className="tabular-nums opacity-70">
                {String(count).padStart(2, "0")}
              </span>
              <span
                aria-hidden
                className={cn(
                  "absolute inset-x-3 bottom-1 h-px transition-all duration-[var(--dur-base)]",
                  active
                    ? "bg-[var(--amber-400)]"
                    : "bg-transparent group-hover:bg-[var(--border-strong)]",
                )}
              />
            </button>
          );
        })}
      </div>
      <div className="relative">
        <Search className="pointer-events-none absolute left-0 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
        <input
          value={keyword}
          onChange={(event) => onKeywordChange(event.target.value)}
          placeholder="搜索标题或基础需求"
          className="h-11 w-full border-b border-[var(--border)] bg-transparent pl-7 pr-9 text-[15px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-2)] focus:border-[var(--amber-400)] md:h-10 md:text-sm"
          aria-label="搜索项目"
        />
        {keyword ? (
          <button
            type="button"
            onClick={() => onKeywordChange("")}
            aria-label="清除搜索"
            className="absolute right-0 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 cursor-pointer items-center justify-center text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        ) : null}
      </div>
    </section>
  );
}

function ProjectsGrid({ items }: { items: WorkflowRunListItem[] }) {
  return (
    <section className="grid grid-cols-2 gap-x-4 gap-y-8 md:gap-x-6 md:gap-y-12 lg:grid-cols-3 xl:grid-cols-4">
      {items.map((item, index) => (
        <ProjectCard key={item.id} item={item} order={index} />
      ))}
    </section>
  );
}

// Portrait 杂志卡：大图 + 下方元数据 + hover micro scale
function ProjectCard({ item, order }: { item: WorkflowRunListItem; order: number }) {
  const running = item.status === "running";
  const needsReview = item.status === "needs_review";
  const completed = item.status === "completed";
  const failed = item.status === "failed";
  const thumb = productThumbSrc(item);
  const reduceMotion = useReducedMotion();
  const isMobile = useIsMobile();
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [title, setTitle] = useState(item.title || "服饰模特图");
  const menuRef = useRef<HTMLDivElement | null>(null);
  const actionButtonRef = useRef<HTMLButtonElement | null>(null);
  const patch = usePatchWorkflowMutation({
    onSuccess: () => {
      toast.success("项目已重命名");
      setRenaming(false);
      setMenuOpen(false);
    },
    onError: (error) => toast.error(error.message || "重命名失败"),
  });
  const remove = useDeleteWorkflowMutation({
    onSuccess: () => toast.success("项目已删除"),
    onError: (error) => toast.error(error.message || "删除失败"),
  });
  const saveTitle = () => {
    const next = title.trim();
    if (!next) {
      toast.error("项目名称不能为空");
      return;
    }
    if (next === item.title) {
      setRenaming(false);
      return;
    }
    patch.mutate({ id: item.id, title: next });
  };

  const closeMenu = useCallback(() => {
    setMenuOpen(false);
    setConfirmingDelete(false);
    setRenaming(false);
  }, []);

  useEffect(() => {
    if (!menuOpen) return;
    if (isMobile) return;
    const onPointerDown = (event: PointerEvent) => {
      if (menuRef.current?.contains(event.target as Node)) return;
      if (actionButtonRef.current?.contains(event.target as Node)) return;
      closeMenu();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeMenu();
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen, isMobile, closeMenu]);

  const openMenu = (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setMenuOpen((open) => !open);
    setConfirmingDelete(false);
    setRenaming(false);
    setTitle(item.title || "服饰模特图");
  };

  const dotTone = running
    ? "bg-[var(--amber-400)] animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]"
    : needsReview
      ? "bg-[var(--amber-300)]"
      : completed
        ? "bg-[var(--success)]"
        : failed
          ? "bg-[var(--danger)]"
          : "bg-[var(--fg-3)]";

  return (
    <motion.div
      className="group relative"
      layout={!reduceMotion}
      initial={reduceMotion ? false : { opacity: 0, y: 12 }}
      animate={reduceMotion ? undefined : { opacity: 1, y: 0 }}
      transition={
        reduceMotion
          ? undefined
          : {
              duration: 0.32,
              ease: EASE.develop,
              delay: Math.min(order * 0.04, 0.32),
            }
      }
    >
      <Link
        href={`/projects/${item.id}`}
        className="block focus-visible:outline-none"
        aria-label={item.title || "服饰模特图"}
      >
        {/* 缩略图区 */}
        <div className="relative aspect-[3/4] overflow-hidden bg-[var(--bg-2)]">
          {thumb ? (
            <Image
              src={thumb}
              alt={item.title || "商品图"}
              fill
              sizes="(max-width: 640px) 100vw, (max-width: 1024px) 50vw, 25vw"
              unoptimized
              className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.04]"
            />
          ) : (
            <div className="flex h-full items-center justify-center font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-3)]">
              No Image
            </div>
          )}
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-transparent opacity-0 transition-opacity duration-[var(--dur-base)] group-hover:opacity-100"
          />
          <span className="absolute left-3 top-3 inline-flex max-w-[calc(100%-5rem)] items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference">
            N°{String(order + 1).padStart(2, "0")}
          </span>
          {running ? (
            <span className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full bg-black/55 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--amber-300)] backdrop-blur">
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[var(--amber-400)] opacity-60" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-[var(--amber-400)]" />
              </span>
              Running
            </span>
          ) : null}
        </div>

        {/* 信息区 */}
        <div className="mt-3 flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <p className="line-clamp-2 text-[15px] font-medium leading-[1.35] text-[var(--fg-0)] transition-colors duration-[var(--dur-base)] group-hover:text-[var(--amber-300)]">
              {item.title || "服饰模特图"}
            </p>
            <p className="mt-1.5 flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              <span aria-hidden className={cn("inline-block h-1.5 w-1.5 rounded-full", dotTone)} />
              <span className="truncate">{STATUS_LABEL[item.status] ?? item.status}</span>
              <span aria-hidden className="text-[var(--fg-3)]">·</span>
              <span className="truncate">{formatRelativeTime(item.updated_at)}</span>
            </p>
            {item.next_action ? (
              <p className="mt-1.5 line-clamp-1 text-[12px] text-[var(--amber-300)]">
                {item.next_action}
              </p>
            ) : null}
          </div>
        </div>
      </Link>

      <button
        ref={actionButtonRef}
        type="button"
        aria-label="项目操作"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={openMenu}
        className="absolute right-1 top-1 inline-flex h-10 min-h-10 w-10 min-w-10 cursor-pointer items-center justify-center rounded-full bg-black/35 text-white/90 opacity-100 backdrop-blur-sm transition-all duration-[var(--dur-base)] hover:bg-black/50 hover:text-white focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:bg-transparent md:text-white/80 md:opacity-0 md:group-hover:opacity-100 md:h-9 md:w-9"
      >
        <MoreHorizontal className="h-4 w-4" />
      </button>

      {menuOpen && isMobile === false ? (
        <div
          ref={menuRef}
          role="menu"
          className="absolute right-2 top-12 z-10 w-[min(15rem,calc(100vw-3rem))] max-w-[calc(100vw-1rem)] border border-[var(--border)] bg-[var(--bg-1)] p-1.5 shadow-[var(--shadow-2)]"
        >
          {renaming ? (
            <form
              className="grid gap-2 p-1.5"
              onSubmit={(event) => {
                event.preventDefault();
                saveTitle();
              }}
            >
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                maxLength={120}
                autoFocus
                className="h-9 border-b border-[var(--border)] bg-transparent px-1 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--amber-400)]"
              />
              <div className="flex justify-end gap-2">
                <Button type="button" variant="ghost" size="sm" onClick={() => setRenaming(false)}>
                  取消
                </Button>
                <Button type="submit" size="sm" disabled={patch.isPending}>
                  保存
                </Button>
              </div>
            </form>
          ) : confirmingDelete ? (
            <div className="grid gap-2 p-1.5">
              <p className="text-sm text-[var(--fg-0)]">确认删除这个项目？</p>
              <p className="text-xs leading-5 text-[var(--fg-2)]">
                项目会从列表移除，关联对话不会被删除。
              </p>
              <div className="flex justify-end gap-2">
                <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmingDelete(false)}>
                  取消
                </Button>
                <Button
                  type="button"
                  variant="danger"
                  size="sm"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(item.id)}
                >
                  删除
                </Button>
              </div>
            </div>
          ) : (
            <div className="grid gap-0.5">
              <button
                type="button"
                onClick={() => setRenaming(true)}
                role="menuitem"
                className="flex min-h-9 cursor-pointer items-center gap-2.5 px-2 text-left text-[13px] text-[var(--fg-1)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)]"
              >
                <Pencil className="h-3.5 w-3.5" />
                重命名
              </button>
              <button
                type="button"
                onClick={() => setConfirmingDelete(true)}
                role="menuitem"
                className="flex min-h-9 cursor-pointer items-center gap-2.5 px-2 text-left text-[13px] text-[var(--danger)] transition-colors hover:bg-[var(--danger-soft)]"
              >
                <Trash2 className="h-3.5 w-3.5" />
                删除
              </button>
            </div>
          )}
        </div>
      ) : null}

      {isMobile === true ? (
        <BottomSheet
          open={menuOpen}
          onClose={closeMenu}
          ariaLabel="项目操作"
          snapPoints={["auto", "40%"]}
        >
          <ProjectActionsSheet
            title={title}
            itemTitle={item.title}
            renaming={renaming}
            confirmingDelete={confirmingDelete}
            onTitleChange={setTitle}
            onStartRename={() => setRenaming(true)}
            onCancelRename={() => setRenaming(false)}
            onSaveRename={saveTitle}
            onStartDelete={() => setConfirmingDelete(true)}
            onCancelDelete={() => setConfirmingDelete(false)}
            onConfirmDelete={() => remove.mutate(item.id)}
            onClose={closeMenu}
            patchPending={patch.isPending}
            removePending={remove.isPending}
          />
        </BottomSheet>
      ) : null}
    </motion.div>
  );
}

function ProjectActionsSheet({
  title,
  itemTitle,
  renaming,
  confirmingDelete,
  onTitleChange,
  onStartRename,
  onCancelRename,
  onSaveRename,
  onStartDelete,
  onCancelDelete,
  onConfirmDelete,
  onClose,
  patchPending,
  removePending,
}: {
  title: string;
  itemTitle: string;
  renaming: boolean;
  confirmingDelete: boolean;
  onTitleChange: (value: string) => void;
  onStartRename: () => void;
  onCancelRename: () => void;
  onSaveRename: () => void;
  onStartDelete: () => void;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
  onClose: () => void;
  patchPending: boolean;
  removePending: boolean;
}) {
  return (
    <div className="grid gap-4 px-5 pb-5 pt-3">
      <div className="border-b border-[var(--border)] pb-3">
        <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
          Project
        </p>
        <p className="mt-1 truncate font-display text-[20px] italic leading-[1.1] text-[var(--fg-0)]">
          {itemTitle || "服饰模特图"}
        </p>
      </div>
      {renaming ? (
        <form
          className="grid gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            onSaveRename();
          }}
        >
          <label className="grid gap-1.5">
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              名称
            </span>
            <input
              value={title}
              onChange={(event) => onTitleChange(event.target.value)}
              maxLength={120}
              autoFocus
              className="h-11 border-b border-[var(--border)] bg-transparent px-1 text-[15px] text-[var(--fg-0)] outline-none focus:border-[var(--amber-400)]"
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <Button type="button" variant="ghost" onClick={onCancelRename} className="min-h-11">
              取消
            </Button>
            <Button type="submit" disabled={patchPending} className="min-h-11">
              保存
            </Button>
          </div>
        </form>
      ) : confirmingDelete ? (
        <div className="grid gap-3">
          <p className="text-[15px] text-[var(--fg-0)]">确认删除这个项目？</p>
          <p className="text-xs leading-5 text-[var(--fg-2)]">
            项目会从列表移除，关联对话不会被删除。
          </p>
          <div className="grid grid-cols-2 gap-2">
            <Button type="button" variant="ghost" onClick={onCancelDelete} className="min-h-11">
              取消
            </Button>
            <Button
              type="button"
              variant="danger"
              disabled={removePending}
              onClick={onConfirmDelete}
              className="min-h-11"
            >
              删除
            </Button>
          </div>
        </div>
      ) : (
        <div className="grid gap-1">
          <button
            type="button"
            onClick={onStartRename}
            className="flex min-h-11 w-full cursor-pointer items-center gap-3 px-1 text-left text-[15px] text-[var(--fg-0)] transition-colors hover:bg-white/[0.06] active:bg-white/[0.08]"
          >
            <Pencil className="h-4 w-4 text-[var(--fg-2)]" />
            重命名
          </button>
          <button
            type="button"
            onClick={onStartDelete}
            className="flex min-h-11 w-full cursor-pointer items-center gap-3 px-1 text-left text-[15px] text-[var(--danger)] transition-colors hover:bg-[var(--danger-soft)] active:bg-[var(--danger-soft)]"
          >
            <Trash2 className="h-4 w-4" />
            删除
          </button>
          <button
            type="button"
            onClick={onClose}
            className="mt-2 flex min-h-11 w-full cursor-pointer items-center justify-center border border-[var(--border)] px-3 text-[15px] text-[var(--fg-1)] transition-colors hover:bg-white/[0.04]"
          >
            取消
          </button>
        </div>
      )}
    </div>
  );
}

function SkeletonGrid() {
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-8 md:gap-x-6 md:gap-y-12 lg:grid-cols-3 xl:grid-cols-4">
      {Array.from({ length: 8 }).map((_, index) => (
        <div key={index} className="grid gap-3">
          <Skeleton className="aspect-[3/4] w-full" />
          <div className="space-y-2">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}

function ErrorPanel({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="border-y border-[var(--danger)]/30 bg-[var(--danger-soft)]/30 px-4 py-8 md:px-6 md:py-10">
      <div className="flex items-start gap-4">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-[var(--danger)]/40 text-[var(--danger)]">
          <AlertTriangle className="h-4 w-4" />
        </span>
        <div className="flex-1">
          <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--danger)]">
            Error
          </p>
          <h3 className="mt-1 font-display text-[20px] italic text-[var(--fg-0)]">
            项目加载失败
          </h3>
          <p className="mt-1 text-[13px] text-[var(--fg-1)]">
            网络错误或服务繁忙，请稍后重试。
          </p>
          <Button
            className="mt-4"
            variant="secondary"
            onClick={onRetry}
            leftIcon={<RefreshCw className="h-4 w-4" />}
          >
            重试
          </Button>
        </div>
      </div>
    </div>
  );
}

function EmptyHero() {
  return (
    <section className="border-y border-[var(--border)] py-12 md:py-20">
      <div className="grid gap-8 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--amber-300)]">
            服饰电商工作流
          </p>
          <h2 className="mt-3 font-display text-[36px] italic leading-[1.05] text-[var(--fg-0)] md:text-[56px]">
            从一张商品图，<br />到 4 张高级棚拍展示图
          </h2>
          <p className="mt-4 max-w-xl text-[14px] leading-7 text-[var(--fg-1)]">
            上传 1-3 张商品图，先确认 AI 合成的模特，再批量生成展示图并进入质检循环。
            八阶段闭环，可随时返修与交付。
          </p>
        </div>
        <Link
          href="/projects/apparel-model-showcase/new"
          className="group inline-flex items-center gap-3 self-start rounded-full bg-[var(--accent)] px-6 py-3 font-medium text-black shadow-[var(--shadow-amber)] transition-transform duration-[var(--dur-base)] hover:scale-[1.02] active:scale-[0.98] md:self-end"
        >
          <Plus className="h-4 w-4" />
          创建第一个项目
          <ArrowRight className="h-4 w-4 -translate-x-1 opacity-0 transition-all duration-[var(--dur-base)] group-hover:translate-x-0 group-hover:opacity-100" />
        </Link>
      </div>
      <div className="mt-10 grid grid-cols-3 gap-px overflow-hidden border border-[var(--border)] md:max-w-3xl">
        {["商品约束", "模特候选", "展示交付"].map((label, index) => (
          <div key={label} className="bg-[var(--bg-0)] px-4 py-5 md:px-6">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              N°{String(index + 1).padStart(2, "0")}
            </p>
            <p className="mt-1.5 font-display text-[18px] italic leading-tight text-[var(--fg-0)] md:text-[22px]">
              {label}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}
