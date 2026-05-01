"use client";

// Lumen V1 全局任务托盘：
//  - 无任务：整个 tray 隐藏
//  - 有任务：顶部任务按钮负责打开；展开态 = 玻璃卡 + TaskItem 列表
//  - 移动端改为底部中间 full-width
//  - 入场/退场用 AnimatePresence，每项 stagger 30ms
//  - 成功后短暂展示「已完成」，随后在自然状态下由 store 维护（不改 store 逻辑）
//
// 注意：本组件不维护任务生命周期；取消 / 重试只是发送 API 调用，store 的 SSE handler 会更新状态。

import { useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ChevronDown, X } from "lucide-react";

import { useUiStore } from "@/store/useUiStore";
import { useChatStore } from "@/store/useChatStore";
import { cancelTask } from "@/lib/apiClient";
import { logWarn } from "@/lib/logger";
import type { Generation } from "@/lib/types";
import { cn } from "@/lib/utils";
import { MobileIconButton } from "@/components/ui/primitives/mobile/MobileIconButton";
import { SPRING } from "@/lib/motion";
import { TaskItem } from "./tray/TaskItem";

const MAX_VISIBLE = 5;
// 成功完成后保留在 tray 的软窗口（ms）；到期后不再纳入 displayList 渲染
const COMPLETED_LINGER_MS = 4000;

