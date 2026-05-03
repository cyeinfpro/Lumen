"use client";

// 项目列表页：
// 1) 搜索（标题 / user_prompt 模糊匹配，本地过滤即可）
// 2) 状态筛选 chips（全部 / 进行中 / 待确认 / 已完成 / 需要处理）+ 计数
// 3) ProjectCard 加 layout/staggered 进入动画 + amber-glow on running
// 4) 错误态可重试；空态升级为带占位渐变的 hero

import { motion, useReducedMotion } from "framer-motion";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  Clock3,
  FolderKanban,
  Image as ImageIcon,
  MoreVertical,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Shirt,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import Link from "next/link";
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { EmptyState } from "@/components/ui/primitives/EmptyState";
import { Skeleton } from "@/components/ui/primitives/Skeleton";
import { toast } from "@/components/ui/primitives/Toast";
import { useDeleteWorkflowMutation, usePatchWorkflowMutation, useWorkflowsQuery } from "@/lib/queries";
import type { WorkflowRunListItem } from "@/lib/apiClient";
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
      : "服饰展示工作流";

  return (
    <div className="relative flex h-[100dvh] w-full min-w-0 flex-col bg-[var(--bg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="项目"
        subtitle={mobileSubtitle}
        right={
          <Link
            href="/projects/new"
            aria-label="新建项目"
            className="inline-flex h-9 w-9 items-center justify-center rounded-full bg-[var(--accent)] text-black shadow-[var(--shadow-amber)] active:scale-[0.96]"
          >
            <Plus className="h-4.5 w-4.5" />
          </Link>
        }
      />
      <ProjectTopBar />
      <main className="mb-[calc(56px+env(safe-area-inset-bottom,0px))] flex-1 overflow-y-auto overscroll-contain px-3 pb-4 pt-3 md:mb-0 md:px-8 md:py-5">
        <div className="mx-auto grid w-full max-w-[1400px] gap-4 md:gap-5">
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

          <TemplateBand compact />
        </div>
      </main>
      <ProjectMobileTabBar />
    </div>
  );
}

