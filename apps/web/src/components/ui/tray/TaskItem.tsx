"use client";

// 单条任务条：缩略图（succeeded 时真实 img，运行时骨架）+ 进度环 + 状态文字 + 动作。
// 不持有自己的副作用；取消/重试走父级传入回调。

import { memo } from "react";
import { Check, Loader2, RotateCw, X } from "lucide-react";
import type { Generation, GenerationStage } from "@/lib/types";
import { recommendedActionsForError } from "@/lib/errors";
import { cn } from "@/lib/utils";

const STAGE_LABEL: Record<GenerationStage, string> = {
  queued: "排队中",
  understanding: "理解中",
  rendering: "渲染中",
  finalizing: "收尾",
};

// 阶段 → 进度占比（视觉用，避免 0% 空环看起来坏掉）
const STAGE_RATIO: Record<GenerationStage, number> = {
  queued: 0.12,
  understanding: 0.35,
  rendering: 0.7,
  finalizing: 0.92,
};

const SUBSTAGE_LABEL: Record<string, string> = {
  waiting_queue: "排队中",
  waiting_provider: "等待可用通道",
  preparing_refs: "准备参考图",
  upstream_started: "模型生成中",
  upstream_retrying: "上游重试中",
  postprocessing: "图片后处理中",
  processing: "图片后处理中",
  storing: "保存图片中",
  display_ready: "图片已完成",
  retryable: "失败，可重试",
  terminal: "失败",
  cancelled: "已取消",
  provider_selected: "通道已就绪",
  stream_started: "模型生成中",
  partial_received: "生成预览中",
  final_received: "生成完成，处理中",
};

function truncate(s: string, n: number) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n) + "…" : s;
}

export interface TaskItemProps {
  gen: Generation;
  onCancel?: (gen: Generation) => void;
  onRetry?: (gen: Generation) => void;
  onView?: (gen: Generation) => void;
}

