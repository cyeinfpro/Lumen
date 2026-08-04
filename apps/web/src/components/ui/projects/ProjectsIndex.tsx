"use client";

// 项目列表页：与「创作 / 图库 / 我的」对齐的编辑室式版式。
// - 顶部信息带 + 数据条
// - 模特库入口改为整行带状链接
// - 筛选 / 搜索 / 项目卡片统一 hairline 节奏
// - 空态 / 错误态 / 骨架保持克制，避免再叠多层卡片

import { motion, useReducedMotion } from "framer-motion";
import Image from "next/image";
import {
  ArrowRight,
  ChevronRight,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Trash2,
  Users,
  X,
} from "lucide-react";
import Link from "next/link";
import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { EmptyState } from "@/components/ui/primitives/EmptyState";
import { MediaControlButton } from "@/components/ui/primitives/MediaControlButton";
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

import {
  EmptyHero,
  ErrorPanel,
  ProjectActionsSheet,
  SkeletonGrid,
} from "./ProjectsIndexStates";

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
    <div className="page-shell relative h-[100dvh]">
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
            className="inline-flex h-11 w-11 items-center justify-center rounded-full bg-[var(--accent)] text-black active:scale-[0.96] md:h-9 md:w-9"
          >
            <Plus className="h-[18px] w-[18px]" />
          </Link>
        }
      />
      <ProjectTopBar />
      <main className="page-scroll lumen-studio-bg project-mobile-scroll mb-[var(--mobile-tabbar-height)]">
        <div className="page-frame grid gap-3">
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

// 与模特库页一致的桌面信息带：标题、摘要、统计和动作收进一行。
function Hero({ counts }: { counts: Record<FilterKey, number> }) {
  const active = counts.running + counts.needs_review + counts.attention;
  const summary =
    counts.all > 0
      ? active > 0
        ? `${active} 个项目进行中，${counts.completed} 个已交付。`
        : "所有项目都已收束，可以开启下一组棚拍。"
      : "管理模特库、商品图分析、模特候选、展示图生成与交付。";
  return (
    <header className="page-header hidden md:grid">
      <div className="page-header-copy">
        <p className="type-page-kicker">
          Project Index
        </p>
        <h1 className="type-page-title">服饰模特图</h1>
        <p className="type-page-subtitle hidden max-w-3xl lg:block">
          {summary}
        </p>
      </div>

      <div className="page-header-actions">
        <Link
          href="/projects/apparel-model-showcase/new"
          className="group inline-flex min-h-9 shrink-0 items-center gap-1.5 bg-[var(--accent)] px-3 type-caption font-medium text-black shadow-[var(--shadow-amber)] transition-[transform,box-shadow] duration-[var(--dur-base)] hover:scale-[1.02] active:scale-[0.98]"
        >
          <Plus className="h-3.5 w-3.5" />
          新建项目
          <ArrowRight className="h-3.5 w-3.5 -translate-x-1 opacity-0 transition-all duration-[var(--dur-base)] group-hover:translate-x-0 group-hover:opacity-100" />
        </Link>
      </div>
    </header>
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
  const mobileThumbs = items.slice(0, 3);
  const hasItems = total > 0;

  return (
    <Link
      href="/library"
      aria-label="进入模特库"
      className="group block border-y border-[var(--border)] py-3 transition-colors hover:bg-[var(--bg-1)]/45 md:py-3.5"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3 md:gap-4">
          <div className="flex shrink-0 -space-x-2.5 md:-space-x-3">
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
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-2)]">
              模特库
            </p>
            <h2 className="type-card-title mt-0.5 ">
              模特库
            </h2>
            <p className="type-page-subtitle mt-0.5 hidden max-w-md truncate sm:block">
              {hasItems
                ? "浏览预设、收藏与生成的模特"
                : "进入并新建你的第一个模特"}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <div className="text-right">
            <p className="hidden type-caption text-[var(--fg-2)] sm:block">
              Models
            </p>
            <p className="type-metric ">
              {String(total).padStart(2, "0")}
            </p>
          </div>
          <span
            aria-hidden
            className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-[var(--border-subtle)] text-[var(--fg-2)] transition-all duration-[var(--dur-base)] group-hover:border-accent-border group-hover:bg-[var(--accent-soft)] group-hover:text-accent"
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
  const dimension = size === "md" ? 40 : 32;
  const cls =
    size === "md"
      ? "h-10 w-10 ring-2 ring-[var(--bg-0)]"
      : "h-8 w-8 ring-2 ring-[var(--bg-0)]";
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
        <Users className="absolute inset-0 m-auto h-3.5 w-3.5 text-[var(--fg-2)]" />
      )}
    </span>
  );
}