function Hero({ counts }: { counts: Record<FilterKey, number> }) {
  const active = counts.running + counts.needs_review + counts.attention;
  return (
    <section className="grid gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/78 p-4 shadow-[var(--shadow-1)] md:grid-cols-[minmax(0,1fr)_auto] md:items-end md:rounded-md md:bg-white/[0.035] md:p-5">
      <div className="min-w-0">
        <p className="flex items-center gap-2 text-[11px] font-medium tracking-[0.16em] text-[var(--fg-2)]">
          <FolderKanban className="h-3.5 w-3.5" />
          STRUCTURED WORKFLOW
        </p>
        <div className="mt-2 flex flex-wrap items-end gap-x-3 gap-y-1">
          <h1 className="text-[28px] font-semibold tracking-normal text-[var(--fg-0)] md:text-[34px]">
            项目
          </h1>
          {counts.all > 0 ? (
            <span className="pb-1 font-mono text-[11px] tracking-wider text-[var(--fg-2)]">
              {counts.all} TOTAL
            </span>
          ) : null}
        </div>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
          {counts.all > 0
            ? active > 0
              ? `${active} 个项目等待推进，${counts.completed} 个已交付。`
              : "所有项目都已收束，可以直接创建下一组展示图。"
            : "从商品图、模特候选到质检交付，集中管理服饰展示图工作流。"}
        </p>
        <div className="mt-4 grid grid-cols-3 gap-2 md:max-w-xl">
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
        href="/projects/new"
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
        "min-w-0 rounded-lg border px-3 py-2 md:rounded-md",
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
      <div className="mt-1 font-mono text-lg leading-none text-[var(--fg-0)]">
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
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [title, setTitle] = useState(item.title || "服饰模特展示图");
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

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (menuRef.current?.contains(event.target as Node)) return;
      if (actionButtonRef.current?.contains(event.target as Node)) return;
      setMenuOpen(false);
      setConfirmingDelete(false);
      setRenaming(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        setConfirmingDelete(false);
        setRenaming(false);
      }
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

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
              ease: [0.22, 1, 0.36, 1],
              delay: Math.min(order * 0.035, 0.24),
            }
      }
    >
      <Link
        href={`/projects/${item.id}`}
        className={cn(
          "group grid min-h-[160px] grid-cols-[112px_minmax(0,1fr)] gap-3 rounded-xl border bg-[var(--bg-1)]/72 p-2.5 pr-3 transition-[background-color,border-color,box-shadow] duration-[var(--dur-base)] md:min-h-[150px] md:grid-cols-[112px_minmax(0,1fr)] md:rounded-md md:bg-white/[0.035] md:p-3",
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
        <div className="relative flex aspect-[4/5] items-center justify-center overflow-hidden rounded-lg bg-[var(--bg-2)] md:rounded-md">
          {thumb ? (
            <img
              src={thumb}
              alt={item.title || "商品图"}
              loading="lazy"
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
              {item.title || "服饰模特展示图"}
            </p>
            <span className="hidden shrink-0 pt-0.5 md:inline-flex">
              <ChevronRight className="h-4 w-4 shrink-0 text-[var(--fg-2)] transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-[var(--fg-0)]" />
            </span>
          </div>
          <p className="mt-1 line-clamp-2 text-xs leading-5 text-[var(--fg-2)] md:line-clamp-2">
            {item.user_prompt || "未填写基础需求"}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-1.5">
            <StatusBadge status={item.status} needsReview={needsReview} completed={completed} />
            <Chip>{item.current_step}</Chip>
            <Chip>{item.output_count} 张产出</Chip>
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
        onClick={() => {
          setMenuOpen((open) => !open);
          setConfirmingDelete(false);
          setRenaming(false);
          setTitle(item.title || "服饰模特展示图");
        }}
        className="absolute right-2.5 top-2.5 inline-flex h-10 w-10 cursor-pointer items-center justify-center rounded-full border border-transparent text-[var(--fg-2)] transition-colors hover:border-[var(--border)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:right-3 md:top-3 md:h-8 md:w-8 md:rounded-md"
      >
        <MoreVertical className="h-4 w-4" />
      </button>
      {menuOpen ? (
        <div
          ref={menuRef}
          role="menu"
          className="absolute right-2.5 top-14 z-10 w-[min(18rem,calc(100vw-2rem))] rounded-lg border border-[var(--border)] bg-[var(--bg-1)] p-2 shadow-[var(--shadow-2)] md:right-3 md:top-12 md:w-64 md:rounded-md"
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
                className="h-11 rounded-md border border-[var(--border)] bg-[var(--bg-0)] px-3 text-[15px] text-[var(--fg-0)] outline-none focus:border-[var(--border-amber)] md:h-9 md:text-sm"
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
                className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md px-2 text-left text-sm text-[var(--fg-1)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)] md:min-h-9"
              >
                <Pencil className="h-4 w-4" />
                重命名
              </button>
              <button
                type="button"
                onClick={() => setConfirmingDelete(true)}
                role="menuitem"
                className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md px-2 text-left text-sm text-[var(--danger)] transition-colors hover:bg-[var(--danger-soft)] md:min-h-9"
              >
                <Trash2 className="h-4 w-4" />
                删除
              </button>
            </div>
          )}
        </div>
      ) : null}
    </motion.div>
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
          className="grid min-h-[160px] grid-cols-[112px_minmax(0,1fr)] gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/60 p-2.5 md:min-h-[150px] md:rounded-md md:bg-white/[0.025] md:p-3"
        >
          <Skeleton className="aspect-[4/5] w-full rounded-lg md:rounded-md" />
          <div className="space-y-2">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-3/4" />
            <div className="mt-2 flex gap-1.5">
              <Skeleton className="h-5 w-12" />
              <Skeleton className="h-5 w-16" />
              <Skeleton className="h-5 w-14" />
            </div>
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
    <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/78 p-5 shadow-[var(--shadow-1)] md:rounded-md md:p-8">
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
        {["商品理解", "模特确认", "质检交付"].map((label, index) => (
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
            <h2 className="text-base font-medium text-[var(--fg-0)]">服饰模特展示图</h2>
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
