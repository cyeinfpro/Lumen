"use client";

// 项目列表页：
// 1) 搜索（标题 / user_prompt 模糊匹配，本地过滤即可）
// 2) 状态筛选 chips（全部 / 进行中 / 待确认 / 已完成 / 需要处理）+ 计数
// 3) ProjectCard 加 layout/staggered 进入动画 + amber-glow on running
// 4) 错误态可重试；空态升级为带占位渐变的 hero

import { motion } from "framer-motion";
import { ChevronRight, Image as ImageIcon, Plus, RefreshCw, Search, Shirt, Sparkles, X } from "lucide-react";
import Link from "next/link";
import { useDeferredValue, useMemo, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { EmptyState } from "@/components/ui/primitives/EmptyState";
import { Skeleton } from "@/components/ui/primitives/Skeleton";
import { useWorkflowsQuery } from "@/lib/queries";
import type { WorkflowRunListItem } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ProjectTopBar } from "./components/ProjectTopBar";
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

  return (
    <div className="flex min-h-[100dvh] flex-col bg-[var(--bg-0)]">
      <OnlineBanner />
      <ProjectTopBar />
      <main className="flex-1 overflow-y-auto px-4 py-5 md:px-8">
        <div className="mx-auto grid w-full max-w-[1400px] gap-5">
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
              className="rounded-md border border-[var(--border)] bg-white/[0.03] py-14"
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
    </div>
  );
}

