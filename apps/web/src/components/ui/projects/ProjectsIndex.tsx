"use client";

// 服饰模特图工作区：
// 1) 顶部「模特库」入口按钮：横向缩略图 + 标题副标题 + 进入箭头，醒目但克制。
// 2) Hero 缩小版：font-mono mini-label + 24-28px 主标题，对齐模特库设计语言。
// 3) ProjectCard 信息密度降低：删 user_prompt，aspect 改 3/4，进入动画用 EASE.develop。
// 4) 移动端「重命名/删除」走 BottomSheet；桌面保留 popover（width 收紧到不出框）。
// 5) 搜索 / 筛选 chips / 空态保留。

import { motion, useReducedMotion } from "framer-motion";
import Image from "next/image";
import {
  AlertTriangle,
  CheckCircle2,
  ArrowLeft,
  ChevronRight,
  Clock3,
  Image as ImageIcon,
  Library,
  MoreVertical,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Shirt,
  Sparkles,
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
  // 把 fallback 数组稳定到 query.data 变化时才重建，避免下游 useMemo 每帧失效
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
      ? `${counts.all} 个项目 · ${activeCount} 个待推进`
      : "模特库 / 列表 / 新建";

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="服饰模特图"
        subtitle={mobileSubtitle}
        backHref="/projects"
        backLabel="返回项目功能中心"
        right={
          <Link
            href="/projects/apparel-model-showcase/new"
            aria-label="新建项目"
            className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-[var(--accent)] text-black shadow-[var(--shadow-amber)] active:scale-[0.96]"
          >
            <Plus className="h-4.5 w-4.5" />
          </Link>
        }
      />
      <ProjectTopBar />
      <main className="mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 pb-4 pt-3 md:mb-0 md:px-8 md:py-5">
        <div className="mx-auto grid w-full max-w-[1400px] gap-4 md:gap-5">
          <nav aria-label="项目路径" className="hidden items-center gap-1.5 text-sm md:flex">
            <Link
              href="/projects"
              className="inline-flex items-center gap-1.5 text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              项目
            </Link>
            <span aria-hidden className="text-[var(--fg-3)]">/</span>
            <span className="text-[var(--fg-0)]">服饰模特图</span>
          </nav>

          <ModelLibraryEntry />

          <Hero counts={counts} />

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
              className="rounded-xl border border-[var(--border)] bg-white/[0.03] py-14 md:rounded-md"
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

          <ResourceBand />
        </div>
      </main>
      <ProjectMobileTabBar />
    </div>
  );
}

// 项目首页底部展示模板入口。模特库已提到顶层 /library，由顶部导航直达。
function ResourceBand() {
  return (
    <section className="grid gap-3">
      <TemplateBand compact />
    </section>
  );
}

