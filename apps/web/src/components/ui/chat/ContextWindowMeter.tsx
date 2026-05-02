"use client";

import { useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Archive, Loader2 } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import {
  describeCompactError,
  useCompactConversation,
  type CompactConversationApiResponse,
} from "@/app/(chat)/_hooks/useCompactConversation";
import { CompactionToast } from "@/components/ui/chat/CompactionToast";
import { RollingTokenCounter } from "@/components/ui/chat/RollingTokenCounter";
import { toast } from "@/components/ui/primitives/Toast";
import {
  type CompactionEvent,
  type CompactJobTracker,
  useContextCompactionEvents,
} from "@/hooks/useContextCompactionEvents";
import type { ConversationContextStats } from "@/lib/apiClient";
import { qk } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { lumenMotion, usePrefersReducedMotion } from "@/styles/motion";
import { useChatStore } from "@/store/useChatStore";

type ExtendedContextStats = ConversationContextStats &
  Partial<{
    compression_enabled: boolean;
    summary_available: boolean;
    summary_tokens: number;
    summary_up_to_message_id: string | null;
    summary_updated_at: string | null;
    summary_first_user_message_id: string | null;
    summary_compression_runs: number;
    compressible_messages_count: number;
    compressible_tokens: number;
    estimated_tokens_freed: number;
    summary_target_tokens: number;
    compressed: boolean;
    last_fallback_reason: string | null;
    manual_compact_available: boolean;
    manual_compact_reset_seconds: number;
    manual_compact_min_input_tokens: number;
    manual_compact_cooldown_seconds: number;
    manual_compact_unavailable_reason: string | null;
  }>;

interface ContextWindowMeterProps {
  stats?: ExtendedContextStats | null;
  compact?: boolean;
  className?: string;
  conversationId?: string | null;
}

type ContextState = "full" | "compressed" | "compressed_truncated" | "truncated" | "circuit";

function formatTokens(value: number | undefined): string {
  const n = Math.max(0, Math.round(value ?? 0));
  if (n >= 1000) return `${Math.round(n / 100) / 10}k`;
  return String(n);
}

function tone(percent: number, state: ContextState): string {
  if (state === "circuit" || state === "truncated") return "bg-[var(--danger)]";
  if (state === "compressed_truncated") return "bg-[var(--warning)]";
  if (state === "compressed") return "bg-[var(--success)]";
  if (percent >= 95) return "bg-[var(--danger)]";
  if (percent >= 80) return "bg-[var(--amber-400)]";
  return "bg-[var(--fg-1)]";
}

function stateOf(stats: ExtendedContextStats): ContextState {
  const fallback = stats.last_fallback_reason;
  if (fallback === "circuit_open") return "circuit";
  const hasSummary = stats.compressed === true || stats.summary_available === true;
  if (hasSummary && stats.truncated) return "compressed_truncated";
  if (hasSummary) return "compressed";
  if (stats.truncated) return "truncated";
  return "full";
}

function stateLabel(state: ContextState): string {
  switch (state) {
    case "compressed":
      return "已压缩";
    case "compressed_truncated":
      return "压缩+截断";
    case "truncated":
      return "已截断";
    case "circuit":
      return "熔断";
    case "full":
    default:
      return "完整";
  }
}

function stateDescription(state: ContextState): string {
  switch (state) {
    case "compressed":
      return "已压缩早期上下文";
    case "compressed_truncated":
      return "已压缩早期上下文，并进一步按最近内容截断";
    case "truncated":
      return "已按最近内容截断";
    case "circuit":
      return "摘要服务暂时不可用，已回退截断";
    case "full":
    default:
      return "未截断";
  }
}

function compactBenefit(stats: ExtendedContextStats): {
  sourceMessages: number;
  sourceTokens: number;
  targetTokens: number;
  tokensFreed: number;
} {
  const sourceMessages = Math.max(0, Math.round(stats.compressible_messages_count ?? 0));
  const sourceTokens = Math.max(0, Math.round(stats.compressible_tokens ?? 0));
  const targetTokens = Math.max(
    0,
    Math.round(stats.summary_target_tokens ?? stats.summary_tokens ?? 0),
  );
  const explicitFreed = stats.estimated_tokens_freed;
  const tokensFreed = Math.max(
    0,
    Math.round(
      explicitFreed != null ? explicitFreed : Math.max(0, sourceTokens - targetTokens),
    ),
  );
  return { sourceMessages, sourceTokens, targetTokens, tokensFreed };
}