function Hero({ counts }: { counts: Record<FilterKey, number> }) {
  return (
    <section className="grid gap-3 md:grid-cols-[1fr_auto] md:items-end">
      <div>
        <p className="text-[11px] tracking-[0.16em] text-[var(--fg-2)]">
          STRUCTURED WORKFLOW
        </p>
        <h1 className="mt-1 text-[26px] font-semibold tracking-normal text-[var(--fg-0)] md:text-[32px]">
          项目
        </h1>
        <p className="mt-2 text-sm text-[var(--fg-2)]">
          {counts.all > 0
            ? `共 ${counts.all} 个 · 进行中 ${counts.running} · 待确认 ${counts.needs_review}`
            : "结构化八阶段工作流，让服饰电商展示图从草稿到交付一气呵成。"}
        </p>
      </div>
      <Link
        href="/projects/new"
        className="inline-flex h-10 items-center justify-center gap-2 rounded-md bg-[var(--accent)] px-4 text-sm font-medium text-black shadow-[var(--shadow-amber)] transition-transform duration-150 hover:scale-[1.02] active:scale-[0.98]"
      >
        <Plus className="h-4 w-4" />
        新建项目
      </Link>
    </section>
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
    <section className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,280px)] lg:items-center">
      <div className="-mx-1 flex flex-wrap gap-1.5 px-1">
        {FILTERS.map((option) => {
          const active = filter === option.key;
          const count = counts[option.key];
          return (
            <button
              key={option.key}
              type="button"
              onClick={() => onFilterChange(option.key)}
              className={cn(
                "inline-flex h-9 items-center gap-1.5 rounded-full border px-3 text-xs transition-colors",
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
          className="h-10 w-full rounded-md border border-[var(--border)] bg-[var(--bg-1)] pl-9 pr-9 text-sm outline-none transition-colors focus:border-[var(--border-amber)]"
          aria-label="搜索项目"
        />
        {keyword ? (
          <button
            type="button"
            onClick={() => onKeywordChange("")}
            aria-label="清除搜索"
            className="absolute right-2 top-1/2 inline-flex h-6 w-6 -translate-y-1/2 items-center justify-center rounded-md text-[var(--fg-2)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)]"
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
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        duration: 0.28,
        ease: [0.22, 1, 0.36, 1],
        delay: Math.min(order * 0.04, 0.32),
      }}
    >
      <Link
        href={`/projects/${item.id}`}
        className={cn(
          "group grid min-h-[140px] grid-cols-[100px_1fr] gap-3 rounded-md border bg-white/[0.035] p-3 transition-all duration-[var(--dur-base)]",
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
        <div className="relative flex aspect-[4/5] items-center justify-center overflow-hidden rounded-md bg-[var(--bg-2)]">
          {productThumbSrc(item) ? (
            <img
              src={productThumbSrc(item)}
              alt={item.title || "商品图"}
              loading="lazy"
              className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] group-hover:scale-[1.04]"
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
        <div className="min-w-0">
          <div className="flex items-center justify-between gap-2">
            <p className="truncate text-sm font-medium text-[var(--fg-0)]">
              {item.title || "服饰模特展示图"}
            </p>
            <ChevronRight className="h-4 w-4 shrink-0 text-[var(--fg-2)] transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-[var(--fg-0)]" />
          </div>
          <p className="mt-1 line-clamp-2 text-xs leading-5 text-[var(--fg-2)]">
            {item.user_prompt || "未填写基础需求"}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-1.5">
            <StatusBadge status={item.status} needsReview={needsReview} completed={completed} />
            <Chip>{item.current_step}</Chip>
            <Chip>{item.output_count} 张产出</Chip>
          </div>
          <div className="mt-3 flex items-center justify-between gap-2">
            <p className="line-clamp-1 text-xs text-[var(--amber-300)]">{item.next_action}</p>
            <p className="shrink-0 text-[11px] text-[var(--fg-2)]">
              {formatRelativeTime(item.updated_at)}
            </p>
          </div>
        </div>
      </Link>
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
          className="grid min-h-[140px] grid-cols-[100px_1fr] gap-3 rounded-md border border-[var(--border)] bg-white/[0.025] p-3"
        >
          <Skeleton className="aspect-[4/5] w-full" />
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
    <div className="rounded-md border border-[var(--danger)]/30 bg-[var(--danger-soft)] p-5 text-sm">
      <h3 className="text-base font-medium text-[var(--fg-0)]">项目加载失败</h3>
      <p className="mt-1 text-xs text-[var(--fg-1)]">网络错误或服务繁忙，请稍后重试。</p>
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
    <section className="relative overflow-hidden rounded-md border border-[var(--border)] bg-gradient-to-br from-[var(--bg-1)] via-[var(--bg-1)] to-[var(--accent-soft)] p-6 md:p-10">
      <span
        aria-hidden
        className="pointer-events-none absolute -right-12 -top-12 h-44 w-44 rounded-full bg-[var(--accent)]/14 blur-3xl"
      />
      <span
        aria-hidden
        className="pointer-events-none absolute -bottom-12 left-1/2 h-32 w-32 -translate-x-1/2 rounded-full bg-[var(--amber-glow)] blur-2xl"
      />
      <div className="relative grid gap-4 md:grid-cols-[1fr_auto] md:items-center">
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
          className="inline-flex h-11 items-center justify-center gap-2 rounded-md bg-[var(--accent)] px-5 text-[15px] font-medium text-black shadow-[var(--shadow-amber)] transition-transform duration-150 hover:scale-[1.02] active:scale-[0.98]"
        >
          <Shirt className="h-4 w-4" />
          创建第一个项目
        </Link>
      </div>
    </section>
  );
}

function TemplateBand({ compact = false }: { compact?: boolean }) {
  return (
    <section className="grid gap-3 rounded-md border border-[var(--border)] bg-white/[0.035] p-4 md:grid-cols-[1fr_auto] md:items-center">
      <div>
        <h2 className="text-base font-medium text-[var(--fg-0)]">服饰模特展示图</h2>
        <p className="mt-1 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
          上传 1 到 3 张商品图，先确认合成模特，再生成 4 张电商展示图并查看质检。
        </p>
      </div>
      <Link
        href="/projects/apparel-model-showcase/new"
        className={cn(
          "inline-flex items-center justify-center gap-2 rounded-md bg-[var(--accent)] px-4 font-medium text-black shadow-[var(--shadow-amber)] transition-transform duration-150 hover:scale-[1.02] active:scale-[0.98]",
          compact ? "h-9 text-sm" : "h-11 text-[15px]",
        )}
      >
        <Shirt className="h-4 w-4" />
        创建项目
      </Link>
    </section>
  );
}
