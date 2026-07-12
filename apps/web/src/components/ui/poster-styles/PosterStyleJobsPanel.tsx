"use client";

// 海报风格库任务中心（蓝本：ModelLibraryJobsPanel）。
// 列出当前用户的 poster_style_library_generate 任务：
// - 进行中（queued / running）：进度条
// - 已完成 / 失败 / 部分成功：终态
//
// 任务来源于隐藏的 hidden workflow_run。每个任务可能携带一个 saved_item_id
// （= worker 完成后入库的 PosterStyleItem）；点击 → 滚到对应卡片或打开详情。

import { motion } from "framer-motion";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Library,
  RefreshCw,
} from "lucide-react";
import { useMemo } from "react";

import { Spinner } from "@/components/ui/primitives/Spinner";
import {
  POSTER_STYLE_CATEGORY_LABEL,
  type PosterStyleJobOut,
  type PosterStyleJobStatus,
} from "@/lib/apiClient";
import { usePosterStyleJobsQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { formatRelativeTime } from "../projects/utils";

const STATUS_LABEL: Record<PosterStyleJobStatus, string> = {
  queued: "排队中",
  running: "生成中",
  succeeded: "已完成",
  failed: "失败",
  partial: "部分成功",
};

export interface PosterStyleJobsPanelProps {
  /** 点击"查看入库条目"时由父组件处理（通常是切到 browse tab + 高亮） */
  onOpenItem?: (itemId: string) => void;
}

export function PosterStyleJobsPanel({ onOpenItem }: PosterStyleJobsPanelProps) {
  const jobs = usePosterStyleJobsQuery({ limit: 50 });
  const items = useMemo(() => jobs.data?.items ?? [], [jobs.data?.items]);

  const { running, finished } = useMemo(() => {
    const r: PosterStyleJobOut[] = [];
    const f: PosterStyleJobOut[] = [];
    for (const job of items) {
      if (job.status === "queued" || job.status === "running") r.push(job);
      else f.push(job);
    }
    return { running: r, finished: f };
  }, [items]);

  return (
    <div className="grid gap-4">
      <header className="border-b border-[var(--border)] pb-3">
        <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="min-w-0 flex-1">
            <p className="type-page-kicker">任务中心</p>
            <h2 className="type-page-title mt-1.5 md:text-[28px]">任务中心</h2>
            <p className="type-page-subtitle mt-2 max-w-xl">
              风格库生成任务的进度跟踪
            </p>
          </div>
          <div className="grid w-full grid-cols-1 gap-2 self-start min-[420px]:flex min-[420px]:w-auto min-[420px]:items-center md:self-end">
            <button
              type="button"
              aria-label="手动刷新"
              onClick={() => jobs.refetch()}
              disabled={jobs.isFetching}
              className={cn(
                "inline-flex min-h-11 items-center justify-center gap-2 rounded-full border border-[var(--border)] px-2.5 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] disabled:cursor-not-allowed disabled:opacity-60 min-[420px]:h-8 min-[420px]:min-h-0 min-[420px]:tracking-[0.16em]",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
              )}
            >
              <RefreshCw
                className={cn(
                  "h-3.5 w-3.5",
                  jobs.isFetching && "animate-spin",
                )}
              />
              <span>刷新</span>
            </button>
          </div>
        </div>
        {!jobs.isPending && items.length > 0 ? (
          <div className="mt-3 grid grid-cols-3 gap-px overflow-hidden border border-[var(--border)] md:max-w-xl">
            <Stat label="已加载" value={items.length} />
            <Stat
              label="进行中"
              value={running.length}
              accent={running.length > 0}
            />
            <Stat label="完成" value={finished.length} />
          </div>
        ) : null}
      </header>

      {jobs.isPending ? (
        <div className="flex h-40 items-center justify-center gap-2 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          <Spinner size={20} />
          加载中
        </div>
      ) : items.length === 0 ? (
        <EmptyJobs />
      ) : (
        <>
          <Section title="进行中" eyebrow="进行中" count={running.length}>
            {running.length === 0 ? (
              <EmptyLine label="目前没有进行中的任务" />
            ) : (
              <div className="grid gap-4">
                {running.map((job) => (
                  <JobCard key={job.job_id} job={job} onOpenItem={onOpenItem} />
                ))}
              </div>
            )}
          </Section>
          <Section
            title="已完成 / 失败"
            eyebrow="归档"
            count={finished.length}
          >
            {finished.length === 0 ? (
              <EmptyLine label="还没有已完成的任务" />
            ) : (
              <div className="grid gap-4">
                {finished.map((job) => (
                  <JobCard key={job.job_id} job={job} onOpenItem={onOpenItem} />
                ))}
              </div>
            )}
          </Section>
        </>
      )}
    </div>
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
    <div className="bg-[var(--bg-0)] px-3 py-3 md:px-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        {label}
      </p>
      <p
        className={cn(
          "type-metric mt-1 md:text-[22px]",
          accent ? "text-[var(--amber-300)]" : "text-[var(--fg-0)]",
        )}
      >
        {String(value).padStart(2, "0")}
      </p>
    </div>
  );
}

function Section({
  title,
  eyebrow,
  count,
  children,
}: {
  title: string;
  eyebrow: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="grid gap-3">
      <div className="flex items-baseline gap-3 border-t border-[var(--border)] pt-3">
        <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          {eyebrow}
        </span>
        <h3 className="text-[16px] font-semibold leading-tight text-[var(--fg-0)] md:text-[18px]">
          {title}
        </h3>
        <span className="font-mono text-[11px] tabular-nums text-[var(--fg-2)]">
          {String(count).padStart(2, "0")}
        </span>
      </div>
      {children}
    </section>
  );
}

function EmptyLine({ label }: { label: string }) {
  return (
    <p className="border-y border-[var(--border)] py-8 text-center font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
      {label}
    </p>
  );
}

function JobCard({
  job,
  onOpenItem,
}: {
  job: PosterStyleJobOut;
  onOpenItem?: (itemId: string) => void;
}) {
  const isRunning = job.status === "queued" || job.status === "running";
  const progress =
    job.requested_count > 0
      ? Math.min(
          100,
          Math.round((job.finished_count / job.requested_count) * 100),
        )
      : 0;

  return (
    <motion.article
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className="grid gap-3 border-t border-[var(--border)] pt-5"
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            <StatusBadge status={job.status} />
            <span aria-hidden className="text-[var(--fg-3)]">
              ·
            </span>
            <span>{POSTER_STYLE_CATEGORY_LABEL[job.category]}</span>
          </div>
          <p className="mt-2 min-w-0 truncate text-[14px] font-medium text-[var(--fg-0)]">
            {job.title || "未命名风格"}
          </p>
          <p className="mt-1.5 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
            <span className="tabular-nums text-[var(--fg-1)]">
              {job.finished_count}
            </span>
            <span className="mx-1 text-[var(--fg-3)]">/</span>
            <span className="tabular-nums">{job.requested_count}</span>
            <span aria-hidden className="mx-2 text-[var(--fg-3)]">
              ·
            </span>
            {formatRelativeTime(job.updated_at ?? job.created_at)}
          </p>
        </div>
        {job.saved_item_id && onOpenItem ? (
          <button
            type="button"
            onClick={() => onOpenItem(job.saved_item_id!)}
            className="inline-flex min-h-11 items-center gap-1.5 border border-[var(--border)] px-2.5 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--amber-300)] md:h-8 md:min-h-0"
          >
            查看入库
            <ArrowRight className="h-3 w-3" />
          </button>
        ) : null}
      </header>

      {job.error_message ? (
        <p role="alert" className="max-w-xl text-[12px] leading-[1.6] text-[var(--danger)]">
          {job.error_message}
        </p>
      ) : null}
      {job.prompt ? (
        <p className="line-clamp-2 max-w-xl break-words text-[12px] leading-[1.6] text-[var(--fg-2)]">
          {job.prompt}
        </p>
      ) : null}
      {isRunning ? <ProgressBar value={progress} /> : null}

      {job.style_tags.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {job.style_tags.map((tag) => (
            <span
              key={tag}
              className="inline-flex max-w-full items-center break-words border border-[var(--border)] px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-1)] min-[390px]:tracking-[0.14em]"
            >
              {tag}
            </span>
          ))}
        </div>
      ) : null}
    </motion.article>
  );
}