function PlaceholderAvatar({ size }: { size: "sm" | "md" }) {
  const cls =
    size === "md"
      ? "h-10 w-10 ring-2 ring-[var(--bg-0)]"
      : "h-8 w-8 ring-2 ring-[var(--bg-0)]";
  return (
    <span
      aria-hidden
      className={cn(
        "relative inline-flex items-center justify-center rounded-full bg-[var(--bg-2)]",
        cls,
      )}
    >
      <Users className="h-3.5 w-3.5 text-[var(--fg-3)]" />
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
    <section className="toolbar-shell lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(0,300px)] lg:items-end">
      <div className="toolbar-group flex-wrap">
        {FILTERS.map((option) => {
          const active = filter === option.key;
          const count = counts[option.key];
          return (
            <button
              key={option.key}
              type="button"
              onClick={() => onFilterChange(option.key)}
              className={cn(
                "group relative inline-flex min-h-9 shrink-0 cursor-pointer items-baseline gap-1.5 px-2.5 py-1.5 type-body-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:shadow-[var(--ring)]",
                active ? "text-[var(--fg-0)]" : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
              )}
            >
              <span>{option.label}</span>
              <span className="type-caption tabular-nums opacity-60">
                {count}
              </span>
              <span
                aria-hidden
                className={cn(
                  "absolute inset-x-3 bottom-1 h-px transition-all duration-[var(--dur-base)]",
                  active
                    ? "bg-accent"
                    : "bg-transparent group-hover:bg-[var(--border-strong)]",
                )}
              />
            </button>
          );
        })}
      </div>
      <div className="relative min-w-0 flex-1 lg:max-w-[300px]">
        <Search className="pointer-events-none absolute left-0 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]" />
        <input
          value={keyword}
          onChange={(event) => onKeywordChange(event.target.value)}
          placeholder="搜索标题或基础需求"
          className="control-shell h-10 w-full pl-7 pr-9 type-body text-[var(--fg-0)] outline-none transition-[border-color,box-shadow] placeholder:text-[var(--fg-2)] focus:border-accent-border focus:shadow-[var(--ring)] md:h-9"
          aria-label="搜索项目"
        />
        {keyword ? (
          <button
            type="button"
            onClick={() => onKeywordChange("")}
            aria-label="清除搜索"
            className="absolute right-0 top-1/2 inline-flex h-11 w-11 -translate-y-1/2 cursor-pointer items-center justify-center text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)] md:h-8 md:w-8"
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
    <section className="grid grid-cols-1 gap-x-4 gap-y-7 min-[390px]:grid-cols-2 md:gap-x-5 md:gap-y-9 lg:grid-cols-3 xl:grid-cols-4">
      {items.map((item, index) => (
        <ProjectCard key={item.id} item={item} order={index} />
      ))}
    </section>
  );
}

function projectStatusDotTone(status: WorkflowRunListItem["status"]) {
  if (status === "running") {
    return "bg-accent animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]";
  }
  if (status === "needs_review") return "bg-accent";
  if (status === "completed") return "bg-[var(--success)]";
  if (status === "failed") return "bg-[var(--danger)]";
  return "bg-[var(--fg-3)]";
}

function ProjectCardMedia({
  item,
  order,
  thumb,
  running,
}: {
  item: WorkflowRunListItem;
  order: number;
  thumb: string;
  running: boolean;
}) {
  return (
    <div className="relative aspect-[3/4] overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)]">
      {thumb ? (
        <Image
          src={thumb}
          alt={item.title || "商品图"}
          fill
          sizes="(max-width: 640px) 100vw, (max-width: 1024px) 50vw, 25vw"
          unoptimized
          className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
        />
      ) : (
        <div className="flex h-full items-center justify-center type-caption text-[var(--fg-3)]">
          暂无图片
        </div>
      )}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-gradient-to-t from-[var(--media-control-bg)] via-transparent to-transparent opacity-0 transition-opacity duration-[var(--dur-base)] group-hover:opacity-100"
      />
      <span className="type-caption absolute left-3 top-3 inline-flex max-w-[calc(100%-5rem)] items-center gap-2 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-2 py-1 text-[var(--media-control-fg)]">
        N°{String(order + 1).padStart(2, "0")}
      </span>
      {running ? (
        <span className="type-caption absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-2 py-1 text-accent">
          <span className="relative flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
          </span>
          运行中
        </span>
      ) : null}
    </div>
  );
}

// Portrait 杂志卡：大图 + 下方元数据 + hover micro scale
function ProjectCard({ item, order }: { item: WorkflowRunListItem; order: number }) {
  const running = item.status === "running";
  const thumb = productThumbSrc(item);
  const reduceMotion = useReducedMotion();
  const isMobile = useIsMobile();
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [title, setTitle] = useState(item.title || "服饰模特图");
  const menuRef = useRef<HTMLDivElement | null>(null);
  const actionButtonRef = useRef<HTMLButtonElement | null>(null);
  const closeMenu = useCallback(() => {
    setMenuOpen(false);
    setConfirmingDelete(false);
    setRenaming(false);
  }, []);
  const cancelRename = useCallback(() => {
    setTitle(item.title || "服饰模特图");
    setRenaming(false);
  }, [item.title]);
  const patch = usePatchWorkflowMutation({
    onSuccess: () => {
      toast.success("项目已重命名");
      closeMenu();
    },
    onError: (error) => toast.error(error.message || "重命名失败"),
  });
  const remove = useDeleteWorkflowMutation({
    onSuccess: () => {
      toast.success("项目已删除");
      closeMenu();
    },
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

  const dotTone = projectStatusDotTone(item.status);

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
        <ProjectCardMedia
          item={item}
          order={order}
          thumb={thumb}
          running={running}
        />

        <div className="mt-3 flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <p className="line-clamp-2 type-body font-medium leading-[1.35] text-[var(--fg-0)] transition-colors duration-[var(--dur-base)] group-hover:text-accent">
              {item.title || "服饰模特图"}
            </p>
            <p className="mt-1.5 flex items-center gap-2 type-caption text-[var(--fg-2)]">
              <span aria-hidden className={cn("inline-block h-1.5 w-1.5 rounded-full", dotTone)} />
              <span className="truncate">{STATUS_LABEL[item.status] ?? item.status}</span>
              <span aria-hidden className="text-[var(--fg-3)]">·</span>
              <span className="truncate">{formatRelativeTime(item.updated_at)}</span>
            </p>
            {item.next_action ? (
              <p className="mt-1.5 line-clamp-1 type-caption text-accent">
                {item.next_action}
              </p>
            ) : null}
          </div>
        </div>
      </Link>

      <MediaControlButton
        ref={actionButtonRef}
        size="sm"
        aria-label="项目操作"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={openMenu}
        className="absolute right-1 top-1 opacity-100 md:opacity-0 md:group-hover:opacity-100"
      >
        <MoreHorizontal className="h-4 w-4" />
      </MediaControlButton>

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
                className="control-shell h-10 px-3 type-body-sm text-[var(--fg-0)] outline-none focus:border-accent-border focus:shadow-[var(--ring)]"
              />
              <div className="flex justify-end gap-2">
                <Button type="button" variant="ghost" size="sm" onClick={cancelRename}>
                  取消
                </Button>
                <Button type="submit" size="sm" disabled={patch.isPending}>
                  保存
                </Button>
              </div>
            </form>
          ) : confirmingDelete ? (
            <div className="grid gap-2 p-1.5">
              <p className="type-body-sm text-[var(--fg-0)]">确认删除这个项目？</p>
              <p className="type-caption leading-5 text-[var(--fg-2)]">
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
                className="flex min-h-9 cursor-pointer items-center gap-2.5 px-2 text-left type-body-sm text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]"
              >
                <Pencil className="h-3.5 w-3.5" />
                重命名
              </button>
              <button
                type="button"
                onClick={() => setConfirmingDelete(true)}
                role="menuitem"
                className="flex min-h-9 cursor-pointer items-center gap-2.5 px-2 text-left type-body-sm text-[var(--danger)] transition-colors hover:bg-[var(--danger-soft)]"
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
            onCancelRename={cancelRename}
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
