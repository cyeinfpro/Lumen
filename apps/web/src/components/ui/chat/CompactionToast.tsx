"use client";

import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { CompactionEvent } from "@/hooks/useContextCompactionEvents";
import { cn } from "@/lib/utils";
import { lumenMotion, reducedMotionTransition, usePrefersReducedMotion } from "@/styles/motion";
import { RollingTokenCounter } from "./RollingTokenCounter";

interface CompactionToastProps {
  event: CompactionEvent | null;
  conversationId: string | null;
  onRetry?: () => void;
  className?: string;
}

function formatTokens(value: number | undefined): string {
  const n = Math.max(0, Math.round(value ?? 0));
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

function titleFor(event: CompactionEvent): string {
  if (event.phase === "started") return "正在压缩较早上下文...";
  if (event.phase === "progress") {
    const current = event.progress?.currentSegment ?? 0;
    const total = event.progress?.totalSegments ?? 0;
    return total > 0 ? `${current}/${total} 段已完成...` : "正在压缩较早上下文...";
  }
  if (event.ok) return "上下文已压缩";
  return "压缩未成功";
}

function descriptionFor(event: CompactionEvent): string {
  if (event.phase !== "completed") {
    return event.trigger === "manual" ? "正在处理手动压缩请求。" : "正在整理可复用的早期信息。";
  }
  if (event.ok) {
    const freed = event.stats?.tokensFreed;
    return freed != null ? `已释放约 ${formatTokens(freed)} tokens。` : "已释放上下文空间。";
  }
  if (event.fallbackReason === "event_timeout") {
    return "事件等待超时，已使用截断模式继续。";
  }
  return "已使用截断模式继续。";
}

function ToneIcon({ event, reducedMotion }: { event: CompactionEvent; reducedMotion: boolean }) {
  const cls = "h-4 w-4";
  if (event.phase !== "completed") {
    return (
      <Loader2
        className={cn(cls, reducedMotion ? "" : "animate-spin")}
        aria-hidden="true"
      />
    );
  }
  if (event.ok) return <CheckCircle2 className={cls} aria-hidden="true" />;
  return <AlertTriangle className={cls} aria-hidden="true" />;
}

export function CompactionToast({
  event,
  conversationId,
  onRetry,
  className,
}: CompactionToastProps) {
  const reducedMotion = usePrefersReducedMotion();
  const eventKey = useMemo(() => {
    if (!event) return null;
    return [
      event.conversationId,
      event.phase,
      event.startedAt,
      event.completedAt ?? "",
      event.fallbackReason ?? "",
    ].join(":");
  }, [event]);
  const [dismissedKey, setDismissedKey] = useState<string | null>(null);
  const visible = Boolean(
    event && event.conversationId === conversationId && eventKey !== dismissedKey,
  );
  const failed = Boolean(event?.phase === "completed" && !event.ok);
  const tone = event?.phase === "completed" ? (event.ok ? "success" : "warning") : "info";

  useEffect(() => {
    if (!event || !eventKey || event.phase !== "completed") return;
    // BUG-030: 成功提示延长至 5 秒，避免用户阅读时自动消失。
    const timer = setTimeout(() => setDismissedKey(eventKey), event.ok ? 5000 : 4000);
    return () => clearTimeout(timer);
  }, [event, eventKey]);

  return (
    <AnimatePresence initial={false}>
      {visible && event ? (
        <motion.div
          key={event.conversationId}
          role="status"
          aria-live="polite"
          initial={{ opacity: 0, y: reducedMotion ? 0 : -8, scale: reducedMotion ? 1 : 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: reducedMotion ? 0 : -6, scale: reducedMotion ? 1 : 0.98 }}
          transition={reducedMotionTransition(reducedMotion, lumenMotion.toastEnterMs)}
          className={cn(
            "pointer-events-auto w-[320px] max-w-[calc(100vw-2rem)] rounded-xl border px-3 py-2.5",
            "bg-neutral-900/95 text-[var(--fg-0)] shadow-lumen-pop backdrop-blur-xl",
            "max-sm:fixed max-sm:left-4 max-sm:right-4 max-sm:top-[max(1rem,env(safe-area-inset-top))] max-sm:w-auto",
            tone === "success" && "border-[var(--success)]/30",
            tone === "warning" && "border-[var(--warning)]/35",
            tone === "info" && "border-[var(--info)]/30",
            className,
          )}
        >
          <div className="flex items-start gap-3">
            <div
              className={cn(
                "mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full",
                tone === "success" && "bg-[var(--success-soft)] text-[var(--success)]",
                tone === "warning" && "bg-[var(--warning-soft)] text-[var(--warning)]",
                tone === "info" && "bg-[var(--info-soft)] text-[var(--info)]",
              )}
            >
              <ToneIcon event={event} reducedMotion={reducedMotion} />
            </div>

            <div className="min-w-0 flex-1">
              <p className="truncate text-[13px] font-medium leading-tight">{titleFor(event)}</p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-[var(--fg-1)]">
                {descriptionFor(event)}
              </p>

              {event.phase === "progress" && event.progress?.totalSegments ? (
                <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/10">
                  <motion.div
                    className="h-full rounded-full bg-[var(--info)]"
                    initial={false}
                    animate={{
                      width: `${Math.min(
                        100,
                        Math.max(
                          0,
                          (event.progress.currentSegment / event.progress.totalSegments) * 100,
                        ),
                      )}%`,
                    }}
                    transition={
                      reducedMotion ? { duration: 0 } : { duration: 0.2, ease: lumenMotion.easeOut }
                    }
                  />
                </div>
              ) : null}

              {event.phase === "completed" && event.ok && event.stats ? (
                <p className="mt-1.5 text-[11px] leading-none text-[var(--fg-2)]">
                  释放{" "}
                  <RollingTokenCounter
                    value={event.stats.tokensFreed}
                    className="text-[var(--fg-1)]"
                    format={(v) => formatTokens(v)}
                  />{" "}
                  tokens
                </p>
              ) : null}

              {failed && onRetry ? (
                <button
                  type="button"
                  onClick={() => {
                    if (eventKey) setDismissedKey(eventKey);
                    onRetry();
                  }}
                  className={cn(
                    "mt-2 inline-flex h-7 items-center gap-1.5 rounded-lg px-2 text-[11px] font-medium",
                    "bg-white/8 text-[var(--fg-0)] hover:bg-white/12 focus-visible:outline-none",
                    "focus-visible:ring-2 focus-visible:ring-[var(--warning)]/60",
                  )}
                >
                  <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                  重试
                </button>
              ) : null}
            </div>
          </div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