function MeterBar({
  percent,
  state,
  active,
  reducedMotion,
}: {
  percent: number;
  state: ContextState;
  active: boolean;
  reducedMotion: boolean;
}) {
  const animated = active && !reducedMotion;
  return (
    <span className="block h-0.5 w-full overflow-hidden rounded-full bg-white/10">
      <motion.span
        className={cn("block h-full rounded-full", tone(percent, state))}
        initial={false}
        animate={{
          width: `${percent}%`,
          backgroundPosition: animated ? ["0% 50%", "100% 50%"] : "0% 50%",
        }}
        transition={{
          width: reducedMotion ? { duration: 0 } : lumenMotion.spring,
          backgroundPosition: animated
            ? { repeat: Infinity, duration: lumenMotion.shimmerSeconds, ease: "linear" }
            : { duration: 0 },
        }}
        style={
          animated
            ? {
                backgroundImage:
                  "linear-gradient(90deg, var(--info), var(--accent), var(--info))",
                backgroundSize: "200% 100%",
              }
            : undefined
        }
      />
    </span>
  );
}

function disabledReason(
  stats: ExtendedContextStats,
  conversationId: string | null,
  isDialogOpen: boolean,
  isCompacting: boolean,
): string | null {
  if (!conversationId) return "没有当前会话";
  if (isCompacting) return "压缩进行中";
  if (isDialogOpen) return "压缩设置已打开";
  const minInputTokens = stats.manual_compact_min_input_tokens ?? 4000;
  if ((stats.estimated_input_tokens ?? 0) < minInputTokens) {
    return `${formatTokens(minInputTokens)} token 后可手动压缩（当前 ${formatTokens(
      stats.estimated_input_tokens,
    )}）`;
  }
  if (stats.manual_compact_available === false) {
    const reset = Math.max(0, Math.ceil(stats.manual_compact_reset_seconds ?? 0));
    if (stats.manual_compact_unavailable_reason === "circuit_open") {
      return reset > 0 ? `摘要服务熔断中，${reset} 秒后可重试` : "摘要服务熔断中";
    }
    if (stats.manual_compact_unavailable_reason === "cooldown") {
      return reset > 0 ? `冷却中，${reset} 秒后可重试` : "冷却中，请稍后重试";
    }
    return reset > 0 ? `暂不可用，${reset} 秒后可重试` : "暂不可用，请稍后重试";
  }
  if (stats.last_fallback_reason === "circuit_open") {
    return "摘要服务熔断中，请稍后重试";
  }
  return null;
}

function resultToast(result: CompactConversationApiResponse) {
  if (result.status === "pending") {
    toast.info("已开始后台压缩", {
      description: "可以继续对话，完成后会自动更新上下文状态。",
      durationMs: 4000,
    });
    return;
  }
  if (result.status === "failed") {
    toast.error("压缩失败", {
      description:
        result.reason === "lock_busy"
          ? "已有压缩任务在进行"
          : result.reason === "circuit_open"
            ? "压缩服务暂不可用"
            : "上游服务异常，稍后重试",
    });
    return;
  }
  if (!result.compacted) {
    toast.info("暂无需压缩", {
      description: `当前 ${formatTokens(result.estimated_input_tokens)} / 阈值 ${formatTokens(
        result.input_budget_tokens,
      )} token`,
    });
    return;
  }
  const s = result.summary;
  const isCached =
    s.status === "cached" ||
    s.status === "cached_after_lock_wait" ||
    s.status === "cas_reused";
  const isFallback = s.status === "created_local_fallback";
  const title = isCached
    ? "已使用现有上下文摘要"
    : isFallback
      ? `已用兜底摘要压缩 ${s.source_message_count} 条消息`
    : `已压缩 ${s.source_message_count} 条早期消息`;
  const description =
    [
      s.tokens_freed != null && s.tokens_freed > 0
        ? `释放约 ${formatTokens(s.tokens_freed)} token`
        : null,
      s.tokens > 0 ? `摘要 ${formatTokens(s.tokens)} token` : null,
      s.image_caption_count != null && s.image_caption_count > 0
        ? `图片描述 ${s.image_caption_count} 个`
        : null,
    ]
      .filter(Boolean)
      .join(" · ") || undefined;
  toast.success(title, { description });
}