export function GlobalTaskTray() {
  const taskTrayMinimized = useUiStore((s) => s.taskTray.minimized);
  const setTaskTrayMinimized = useUiStore((s) => s.setTaskTrayMinimized);
  const generations = useChatStore((s) => s.generations);
  const retryGeneration = useChatStore((s) => s.retryGeneration);
  const openLightbox = useUiStore((s) => s.openLightbox);

  const [now, setNow] = useState(() => Date.now());

  const { active, terminal } = useMemo(() => {
    const activeItems: Generation[] = [];
    const terminalItems: Generation[] = [];
    for (const g of Object.values(generations)) {
      if (g.status === "queued" || g.status === "running") {
        activeItems.push(g);
      } else if (g.status === "succeeded" || g.status === "failed") {
        terminalItems.push(g);
      }
    }
    activeItems.sort((a, b) => b.started_at - a.started_at);
    terminalItems.sort((a, b) => b.started_at - a.started_at);
    return { active: activeItems, terminal: terminalItems };
  }, [generations]);

  const {
    displayList,
    activeCount,
    hasActive,
    hiddenCount,
    hasLingeringTerminal,
  } = useMemo(() => {
    const nowMs = now;

    const lingering: Generation[] = [];
    for (const g of terminal) {
      const t0 = g.finished_at ?? g.started_at ?? nowMs;
      if (nowMs - t0 <= COMPLETED_LINGER_MS) lingering.push(g);
    }

    const combined = [...active, ...lingering];
    const visible = combined.slice(0, MAX_VISIBLE);

    return {
      displayList: visible,
      activeCount: active.length,
      hasActive: active.length > 0,
      hiddenCount: Math.max(0, combined.length - visible.length),
      hasLingeringTerminal: lingering.length > 0,
    };
  }, [active, now, terminal]);

  // 只有终态任务处于 linger 展示窗口时才需要本地 tick 来让它自然消失。
  useEffect(() => {
    if (!hasLingeringTerminal) return;
    const t = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(t);
  }, [hasLingeringTerminal]);

  const hasAnything = displayList.length > 0;

  useEffect(() => {
    if (!hasAnything && !taskTrayMinimized) {
      setTaskTrayMinimized(true);
    }
  }, [hasAnything, setTaskTrayMinimized, taskTrayMinimized]);

  // 完全无任务：整个 tray 隐藏（避免占位）
  if (!hasAnything) return null;

  // —— 操作：取消 / 重试 ——
  const handleCancel = async (gen: Generation) => {
    try {
      await cancelTask("generations", gen.id);
    } catch (err) {
      logWarn("tray.cancel_failed", {
        scope: "tray",
        extra: { genId: gen.id, err: String(err) },
      });
    }
  };
  const handleRetry = async (gen: Generation) => {
    try {
      await retryGeneration(gen.id);
    } catch (err) {
      logWarn("tray.retry_failed", {
        scope: "tray",
        extra: { genId: gen.id, err: String(err) },
      });
    }
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

  const expanded = !taskTrayMinimized;

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
            onClick={() => setTaskTrayMinimized(true)}
            aria-label="关闭任务面板"
            className="sm:hidden fixed inset-0 bg-black/40 backdrop-blur-[2px] z-[var(--z-tray)]"
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
            ? "inset-x-0 bottom-0 sm:inset-x-auto sm:bottom-6 sm:right-6 sm:left-auto flex flex-col items-stretch sm:items-end"
            : "bottom-[calc(6.5rem+env(safe-area-inset-bottom))] sm:bottom-6 right-4 sm:right-6 left-auto flex flex-col items-end",
        )}
        initial={{ y: 40, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={SPRING.soft}
      >
        <AnimatePresence mode="popLayout">
          {!taskTrayMinimized && (
            <motion.div
              key="tray-panel"
              initial={{ opacity: 0, y: 24, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 24, scale: 0.98 }}
              transition={SPRING.sheet}
              className={cn(
                "pointer-events-auto",
                // 移动端：底部 sheet，宽度铺满，圆角仅顶部，顶部把手
                "w-full rounded-t-2xl sm:rounded-2xl",
                "sm:w-80 sm:mb-3",
                "border border-white/10 bg-[var(--surface)] backdrop-blur-xl shadow-lumen-card",
                "overflow-hidden",
                "pb-[env(safe-area-inset-bottom)] sm:pb-0",
              )}
              role="region"
              aria-label="任务托盘"
            >
              {/* 把手（仅移动端） */}
              <div className="sm:hidden flex justify-center pt-2 pb-1" aria-hidden>
                <span className="block w-10 h-1 rounded-full bg-white/20" />
              </div>
              <header className="flex items-center gap-2 px-3 py-2.5 border-b border-white/5">
                <span
                  className={cn(
                    "w-2 h-2 rounded-full shrink-0",
                    hasActive
                      ? "bg-[var(--accent)] animate-pulse"
                      : "bg-[var(--ok)]",
                  )}
                />
                <h4 className="text-xs font-medium text-neutral-200 flex-1">
                  {hasActive ? `进行中的任务 (${activeCount})` : "全部完成"}
                </h4>
                {/* 移动端关闭按钮 */}
                <MobileIconButton
                  icon={<X className="w-4 h-4" />}
                  label="关闭任务面板"
                  onPress={() => setTaskTrayMinimized(true)}
                  className="sm:hidden text-neutral-400 hover:text-white hover:bg-white/10 focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60 rounded-md"
                />
                {/* 桌面端折叠按钮 */}
                <button
                  type="button"
                  onClick={() => setTaskTrayMinimized(true)}
                  aria-label="折叠任务面板"
                  className="hidden sm:inline-flex w-6 h-6 items-center justify-center rounded-md text-neutral-400 hover:text-white hover:bg-white/10 active:scale-[0.95] transition-all outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60"
                >
                  <ChevronDown className="w-3.5 h-3.5" />
                </button>
              </header>

              <ul
                className="p-2 space-y-1.5 max-h-[min(70dvh,480px)] sm:max-h-[50vh] overflow-y-auto"
                aria-live="polite"
              >
                <AnimatePresence initial={false}>
                  {displayList.map((gen, i) => (
                    <motion.li
                      key={gen.id}
                      layout
                      initial={{ opacity: 0, y: 8, scale: 0.98 }}
                      animate={{
                        opacity: 1,
                        y: 0,
                        scale: 1,
                        transition: { delay: i * 0.03 },
                      }}
                      exit={{
                        opacity: 0,
                        y: -4,
                        scale: 0.98,
                        transition: { duration: 0.18 },
                      }}
                    >
                      <TaskItem
                        gen={gen}
                        onCancel={handleCancel}
                        onRetry={handleRetry}
                        onView={handleView}
                      />
                    </motion.li>
                  ))}
                </AnimatePresence>
                {hiddenCount > 0 && (
                  <li className="text-center pt-0.5">
                    <span className="text-[11px] text-neutral-500">
                      还有 {hiddenCount} 项未展示
                    </span>
                  </li>
                )}
              </ul>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </>
  );
}