export const TaskItem = memo(function TaskItem({
  gen,
  onCancel,
  onRetry,
  onView,
}: TaskItemProps) {
  const running = gen.status === "queued" || gen.status === "running";
  const queued = gen.status === "queued";
  const failed = gen.status === "failed";
  const succeeded = gen.status === "succeeded";
  const canceled = gen.status === "canceled";

  const ratio = succeeded
    ? 1
    : failed || canceled
      ? 1
      : (STAGE_RATIO[gen.stage] ?? 0.2);

  // 状态文字
  let statusText: string;
  if (failed) {
    statusText =
      gen.diagnostics?.safe_error_summary ?? gen.error_message ?? "生成失败";
  } else if (canceled) {
    statusText = "已取消";
  } else if (succeeded) {
    statusText = "已完成";
  } else if (queued) {
    statusText =
      gen.substage && SUBSTAGE_LABEL[gen.substage]
        ? SUBSTAGE_LABEL[gen.substage]
        : "排队中";
    if (gen.queue_position != null && gen.queue_position > 0) {
      statusText += ` · 第 ${gen.queue_position} 位`;
    }
  } else if (gen.attempt > 1 && running) {
    statusText = `${
      gen.substage && SUBSTAGE_LABEL[gen.substage]
        ? SUBSTAGE_LABEL[gen.substage]
        : STAGE_LABEL[gen.stage]
    } (第${gen.attempt}次)`;
  } else {
    statusText =
      gen.substage && SUBSTAGE_LABEL[gen.substage]
        ? SUBSTAGE_LABEL[gen.substage]
        : STAGE_LABEL[gen.stage];
  }
  const actions =
    gen.recommended_actions?.length
      ? gen.recommended_actions
      : recommendedActionsForError(gen.error_code, {
          retryable: gen.retryable,
          status: gen.status,
        });
  const showRecoveryActions = (failed || canceled) && actions.length > 0;

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "relative flex gap-2.5 sm:gap-3 items-center rounded-[var(--radius-card)] border p-2 transition-all",
        "active:scale-[0.98] active:bg-white/5",
        failed
          ? "bg-danger-soft border-danger-border pb-8"
          : "bg-white/[0.03] border-[var(--border)]",
        showRecoveryActions && !failed && "pb-8",
      )}
    >
      {/* 缩略图 / 骨架：窄屏缩小到 40，桌面保持 44 */}
      <button
        type="button"
        onClick={() => onView?.(gen)}
        disabled={!onView || !succeeded}
        aria-label={succeeded ? "查看结果" : "缩略图"}
        className={cn(
          "relative h-10 w-10 shrink-0 overflow-hidden rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)] sm:h-11 sm:w-11",
          "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
          succeeded && onView
            ? "cursor-pointer hover:opacity-90 active:scale-[0.92]"
            : "cursor-default",
        )}
      >
        {succeeded && gen.image ? (
          /* eslint-disable-next-line @next/next/no-img-element */
          <img
            src={
              gen.image.thumb_url ?? gen.image.preview_url ?? gen.image.data_url
            }
            alt=""
            className="w-full h-full object-cover"
          />
        ) : (
          <div
            className={cn(
              "w-full h-full flex items-center justify-center",
              running && "bg-white/5 animate-pulse",
            )}
          >
            {failed && (
              <span className="text-danger text-lg leading-none">!</span>
            )}
          </div>
        )}
        {/* 角标：进度环 */}
        {running && (
          <span className="absolute bottom-0.5 right-0.5 w-4 h-4 rounded-full bg-black/80 flex items-center justify-center">
            <ProgressRing ratio={ratio} size={12} stroke={2} />
          </span>
        )}
        {succeeded && (
          <span className="absolute bottom-0.5 right-0.5 w-4 h-4 rounded-full bg-[var(--ok)] flex items-center justify-center">
            <Check className="w-2.5 h-2.5 text-white" strokeWidth={3} />
          </span>
        )}
      </button>

      <div className="flex-1 min-w-0">
        <p className="text-[13px] font-medium text-[var(--fg-0)] truncate leading-tight">
          {truncate(gen.prompt || "图像生成", 40)}
        </p>
        <p
          className={cn(
            // 窄屏允许换行，避免进度/状态/百分比被 truncate 挤断
            "text-[11px] mt-0.5 break-words sm:truncate",
            failed ? "text-danger" : "text-[var(--fg-2)]",
          )}
        >
          {running && (
            <span className="inline-flex flex-wrap items-center gap-x-1 gap-y-0.5">
              <Loader2 className="w-2.5 h-2.5 animate-spin" />
              <span>{statusText}</span>
              {!queued && (
                <span className="tabular-nums text-[var(--fg-3)]">
                  {Math.round(ratio * 100)}%
                </span>
              )}
            </span>
          )}
          {!running && statusText}
        </p>
      </div>

      {/* 动作 */}
      <div className="flex items-center gap-0.5 shrink-0">
        {running && onCancel && (
          <IconBtn
            onClick={() => onCancel(gen)}
            aria-label="取消任务"
            title="取消"
          >
            <X className="w-3.5 h-3.5" />
          </IconBtn>
        )}
        {(failed || canceled) && onRetry && !showRecoveryActions && (
          <IconBtn
            onClick={() => onRetry(gen)}
            aria-label="重试任务"
            title="重试"
            className="text-[var(--accent)] hover:bg-[var(--accent)]/15"
          >
            <RotateCw className="w-3.5 h-3.5" />
          </IconBtn>
        )}
      </div>
      {showRecoveryActions && (
        <div className="absolute bottom-1.5 left-[3.75rem] right-2 flex flex-wrap gap-1">
          {actions.slice(0, 2).map((action) => {
            if (action.kind === "retry" && onRetry) {
              return (
                <button
                  key={action.id}
                  type="button"
                  onClick={() => onRetry(gen)}
                  className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-1.5 py-0.5 text-[10px] text-[var(--fg-1)] hover:text-[var(--fg-0)]"
                >
                  {action.label}
                </button>
              );
            }
            if (action.kind === "link" && action.href) {
              return (
                <a
                  key={action.id}
                  href={action.href}
                  className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-1.5 py-0.5 text-[10px] text-[var(--fg-1)] hover:text-[var(--fg-0)]"
                >
                  {action.label}
                </a>
              );
            }
            return (
              <span
                key={action.id}
                className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-1.5 py-0.5 text-[10px] text-[var(--fg-2)]"
              >
                {action.label}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
});

function IconBtn({
  children,
  className,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      {...rest}
      className={cn(
        // 移动端 44px 命中区；桌面端保持紧凑 28px
        "inline-flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-1)] transition-all hover:bg-white/10 hover:text-[var(--fg-0)] active:scale-[0.95] sm:h-7 sm:w-7",
        "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
        className,
      )}
    >
      {children}
    </button>
  );
}

// 进度环（小号）
export function ProgressRing({
  ratio,
  size = 20,
  stroke = 3,
  className,
}: {
  ratio: number;
  size?: number;
  stroke?: number;
  className?: string;
}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(1, ratio));
  const offset = c * (1 - clamped);
  return (
    <svg
      className={cn("block", className)}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      aria-hidden
    >
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        stroke="currentColor"
        strokeWidth={stroke}
        fill="none"
        className="text-white/15"
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        stroke="currentColor"
        strokeWidth={stroke}
        strokeLinecap="round"
        fill="none"
        strokeDasharray={c}
        strokeDashoffset={offset}
        style={{
          transform: "rotate(-90deg)",
          transformOrigin: "center",
          transition: "stroke-dashoffset 150ms linear",
        }}
        className="text-[var(--accent)]"
      />
    </svg>
  );
}
