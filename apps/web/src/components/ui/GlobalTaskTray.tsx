"use client";

// Lumen V1 全局任务托盘：
//  - 无任务：整个 tray 隐藏
//  - 有任务：任务按钮负责打开；展开态 = 可持久查看最近任务的 TaskCenter
//  - 移动端改为底部中间 full-width
//  - 入场/退场用 AnimatePresence
//
// 注意：本组件不维护任务生命周期；取消 / 重试只是发送 API 调用，store 的 SSE handler 会更新状态。

import { useCallback, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ListChecks } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { useUiStore } from "@/store/useUiStore";
import { useChatStore } from "@/store/useChatStore";
import { cancelTask, listTasks } from "@/lib/apiClient";
import { logWarn } from "@/lib/logger";
import type { Generation } from "@/lib/types";
import { cn } from "@/lib/utils";
import { SPRING } from "@/lib/motion";
import {
  userTaskQueryKeys,
  useUserQueryScope,
} from "@/components/QueryProvider";
import { TaskCenter } from "./tray/TaskCenter";
import { resolveGenerationRoute } from "./tray/taskCenterModel";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useModalLayer } from "./primitives/mobile/useModalLayer";
import { toast } from "./primitives";

type GenerationActionState = {
  busy: boolean;
  error: string | null;
};

function taskTrayRefetchInterval(minimized: boolean, hasActive: boolean) {
  if (minimized) return false;
  return hasActive ? 10_000 : 30_000;
}

function taskTrayBadge(activeCount: number, recentCount: number) {
  if (activeCount > 0) {
    return {
      count: activeCount,
      label: `进行中的任务：${activeCount}`,
      active: true,
    };
  }
  return {
    count: Math.min(recentCount, 99),
    label: `最近任务：${recentCount}`,
    active: false,
  };
}