// 顶部「模特库」入口按钮：移动单行 88px / 桌面 110-130px，左侧最近模特缩略图，
// 右侧「N 个模特」+ ChevronRight 箭头。点击整体跳 /library。
function ModelLibraryEntry() {
  // TODO: 后端加 limit 后改成 limit:6 减少 payload（仅用前 6 个 avatar + total）
  const libraryQuery = useApparelModelLibraryQuery({ source: "all" });
  const items: ApparelModelLibraryItem[] = useMemo(
    () => libraryQuery.data?.items ?? [],
    [libraryQuery.data?.items],
  );
  const total = items.length;
  // 桌面 6 张缩略图，移动 3 张
  const desktopThumbs = items.slice(0, 6);
  const mobileThumbs = items.slice(0, 3);
  const hasItems = total > 0;
  const remainingDesktop = Math.max(0, total - desktopThumbs.length);

  return (
    <Link
      href="/library"
      aria-label="进入模特库"
      className={cn(
        "group block rounded-xl border bg-[var(--bg-1)]/72 transition-all duration-[var(--dur-base)] md:rounded-md md:bg-white/[0.035]",
        "border-[var(--border)] hover:border-[var(--border-amber)] hover:bg-white/[0.055] hover:shadow-[var(--shadow-amber)]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        "active:scale-[0.98] md:active:scale-100",
      )}
    >
      {/* 移动端：单行 88px */}
      <div className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 p-3 md:hidden">
        <div className="flex items-center -space-x-2.5">
          {hasItems ? (
            mobileThumbs.map((item) => (
              <ModelAvatar key={item.id} item={item} size="sm" />
            ))
          ) : (
            <PlaceholderAvatar size="sm" />
          )}
        </div>
        <div className="min-w-0">
          <p className="truncate text-[15px] font-medium text-[var(--fg-0)]">模特库</p>
          <p className="mt-0.5 truncate text-xs text-[var(--fg-2)]">
            {hasItems
              ? `${total} 个模特 · 浏览预设、收藏与生成`
              : "点击进入并新建你的第一个模特"}
          </p>
        </div>
        <ChevronRight className="h-4 w-4 shrink-0 text-[var(--fg-2)] transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-[var(--fg-0)]" />
      </div>

      {/* 桌面端：高 110-130px 三列 */}
      <div className="hidden md:grid md:grid-cols-[auto_minmax(0,1fr)_auto] md:items-center md:gap-4 md:p-5">
        <div className="flex items-center -space-x-3">
          {hasItems ? (
            <>
              {desktopThumbs.map((item) => (
                <ModelAvatar key={item.id} item={item} size="md" />
              ))}
              {remainingDesktop > 0 ? (
                <span
                  aria-hidden
                  className="relative inline-flex h-12 w-12 items-center justify-center rounded-full bg-[var(--bg-2)] text-[11px] font-medium text-[var(--fg-1)] ring-2 ring-[var(--bg-0)]"
                >
                  +{remainingDesktop}
                </span>
              ) : null}
            </>
          ) : (
            <>
              <PlaceholderAvatar size="md" />
              <PlaceholderAvatar size="md" />
              <PlaceholderAvatar size="md" />
            </>
          )}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Library className="h-4 w-4 text-[var(--amber-300)]" />
            <h2 className="text-[20px] font-medium text-[var(--fg-0)] md:text-[22px]">
              模特库
            </h2>
          </div>
          <p className="mt-1 max-w-2xl truncate text-sm text-[var(--fg-1)]">
            {hasItems
              ? "浏览预设、收藏与生成的模特"
              : "点击进入并新建你的第一个模特"}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-right">
            <p className="font-mono text-[11px] tracking-[0.16em] text-[var(--fg-2)]">
              MODELS
            </p>
            <p className="mt-0.5 font-mono text-lg leading-none tabular-nums text-[var(--fg-0)]">
              {total}
            </p>
          </div>
          <ChevronRight className="h-5 w-5 shrink-0 text-[var(--fg-2)] transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-[var(--fg-0)]" />
        </div>
      </div>
    </Link>
  );
}

