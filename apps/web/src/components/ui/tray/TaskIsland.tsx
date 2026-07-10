"use client";

import { useQuery } from "@tanstack/react-query";
import { ListChecks } from "lucide-react";
import { useEffect, useMemo } from "react";

import { listTasks } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";

export function TaskIsland({
  compact = false,
  className,
}: {
  compact?: boolean;
  className?: string;
}) {
  const generations = useChatStore((state) => state.generations);
  const userId = useChatStore((state) => state.currentUserId);
  const setTaskTrayMinimized = useUiStore(
    (state) => state.setTaskTrayMinimized,
  );
  const setTaskIslandMounted = useUiStore(
    (state) => state.setTaskIslandMounted,
  );

  useEffect(() => {
    setTaskIslandMounted(true);
    return () => setTaskIslandMounted(false);
  }, [setTaskIslandMounted]);

  const activeQuery = useQuery({
    queryKey: ["tasks", "island", "active"],
    queryFn: ({ signal }) =>
      listTasks({ status: "active", limit: 24 }, { signal }),
    enabled: Boolean(userId),
    staleTime: 5_000,
    refetchInterval: 8_000,
  });
  const recentQuery = useQuery({
    queryKey: ["tasks", "island", "recent"],
    queryFn: ({ signal }) => listTasks({ limit: 1 }, { signal }),
    enabled: Boolean(userId),
    staleTime: 15_000,
  });

  const summary = useMemo(() => {
    const statuses = new Map<string, string>();
    for (const generation of Object.values(generations)) {
      if (generation.status !== "queued" && generation.status !== "running") {
        continue;
      }
      statuses.set(generation.id, generation.status);
    }
    for (const task of activeQuery.data?.items ?? []) {
      statuses.set(task.id, task.status);
    }
    return {
      count: statuses.size,
      queued: Array.from(statuses.values()).filter(
        (status) => status === "queued",
      ).length,
      hasRecent: (recentQuery.data?.items.length ?? 0) > 0,
    };
  }, [activeQuery.data?.items, generations, recentQuery.data?.items.length]);

  if (summary.count === 0 && !summary.hasRecent) return null;

  const label =
    summary.count === 0
      ? "任务"
      : summary.queued > 0
      ? `${summary.count} 个任务 · ${summary.queued} 个排队`
      : `${summary.count} 个任务进行中`;

  return (
    <button
      type="button"
      data-task-island
      onClick={() => setTaskTrayMinimized(false)}
      aria-label={`${label}，打开任务中心`}
      className={cn(
        "inline-flex min-h-10 shrink-0 items-center gap-2 rounded-[var(--radius-control)]",
        "border border-[var(--border)] bg-[var(--bg-1)] px-3 text-[12px] font-medium",
        "text-[var(--fg-1)] shadow-[var(--shadow-1)] transition-[background-color,border-color,color] duration-[var(--dur-quick)]",
        "hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
        "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
        compact && "min-h-9 px-2.5",
        className,
      )}
    >
      <span className="relative inline-flex h-5 w-5 items-center justify-center">
        <ListChecks className="h-4 w-4" aria-hidden />
        {summary.count > 0 ? (
          <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-[var(--accent)]" />
        ) : null}
      </span>
      <span className={cn("whitespace-nowrap", compact && "hidden 2xl:inline")}>
        {label}
      </span>
    </button>
  );
}