function StatusBadge({ status }: { status: PosterStyleJobStatus }) {
  const dot =
    status === "queued"
      ? "bg-[var(--fg-3)]"
      : status === "running"
        ? "bg-[var(--amber-400)] animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]"
        : status === "succeeded"
          ? "bg-[var(--success)]"
          : status === "failed"
            ? "bg-[var(--danger)]"
            : "bg-[var(--amber-300)]";
  const tone =
    status === "running" ||
    status === "succeeded" ||
    status === "failed" ||
    status === "partial"
      ? "text-[var(--fg-1)]"
      : "text-[var(--fg-2)]";
  return (
    <span className={cn("inline-flex items-center gap-1.5", tone)}>
      {status === "running" ? (
        <Spinner size={12} />
      ) : status === "succeeded" ? (
        <CheckCircle2 className="h-3 w-3 text-[var(--success)]" />
      ) : status === "failed" || status === "partial" ? (
        <AlertTriangle className="h-3 w-3 text-[var(--danger)]" />
      ) : (
        <span
          aria-hidden
          className={cn("inline-block h-1.5 w-1.5 rounded-full", dot)}
        />
      )}
      {STATUS_LABEL[status]}
    </span>
  );
}

function ProgressBar({ value }: { value: number }) {
  return (
    <div className="grid gap-1.5">
      <div className="h-px overflow-hidden bg-[var(--border)]">
        <div
          className="h-full bg-[var(--amber-400)] transition-[width] duration-300"
          style={{ width: `${value}%` }}
        />
      </div>
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] tabular-nums text-[var(--fg-2)]">
        {String(value).padStart(2, "0")}%
      </p>
    </div>
  );
}

function EmptyJobs() {
  return (
    <section className="border-y border-[var(--border)] py-14 md:py-16">
      <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
        <div>
          <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--amber-300)]">
            <Library className="mr-1.5 -mt-px inline-block h-3 w-3" />
            空队列
          </p>
          <h4 className="type-page-title mt-3 md:text-[28px]">还没有任务</h4>
          <p className="type-body mt-3 max-w-xl">
            {`从"新建风格"提交一次 prompt，就会派发风格生成任务，进度会实时聚合到这里。`}
          </p>
        </div>
      </div>
    </section>
  );
}