function ModelAvatar({
  item,
  size,
}: {
  item: ApparelModelLibraryItem;
  size: "sm" | "md";
}) {
  const dimension = size === "md" ? 48 : 40;
  const cls =
    size === "md"
      ? "h-12 w-12 ring-2 ring-[var(--bg-0)]"
      : "h-10 w-10 ring-2 ring-[var(--bg-0)]";
  const src = item.thumb_url || item.image_url;
  return (
    <span
      className={cn(
        "relative inline-block overflow-hidden rounded-full bg-[var(--bg-2)]",
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
      ? "h-12 w-12 ring-2 ring-[var(--bg-0)]"
      : "h-10 w-10 ring-2 ring-[var(--bg-0)]";
  return (
    <span
      aria-hidden
      className={cn(
        "relative inline-flex items-center justify-center rounded-full bg-[var(--bg-2)]",
        cls,
      )}
    >
      <Users className="h-4 w-4 text-[var(--fg-2)]" />
    </span>
  );
}

function Hero({ counts }: { counts: Record<FilterKey, number> }) {
  const active = counts.running + counts.needs_review + counts.attention;
  return (
    <section className="grid gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/78 p-4 shadow-[var(--shadow-1)] md:grid-cols-[minmax(0,1fr)_auto] md:items-end md:rounded-md md:bg-white/[0.035] md:p-5">
      <div className="min-w-0">
        <p className="font-mono text-[11px] tracking-[0.16em] text-[var(--fg-2)]">
          PROJECTS · 服饰模特图
        </p>
        <div className="mt-2 flex flex-wrap items-end gap-x-3 gap-y-1">
          <h1 className="text-[20px] font-medium tracking-normal text-[var(--fg-0)] md:text-[24px]">
            服饰模特图
          </h1>
          {counts.all > 0 ? (
            <span className="pb-0.5 font-mono text-[11px] tracking-wider text-[var(--fg-2)]">
              {counts.all} TOTAL
            </span>
          ) : null}
        </div>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
          {counts.all > 0
            ? active > 0
              ? `${active} 个项目等待推进，${counts.completed} 个已交付。`
              : "所有项目都已收束，可以直接创建下一组展示图。"
            : "管理模特库、商品图分析、模特候选、展示图生成和交付流程。"}
        </p>
        <div className="mt-3 grid grid-cols-3 gap-2 md:max-w-xl">
          <StatPill
            icon={<Clock3 className="h-3.5 w-3.5" />}
            label="进行中"
            value={counts.running}
            active={counts.running > 0}
          />
          <StatPill
            icon={<AlertTriangle className="h-3.5 w-3.5" />}
            label="待确认"
            value={counts.needs_review + counts.attention}
            active={counts.needs_review + counts.attention > 0}
          />
          <StatPill
            icon={<CheckCircle2 className="h-3.5 w-3.5" />}
            label="已完成"
            value={counts.completed}
          />
        </div>
      </div>
      <Link
        href="/projects/apparel-model-showcase/new"
        className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md bg-[var(--accent)] px-4 text-[15px] font-medium text-black shadow-[var(--shadow-amber)] transition-[background-color,box-shadow,opacity] duration-150 hover:bg-[#F6B755] active:opacity-90 md:h-10 md:min-h-0 md:text-sm"
      >
        <Plus className="h-4 w-4" />
        新建项目
      </Link>
    </section>
  );
}

function StatPill({
  icon,
  label,
  value,
  active = false,
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  active?: boolean;
}) {
  return (
    <div
      className={cn(
        "min-w-0 rounded-lg border px-2 py-1.5 md:rounded-md",
        active
          ? "border-[var(--border-amber)] bg-[var(--accent-soft)]"
          : "border-[var(--border)] bg-white/[0.035]",
      )}
    >
      <div
        className={cn(
          "flex items-center gap-1.5 text-[11px]",
          active ? "text-[var(--amber-300)]" : "text-[var(--fg-2)]",
        )}
      >
        {icon}
        <span className="truncate">{label}</span>
      </div>
      <div className="mt-0.5 font-mono text-base leading-none text-[var(--fg-0)]">
        {value}
      </div>
    </div>
  );
}

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
    <section className="grid gap-3 rounded-xl border border-[var(--border)] bg-white/[0.028] p-2.5 md:rounded-md md:bg-transparent md:p-0 lg:grid-cols-[minmax(0,1fr)_minmax(0,320px)] lg:items-center">
      <div className="scrollbar-none -mx-1 flex gap-1.5 overflow-x-auto px-1 pb-0.5 md:flex-wrap md:overflow-visible md:pb-0">
        {FILTERS.map((option) => {
          const active = filter === option.key;
          const count = counts[option.key];
          return (
            <button
              key={option.key}
              type="button"
              onClick={() => onFilterChange(option.key)}
              className={cn(
                "inline-flex min-h-10 shrink-0 cursor-pointer items-center gap-1.5 rounded-full border px-3 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:h-9 md:min-h-0",
                active
                  ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                  : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/[0.04]",
              )}
            >
              {option.label}
              <span
                className={cn(
                  "rounded-full px-1.5 text-[10px] tabular-nums",
                  active ? "bg-[var(--accent)]/20 text-[var(--amber-300)]" : "bg-white/[0.06] text-[var(--fg-2)]",
                )}
              >
                {count}
              </span>
            </button>
          );
        })}
      </div>
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
        <input
          value={keyword}
          onChange={(event) => onKeywordChange(event.target.value)}
          placeholder="搜索标题或基础需求"
          className="h-11 w-full rounded-lg border border-[var(--border)] bg-[var(--bg-1)] pl-9 pr-9 text-[15px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-2)] focus:border-[var(--border-amber)] md:h-10 md:rounded-md md:text-sm"
          aria-label="搜索项目"
        />
        {keyword ? (
          <button
            type="button"
            onClick={() => onKeywordChange("")}
            aria-label="清除搜索"
            className="absolute right-1.5 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 cursor-pointer items-center justify-center rounded-full text-[var(--fg-2)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)]"
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
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {items.map((item, index) => (
        <ProjectCard key={item.id} item={item} order={index} />
      ))}
    </section>
  );
}

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

  // 桌面 popover：点击外部 / Esc 关闭。移动 BottomSheet 自带这两个行为。
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

  const openMenu = () => {
    setMenuOpen((open) => !open);
    setConfirmingDelete(false);
    setRenaming(false);
    setTitle(item.title || "服饰模特图");
  };

  // 单一 step chip 信息（合并 step + count 选 step；count 偏次要 → 删）
  return (
    <motion.div
      className="relative"
      layout={!reduceMotion}
      initial={reduceMotion ? false : { opacity: 0, y: 12 }}
      animate={reduceMotion ? undefined : { opacity: 1, y: 0 }}
      transition={
        reduceMotion
          ? undefined
          : {
              duration: 0.24,
              ease: EASE.develop,
              delay: Math.min(order * 0.035, 0.24),
            }
      }
    >
      <Link
        href={`/projects/${item.id}`}
        className={cn(
          "group grid min-h-[148px] grid-cols-[96px_minmax(0,1fr)] gap-3 rounded-xl border bg-[var(--bg-1)]/72 p-2.5 pr-3 transition-[background-color,border-color,box-shadow] duration-[var(--dur-base)] md:min-h-[140px] md:grid-cols-[104px_minmax(0,1fr)] md:rounded-md md:bg-white/[0.035] md:p-3",
          "cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          "hover:border-[var(--border-strong)] hover:bg-white/[0.055] hover:shadow-[var(--shadow-2)]",
          running
            ? "border-[var(--border-amber)]/60 shadow-[0_0_0_1px_var(--border-amber)] hover:shadow-[var(--shadow-amber)]"
            : completed
              ? "border-[var(--success)]/30"
              : failed
                ? "border-[var(--danger)]/30"
                : "border-[var(--border)]",
        )}
      >
        <div className="relative flex aspect-[3/4] items-center justify-center overflow-hidden rounded-lg bg-[var(--bg-2)] md:rounded-md">
          {thumb ? (
            <Image
              src={thumb}
              alt={item.title || "商品图"}
              fill
              sizes="104px"
              unoptimized
              className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] group-hover:scale-[1.025]"
            />
          ) : (
            <ImageIcon className="h-5 w-5 text-[var(--fg-2)]" />
          )}
          {running ? (
            <span className="absolute right-1.5 top-1.5 inline-flex items-center gap-1 rounded-full border border-[var(--border-amber)] bg-black/55 px-2 py-0.5 text-[10px] text-[var(--amber-300)] backdrop-blur">
              <span className="relative flex h-1.5 w-1.5">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[var(--amber-400)] opacity-60" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-[var(--amber-400)]" />
              </span>
              运行中
            </span>
          ) : null}
        </div>
        <div className="flex min-w-0 flex-col">
          <div className="flex items-start justify-between gap-2 pr-8">
            <p className="line-clamp-2 text-[15px] font-medium leading-5 text-[var(--fg-0)] md:truncate md:text-sm">
              {item.title || "服饰模特图"}
            </p>
            <span className="hidden shrink-0 pt-0.5 md:inline-flex">
              <ChevronRight className="h-4 w-4 shrink-0 text-[var(--fg-2)] transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-[var(--fg-0)]" />
            </span>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <StatusBadge status={item.status} needsReview={needsReview} completed={completed} />
            <Chip>{item.current_step}</Chip>
          </div>
          <div className="mt-auto flex items-end justify-between gap-2 pt-3">
            <p className="line-clamp-1 text-xs text-[var(--amber-300)]">{item.next_action}</p>
            <p className="shrink-0 text-[11px] text-[var(--fg-2)]">
              {formatRelativeTime(item.updated_at)}
            </p>
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
        className="absolute right-2.5 top-2.5 inline-flex h-11 min-h-11 w-11 min-w-11 cursor-pointer items-center justify-center rounded-full border border-transparent text-[var(--fg-2)] transition-colors hover:border-[var(--border)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:right-3 md:top-3 md:h-8 md:min-h-0 md:w-8 md:min-w-0 md:rounded-md"
      >
        <MoreVertical className="h-4 w-4" />
      </button>

      {/* 桌面 popover：仅当 isMobile === false 时挂载，避免 SSR / 移动端误显 */}
      {menuOpen && isMobile === false ? (
        <div
          ref={menuRef}
          role="menu"
          className="absolute right-3 top-12 z-10 w-[min(16rem,calc(100vw-3rem))] rounded-md border border-[var(--border)] bg-[var(--bg-1)] p-2 shadow-[var(--shadow-2)]"
        >
          {renaming ? (
            <form
              className="grid gap-2"
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
                className="h-9 rounded-md border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--border-amber)]"
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
            <div className="grid gap-2">
              <p className="text-sm text-[var(--fg-0)]">确认删除这个项目？</p>
              <p className="text-xs leading-5 text-[var(--fg-2)]">项目会从列表移除，关联对话不会被删除。</p>
              <div className="flex justify-end gap-2">
                <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmingDelete(false)}>
                  取消
                </Button>
                <Button type="button" variant="danger" size="sm" disabled={remove.isPending} onClick={() => remove.mutate(item.id)}>
                  删除
                </Button>
              </div>
            </div>
          ) : (
            <div className="grid gap-1">
              <button
                type="button"
                onClick={() => setRenaming(true)}
                role="menuitem"
                className="flex min-h-9 cursor-pointer items-center gap-2 rounded-md px-2 text-left text-sm text-[var(--fg-1)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)]"
              >
                <Pencil className="h-4 w-4" />
                重命名
              </button>
              <button
                type="button"
                onClick={() => setConfirmingDelete(true)}
                role="menuitem"
                className="flex min-h-9 cursor-pointer items-center gap-2 rounded-md px-2 text-left text-sm text-[var(--danger)] transition-colors hover:bg-[var(--danger-soft)]"
              >
                <Trash2 className="h-4 w-4" />
                删除
              </button>
            </div>
          )}
        </div>
      ) : null}

      {/* 移动 BottomSheet：仅当 isMobile === true 时挂载（null 时不挂，避免 SSR mismatch） */}
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

// 移动端 BottomSheet 内容：重命名输入框 / 删除确认 / 默认列表三种状态。
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
    <div className="grid gap-3 px-4 pb-4 pt-2">
      <div className="border-b border-[var(--border)] pb-3">
        <p className="font-mono text-[11px] tracking-[0.16em] text-[var(--fg-2)]">PROJECT</p>
        <p className="mt-1 truncate text-[15px] font-medium text-[var(--fg-0)]">
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
            <span className="text-xs text-[var(--fg-2)]">项目名称</span>
            <input
              value={title}
              onChange={(event) => onTitleChange(event.target.value)}
              maxLength={120}
              autoFocus
              className="h-11 rounded-md border border-[var(--border)] bg-[var(--bg-0)] px-3 text-[15px] text-[var(--fg-0)] outline-none focus:border-[var(--border-amber)]"
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
        <div className="grid gap-1.5">
          <button
            type="button"
            onClick={onStartRename}
            className="flex min-h-11 w-full cursor-pointer items-center gap-3 rounded-md px-3 text-left text-[15px] text-[var(--fg-0)] transition-colors hover:bg-white/[0.06] active:bg-white/[0.08]"
          >
            <Pencil className="h-4 w-4 text-[var(--fg-1)]" />
            重命名
          </button>
          <button
            type="button"
            onClick={onStartDelete}
            className="flex min-h-11 w-full cursor-pointer items-center gap-3 rounded-md px-3 text-left text-[15px] text-[var(--danger)] transition-colors hover:bg-[var(--danger-soft)] active:bg-[var(--danger-soft)]"
          >
            <Trash2 className="h-4 w-4" />
            删除
          </button>
          <button
            type="button"
            onClick={onClose}
            className="mt-2 flex min-h-11 min-w-11 w-full cursor-pointer items-center justify-center rounded-md border border-[var(--border)] px-3 text-[15px] text-[var(--fg-1)] transition-colors hover:bg-white/[0.04]"
          >
            取消
          </button>
        </div>
      )}
    </div>
  );
}

