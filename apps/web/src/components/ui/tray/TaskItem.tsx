"use client";

// 单条任务条：缩略图（succeeded 时真实 img，运行时骨架）+ 进度环 + 状态文字 + 动作。
// 不持有自己的副作用；取消/重试走父级传入回调。

import { memo } from "react";
import { Check, Loader2, RotateCw, X } from "lucide-react";
import type { Generation, GenerationStage } from "@/lib/types";
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
    statusText = gen.error_message ?? "生成失败";
  } else if (canceled) {
    statusText = "已取消";
  } else if (succeeded) {
    statusText = "已完成";
  } else if (queued) {
    statusText = "排队中";
  } else if (gen.attempt > 1 && running) {
    statusText = `${STAGE_LABEL[gen.stage]} (第${gen.attempt}次)`;
  } else {
    statusText = STAGE_LABEL[gen.stage];
  }

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex gap-2.5 sm:gap-3 items-center p-2 rounded-xl border transition-all",
        "active:scale-[0.98] active:bg-white/5",
        failed
          ? "bg-red-500/5 border-red-500/30"
          : "bg-white/[0.03] border-white/10",
      )}
    >
      {/* 缩略图 / 骨架：窄屏缩小到 40，桌面保持 44 */}
      <button
        type="button"
        onClick={() => onView?.(gen)}
        disabled={!onView || !succeeded}
        aria-label={succeeded ? "查看结果" : "缩略图"}
        className={cn(
          "relative w-10 h-10 sm:w-11 sm:h-11 shrink-0 rounded-lg overflow-hidden bg-neutral-900 border border-white/5",
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
              <span className="text-red-400 text-lg leading-none">!</span>
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
        <p className="text-[13px] font-medium text-neutral-200 truncate leading-tight">
          {truncate(gen.prompt || "图像生成", 40)}
        </p>
        <p
          className={cn(
            // 窄屏允许换行，避免进度/状态/百分比被 truncate 挤断
            "text-[11px] mt-0.5 break-words sm:truncate",
            failed ? "text-red-300/90" : "text-neutral-500",
          )}
        >
          {running && (
            <span className="inline-flex flex-wrap items-center gap-x-1 gap-y-0.5">
              <Loader2 className="w-2.5 h-2.5 animate-spin" />
              <span>{statusText}</span>
              {!queued && (
                <span className="tabular-nums text-neutral-600">
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
        {failed && onRetry && (
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
        "w-11 h-11 sm:w-7 sm:h-7 inline-flex items-center justify-center rounded-md text-neutral-400 hover:text-white hover:bg-white/10 active:scale-[0.95] transition-all",
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