export function GlobalTaskTray() {
  const router = useRouter();
  const taskTrayMinimized = useUiStore((s) => s.taskTray.minimized);
  const taskIslandMounted = useUiStore((s) => s.taskIslandMounted);
  const setTaskTrayMinimized = useUiStore((s) => s.setTaskTrayMinimized);
  const generations = useChatStore((s) => s.generations);
  const retryGeneration = useChatStore((s) => s.retryGeneration);
  const pollInflightTasks = useChatStore((s) => s.pollInflightTasks);
  const openLightbox = useUiStore((s) => s.openLightbox);
  const userScope = useUserQueryScope();
  const actionLocksRef = useRef(new Set<string>());
  const [generationActionStates, setGenerationActionStates] = useState<
    Record<string, GenerationActionState>
  >({});

  const active = useMemo(() => {
    const activeItems: Generation[] = [];
    for (const g of Object.values(generations)) {
      if (g.status === "queued" || g.status === "running") {
        activeItems.push(g);
      }
    }
    activeItems.sort((a, b) => b.started_at - a.started_at);
    return activeItems;
  }, [generations]);

  const hasActive = active.length > 0;
  const activeCount = active.length;
  const recentTasks = useQuery({
    queryKey: userTaskQueryKeys.presence(userScope.userId),
    queryFn: ({ signal }) => listTasks({ limit: 20 }, { signal }),
    enabled: userScope.enabled,
    staleTime: 10_000,
    refetchInterval: taskTrayRefetchInterval(taskTrayMinimized, hasActive),
  });

  const recentCount = recentTasks.data?.items.length ?? 0;
  const hasAnything = hasActive || recentCount > 0;
  const badge = taskTrayBadge(activeCount, recentCount);

  // —— 操作：取消 / 重试 ——
  const runGenerationAction = async (
    gen: Generation,
    action: "cancel" | "retry",
  ) => {
    if (actionLocksRef.current.has(gen.id)) return;
    actionLocksRef.current.add(gen.id);
    setGenerationActionStates((current) => ({
      ...current,
      [gen.id]: { busy: true, error: null },
    }));
    try {
      if (action === "cancel") {
        await cancelTask("generations", gen.id);
      } else {
        await retryGeneration(gen.id);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "任务操作失败";
      logWarn(`tray.${action}_failed`, {
        scope: "tray",
        extra: { genId: gen.id, err: String(err) },
      });
      setGenerationActionStates((current) => ({
        ...current,
        [gen.id]: { busy: true, error: message },
      }));
      toast.error(action === "cancel" ? "取消任务失败" : "重试任务失败", {
        description: message,
      });
    } finally {
      await Promise.allSettled([
        pollInflightTasks({ generationIds: [gen.id], maxChecks: 1 }),
        recentTasks.refetch(),
      ]);
      actionLocksRef.current.delete(gen.id);
      setGenerationActionStates((current) => ({
        ...current,
        [gen.id]: {
          busy: false,
          error: current[gen.id]?.error ?? null,
        },
      }));
    }
  };
  const handleCancel = (gen: Generation) => {
    void runGenerationAction(gen, "cancel");
  };
  const handleRetry = (gen: Generation) => {
    void runGenerationAction(gen, "retry");
  };
  const handleView = (gen: Generation) => {
    if (!gen.image) return;
    openLightbox(
      gen.image.id,
      gen.image.data_url,
      gen.prompt,
      gen.image.display_url ?? gen.image.preview_url ?? gen.image.thumb_url,
    );
  };
  const handleLocate = (gen: Generation) => {
    const route = resolveGenerationRoute(gen);
    if (!route) return;
    setTaskTrayMinimized(true);
    router.push(route);
  };

  const expanded = !taskTrayMinimized;
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeTray = useCallback(
    () => setTaskTrayMinimized(true),
    [setTaskTrayMinimized],
  );
  useBodyScrollLock(hasAnything && expanded);
  const onPanelKeyDown = useModalLayer({
    open: hasAnything && expanded,
    rootRef: panelRef,
    onClose: closeTray,
  });

  // 完全无任务：整个 tray 隐藏（避免占位）
  if (!hasAnything) return null;

  return (
    <>
      {/* 移动端展开时 backdrop，点击收起（桌面端不渲染） */}
      <AnimatePresence>
        {expanded && (
          <motion.button
            type="button"
            key="tray-scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={closeTray}
            aria-label="关闭任务面板"
            className="fixed inset-0 z-[var(--z-tray)] bg-[var(--surface-scrim)] sm:hidden"
          />
        )}
      </AnimatePresence>

      <motion.div
        layout
        className={cn(
          "fixed z-[calc(var(--z-tray)+1)] pointer-events-none",
          // 移动端展开：作为底部 sheet 铺满底部。
          // 桌面端：右下角
          expanded
            ? "inset-x-0 bottom-0 mobile-dialog-shell flex flex-col items-stretch justify-end sm:inset-x-auto sm:bottom-6 sm:right-6 sm:left-auto sm:items-end sm:justify-start sm:p-0"
            : "bottom-[calc(6.5rem+env(safe-area-inset-bottom))] sm:bottom-6 right-4 sm:right-6 left-auto flex flex-col items-end",
        )}
        initial={{ y: 40, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={SPRING.soft}
      >
        <AnimatePresence mode="popLayout">
          {!taskTrayMinimized && (
            <motion.div
              ref={panelRef}
              key="tray-panel"
              initial={{ opacity: 0, y: 24, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 24, scale: 0.98 }}
              transition={SPRING.sheet}
              className={cn(
                "pointer-events-auto mobile-dialog-sheet flex min-h-0 w-full flex-col overflow-hidden rounded-t-[var(--radius-sheet)] border border-[var(--border)] bg-[var(--surface-glass)] shadow-[var(--shadow-2)] backdrop-blur-xl sm:mb-3 sm:w-[23rem] sm:rounded-[var(--radius-sheet)]",
              )}
              role="dialog"
              aria-modal="true"
              aria-label="任务中心"
              tabIndex={-1}
              onKeyDown={onPanelKeyDown}
            >
              {/* 把手（仅移动端） */}
              <div
                className="sm:hidden flex min-h-11 items-center justify-center"
                aria-hidden
              >
                <span className="block h-1 w-10 rounded-full bg-[var(--fg-3)]/70" />
              </div>
              <TaskCenter
                activeGenerations={active}
                localGenerations={generations}
                generationActionStates={generationActionStates}
                onCancelGeneration={handleCancel}
                onRetryGeneration={handleRetry}
                onViewGeneration={handleView}
                onLocateGeneration={handleLocate}
                onClose={closeTray}
              />
            </motion.div>
          )}
          {taskTrayMinimized && !taskIslandMounted && (
            <motion.button
              key="tray-button"
              type="button"
              initial={{ opacity: 0, y: 12, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 12, scale: 0.96 }}
              transition={SPRING.soft}
              onClick={() => setTaskTrayMinimized(false)}
              aria-label={badge.label}
              className={cn(
                "pointer-events-auto relative inline-flex h-12 w-12 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--surface-glass)] text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-xl transition",
                "hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
              )}
            >
              <ListChecks className="h-5 w-5" />
              {badge.count > 0 && (
                <span
                  className={cn(
                    "absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full px-1 type-caption font-semibold",
                    badge.active
                      ? "bg-[var(--accent)] text-[var(--accent-on)]"
                      : "border border-[var(--border-strong)] bg-[var(--bg-2)] text-[var(--fg-1)]",
                  )}
                >
                  {badge.count}
                </span>
              )}
            </motion.button>
          )}
        </AnimatePresence>
      </motion.div>
    </>
  );
}
