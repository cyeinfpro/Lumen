"use client";

import { Eraser, RefreshCw } from "lucide-react";
import { useMemo } from "react";

import { Spinner } from "@/components/ui/primitives/Spinner";
import { toast } from "@/components/ui/primitives/Toast";
import type { ApparelModelLibraryJob } from "@/lib/apiClient";
import {
  useApparelModelLibraryJobsInfiniteQuery,
  useClearApparelModelLibraryJobsMutation,
} from "@/lib/queries";
import { cn } from "@/lib/utils";
import {
  EmptyJobs,
  EmptyLine,
  FinishedJobCard,
  RunningJobCard,
  Section,
  Stat,
} from "./ModelLibraryJobViews";

export function ModelLibraryJobsPanel() {
  const jobs = useApparelModelLibraryJobsInfiniteQuery({ limit: 30 });
  const items = useMemo(
    () => jobs.data?.pages.flatMap((page) => page.items) ?? [],
    [jobs.data?.pages],
  );
  const clearJobs = useClearApparelModelLibraryJobsMutation({
    onSuccess: (result) =>
      toast.success("已清理生成任务", {
        description: `清理 ${result.deleted} 条历史任务`,
      }),
    onError: (err) =>
      toast.error("清理失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  const { running, finished } = useMemo(() => {
    const r: ApparelModelLibraryJob[] = [];
    const f: ApparelModelLibraryJob[] = [];
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
            <p className="type-page-kicker">
              任务中心
            </p>
            <h2 className="type-page-title mt-1.5 ">
              任务中心
            </h2>
            <p className="type-page-subtitle mt-2 max-w-xl">
              独立生成与项目候选的统一进度跟踪
            </p>
          </div>
          <div className="grid w-full grid-cols-2 gap-2 self-start min-[420px]:flex min-[420px]:w-auto min-[420px]:flex-wrap min-[420px]:items-center md:self-end">
            <button
              type="button"
              aria-label="清理已完成任务"
              onClick={() => clearJobs.mutate()}
              disabled={clearJobs.isPending || (finished.length === 0 && !jobs.hasNextPage)}
              className={cn(
                "inline-flex min-h-11 items-center justify-center gap-2 rounded-full border border-[var(--border)] px-2.5 type-caption text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] disabled:cursor-not-allowed disabled:opacity-50 min-[420px]:h-8 min-[420px]:min-h-0 ",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:shadow-[var(--ring)]",
              )}
            >
              {clearJobs.isPending ? (
                <Spinner size={12} />
              ) : (
                <Eraser className="h-3.5 w-3.5" />
              )}
              <span>清理已完成</span>
            </button>
            <button
              type="button"
              aria-label="手动刷新"
              onClick={() => jobs.refetch()}
              disabled={jobs.isFetching}
              className={cn(
                "inline-flex min-h-11 items-center justify-center gap-2 rounded-full border border-[var(--border)] px-2.5 type-caption text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] disabled:cursor-not-allowed disabled:opacity-60 min-[420px]:h-8 min-[420px]:min-h-0 ",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:shadow-[var(--ring)]",
              )}
            >
              <RefreshCw className={cn("h-3.5 w-3.5", jobs.isFetching && "animate-spin")} />
              <span>刷新</span>
            </button>
          </div>
        </div>
        {!jobs.isPending && items.length > 0 ? (
          <div className="mt-3 grid grid-cols-3 gap-px overflow-hidden border border-[var(--border)] md:max-w-xl">
            <Stat label="已加载" value={items.length} />
            <Stat label="进行中" value={running.length} accent={running.length > 0} />
            <Stat label="完成" value={finished.length} />
          </div>
        ) : null}
      </header>

      {jobs.isPending ? (
        <div className="flex h-40 items-center justify-center gap-2 type-caption text-[var(--fg-2)]">
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
                  <RunningJobCard key={job.job_id} job={job} />
                ))}
              </div>
            )}
          </Section>

          <Section title="已完成 / 失败" eyebrow="归档" count={finished.length}>
            {finished.length === 0 ? (
              <EmptyLine label="还没有已完成的任务" />
            ) : (
              <div className="grid gap-4">
                {finished.map((job) => (
                  <FinishedJobCard key={job.job_id} job={job} />
                ))}
              </div>
            )}
          </Section>
          {jobs.hasNextPage ? (
            <button
              type="button"
              onClick={() => jobs.fetchNextPage()}
              disabled={jobs.isFetchingNextPage}
              className={cn(
                "mx-auto inline-flex min-h-11 items-center gap-2 rounded-full border border-[var(--border)] px-4 type-caption text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] disabled:cursor-not-allowed disabled:opacity-60 md:h-9 md:min-h-0",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:shadow-[var(--ring)]",
              )}
            >
              {jobs.isFetchingNextPage ? <Spinner size={12} /> : null}
              加载更多历史任务
            </button>
          ) : null}
        </>
      )}
    </div>
  );
}