export function ContextWindowMeter({
  stats,
  compact = false,
  className,
  conversationId,
}: ContextWindowMeterProps) {
  const storeConvId = useChatStore((s) => s.currentConvId);
  const queryClient = useQueryClient();
  const retryCompact = useCompactConversation();
  const convId = conversationId ?? storeConvId;
  const [trackedJob, setTrackedJob] = useState<CompactJobTracker | null>(null);
  const reducedMotion = usePrefersReducedMotion();

  const onCompactionEvent = useCallback(
    (event: CompactionEvent) => {
      if (event.phase === "completed" && convId) {
        setTrackedJob(null);
        void queryClient.refetchQueries({
          queryKey: qk.conversationContext(convId),
        });
        void queryClient.invalidateQueries({ queryKey: ["messages", convId] });
        void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      }
    },
    [convId, queryClient],
  );
  const { active: eventActive, latest: latestCompaction } =
    useContextCompactionEvents(convId, onCompactionEvent, trackedJob);
  const isCompacting = eventActive;

  const retryManualCompact = useCallback(() => {
    if (!convId || retryCompact.isPending || eventActive) return;
    retryCompact.mutate(
      { conversationId: convId },
      {
        onSuccess: resultToast,
        onError: (err) => {
          toast.error("重试压缩失败", { description: describeCompactError(err) });
        },
      },
    );
  }, [convId, retryCompact, eventActive]);

  const meta = useMemo(() => {
    if (!stats) return null;
    const percent = Math.max(0, Math.min(stats.percent ?? 0, 100));
    const rounded = Math.round(percent);
    const state = stateOf(stats);
    const benefit = compactBenefit(stats);
    const parts = [
      `上下文 ${formatTokens(stats.estimated_input_tokens)} / ${formatTokens(stats.input_budget_tokens)}`,
      `回复预留 ${formatTokens(stats.response_reserve_tokens)}`,
      stateDescription(state),
    ];
    if (benefit.sourceMessages > 0 || benefit.tokensFreed > 0) {
      parts.push(
        `可压缩 ${benefit.sourceMessages} 条 / ${formatTokens(benefit.sourceTokens)} token`,
      );
      if (benefit.tokensFreed > 0) {
        parts.push(`预计释放 ${formatTokens(benefit.tokensFreed)} token`);
      }
    }
    if (stats.summary_available || stats.compressed) {
      parts.push(`摘要 ${formatTokens(stats.summary_tokens)}`);
      if (stats.summary_compression_runs != null) {
        parts.push(`压缩 ${stats.summary_compression_runs} 次`);
      }
      if (stats.summary_up_to_message_id) {
        parts.push(`覆盖到 ${stats.summary_up_to_message_id}`);
      }
    }
    if (stats.last_fallback_reason) {
      parts.push(`fallback: ${stats.last_fallback_reason}`);
    }
    return {
      percent,
      rounded,
      state,
      benefit,
      title: parts.join(" · "),
      disabled: disabledReason(stats, convId, false, isCompacting),
    };
  }, [stats, convId, isCompacting]);

  if (!stats || !meta) return null;

  const benefitLabel =
    meta.benefit.tokensFreed > 0 ? `预计释放 ${formatTokens(meta.benefit.tokensFreed)}` : null;
  const buttonTitle =
    meta.disabled ??
    [
      "压缩历史上下文",
      meta.benefit.sourceMessages > 0
        ? `将 ${meta.benefit.sourceMessages} 条早期消息压成约 ${formatTokens(
            meta.benefit.targetTokens,
          )} token`
        : "将早期对话压缩为摘要以节省上下文窗口",
      benefitLabel,
    ]
      .filter(Boolean)
      .join(" — ");
  const buttonDisabled = meta.disabled != null;

  const startBackgroundCompact = () => {
    if (!convId || buttonDisabled || retryCompact.isPending) return;
    retryCompact.mutate(
      { conversationId: convId },
      {
        onSuccess: (result) => {
          if (result.status === "pending") {
            setTrackedJob({ jobId: result.job_id, startedAt: new Date().toISOString() });
          }
          resultToast(result);
        },
        onError: (err) => {
          toast.error("压缩启动失败", { description: describeCompactError(err) });
        },
      },
    );
  };

  if (compact) {
    return (
      <div
        className={cn("relative inline-flex shrink-0 items-center gap-1", className)}
        data-compacting={isCompacting ? "true" : undefined}
      >
          <span
            role="meter"
            aria-label={meta.title}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={meta.rounded}
            title={meta.title}
            className={cn(
              "inline-flex h-8 w-12 flex-col justify-center gap-1 rounded-lg",
              "border border-[var(--border-subtle)] bg-[var(--bg-1)]/70 px-1.5",
              "text-[10px] tabular-nums text-[var(--fg-2)]",
            )}
            style={{ fontFamily: "var(--font-mono)" }}
          >
            <span className="text-center leading-none">
              <RollingTokenCounter
                value={meta.rounded}
                active={isCompacting}
                format={(value) => `${Math.round(value)}%`}
              />
            </span>
            <MeterBar
              percent={meta.percent}
              state={meta.state}
              active={isCompacting}
              reducedMotion={reducedMotion}
            />
          </span>
        <button
            type="button"
            aria-label={buttonTitle}
            title={buttonTitle}
            disabled={buttonDisabled || retryCompact.isPending}
            onClick={startBackgroundCompact}
            className={cn(
              "inline-flex h-8 w-8 items-center justify-center rounded-lg border border-[var(--border-subtle)]",
              "bg-[var(--bg-1)]/70 text-[var(--fg-1)] transition-colors hover:bg-white/8 hover:text-[var(--fg-0)]",
              "disabled:pointer-events-none disabled:opacity-45",
            )}
          >
            {isCompacting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            ) : (
              <Archive className="h-3.5 w-3.5" aria-hidden="true" />
            )}
          </button>
        <CompactionToast
          event={latestCompaction}
          conversationId={convId}
          onRetry={retryManualCompact}
          className="absolute right-0 top-10 z-50"
        />
      </div>
    );
  }

  return (
    <div
      className={cn("relative inline-flex shrink-0 items-center gap-1.5", className)}
      data-compacting={isCompacting ? "true" : undefined}
    >
        <span
          role="meter"
          aria-label={meta.title}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={meta.rounded}
          title={meta.title}
          className={cn(
            "inline-flex h-8 w-[168px] flex-col justify-center gap-1 rounded-lg",
            "border border-[var(--border-subtle)] bg-[var(--bg-1)]/70 px-2.5",
            "text-[10px] text-[var(--fg-2)]",
          )}
        >
          <span className="flex min-w-0 items-center justify-between gap-2 leading-none">
            <span className="truncate">{stateLabel(meta.state)}</span>
            <span className="shrink-0 tabular-nums" style={{ fontFamily: "var(--font-mono)" }}>
              <RollingTokenCounter
                value={stats.estimated_input_tokens}
                active={isCompacting}
                format={formatTokens}
              />{" "}
              / {formatTokens(stats.input_budget_tokens)}
            </span>
          </span>
          <MeterBar
            percent={meta.percent}
            state={meta.state}
            active={isCompacting}
            reducedMotion={reducedMotion}
          />
        </span>
        <button
          type="button"
          title={buttonTitle}
          disabled={buttonDisabled || retryCompact.isPending}
          onClick={startBackgroundCompact}
          className={cn(
            "inline-flex h-8 items-center justify-center gap-1.5 rounded-lg px-2.5 text-[11px] font-medium",
            "border border-[var(--border-subtle)] bg-[var(--bg-1)]/70 text-[var(--fg-1)]",
            "transition-colors hover:bg-white/8 hover:text-[var(--fg-0)]",
            "disabled:pointer-events-none disabled:opacity-45",
          )}
        >
          {isCompacting ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Archive className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          <span>{isCompacting ? "压缩中" : benefitLabel ?? "压缩历史"}</span>
        </button>
      <CompactionToast
        event={latestCompaction}
        conversationId={convId}
        onRetry={retryManualCompact}
        className="absolute right-0 top-10 z-50"
      />
    </div>
  );
}