function StatusBadge({
  status,
  needsReview,
  completed,
}: {
  status: string;
  needsReview: boolean;
  completed: boolean;
}) {
  const tone = completed
    ? "border-[var(--success)]/30 bg-[var(--success-soft)] text-[var(--success)]"
    : needsReview
      ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
      : status === "failed"
        ? "border-[var(--danger)]/30 bg-[var(--danger-soft)] text-[var(--danger)]"
        : "border-[var(--border)] bg-white/[0.04] text-[var(--fg-1)]";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px]",
        tone,
      )}
    >
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full border border-[var(--border)] bg-white/[0.04] px-2 py-0.5 text-[10px] text-[var(--fg-1)]">
      {children}
    </span>
  );
}

function SkeletonGrid() {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 6 }).map((_, index) => (
        <div
          key={index}
          className="grid min-h-[148px] grid-cols-[96px_minmax(0,1fr)] gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/60 p-2.5 md:min-h-[140px] md:grid-cols-[104px_minmax(0,1fr)] md:rounded-md md:bg-white/[0.025] md:p-3"
        >
          <Skeleton className="aspect-[3/4] w-full rounded-lg md:rounded-md" />
          <div className="space-y-2">
            <Skeleton className="h-4 w-2/3" />
            <div className="mt-2 flex gap-1.5">
              <Skeleton className="h-5 w-12" />
              <Skeleton className="h-5 w-16" />
            </div>
            <Skeleton className="h-3 w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}

function ErrorPanel({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="rounded-xl border border-[var(--danger)]/30 bg-[var(--danger-soft)] p-5 text-sm md:rounded-md">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-[var(--danger-soft)] text-[var(--danger)]">
          <AlertTriangle className="h-4 w-4" />
        </span>
        <div>
          <h3 className="text-base font-medium text-[var(--fg-0)]">项目加载失败</h3>
          <p className="mt-1 text-xs leading-5 text-[var(--fg-1)]">网络错误或服务繁忙，请稍后重试。</p>
        </div>
      </div>
      <Button
        className="mt-3"
        variant="secondary"
        onClick={onRetry}
        leftIcon={<RefreshCw className="h-4 w-4" />}
      >
        重试
      </Button>
    </div>
  );
}

function EmptyHero() {
  return (
    <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/78 p-4 shadow-[var(--shadow-1)] md:rounded-md md:p-5">
      <div className="grid gap-5 md:grid-cols-[1fr_auto] md:items-center">
        <div>
          <p className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border-amber)] bg-[var(--accent-soft)] px-2.5 py-1 text-[10px] text-[var(--amber-300)]">
            <Sparkles className="h-3 w-3" />
            服饰电商工作流
          </p>
          <h2 className="mt-3 text-xl font-medium tracking-normal text-[var(--fg-0)] md:text-2xl">
            从一张商品图，到 4 张高级棚拍展示图
          </h2>
          <p className="mt-2 max-w-xl text-sm leading-6 text-[var(--fg-1)]">
            上传 1-3 张商品图，先确认 AI 合成的模特，再批量生成展示图并进入质检循环。
            八阶段闭环，可随时返修与交付。
          </p>
        </div>
        <Link
          href="/projects/apparel-model-showcase/new"
          className="inline-flex min-h-12 items-center justify-center gap-2 rounded-md bg-[var(--accent)] px-5 text-[15px] font-medium text-black shadow-[var(--shadow-amber)] transition-[background-color,opacity] duration-150 hover:bg-[#F6B755] active:opacity-90 md:h-11 md:min-h-0"
        >
          <Shirt className="h-4 w-4" />
          创建第一个项目
        </Link>
      </div>
      <div className="mt-5 grid grid-cols-3 gap-2 text-center">
        {["商品约束", "模特配饰", "生成交付"].map((label, index) => (
          <div
            key={label}
            className="rounded-lg border border-[var(--border)] bg-white/[0.035] px-2 py-3 md:rounded-md"
          >
            <div className="mx-auto flex h-7 w-7 items-center justify-center rounded-full bg-[var(--accent-soft)] font-mono text-[11px] text-[var(--amber-300)]">
              {index + 1}
            </div>
            <p className="mt-2 truncate text-xs text-[var(--fg-1)]">{label}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function TemplateBand({ compact = false }: { compact?: boolean }) {
  return (
    <section className="grid gap-3 rounded-xl border border-[var(--border)] bg-white/[0.032] p-4 md:grid-cols-[1fr_auto] md:items-center md:rounded-md">
      <div className="flex min-w-0 gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[var(--accent-soft)] text-[var(--amber-300)] md:rounded-md">
          <Shirt className="h-5 w-5" />
        </span>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-medium text-[var(--fg-0)]">服饰模特图</h2>
            <span className="rounded-full border border-[var(--border)] px-2 py-0.5 text-[10px] text-[var(--fg-2)]">
              当前模板
            </span>
          </div>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
            上传 1 到 3 张商品图，先确认合成模特，再生成 4 张电商展示图并查看质检。
          </p>
        </div>
      </div>
      <Link
        href="/projects/apparel-model-showcase/new"
        className={cn(
          "inline-flex min-h-11 items-center justify-center gap-2 rounded-md bg-[var(--accent)] px-4 font-medium text-black shadow-[var(--shadow-amber)] transition-[background-color,opacity] duration-150 hover:bg-[#F6B755] active:opacity-90",
          compact ? "md:h-9 md:min-h-0 text-sm" : "h-11 text-[15px]",
        )}
      >
        创建项目
      </Link>
    </section>
  );
}
