"use client";

// 单条任务条只负责视图组合；状态派生集中在 taskItemModel。

import { Check, Loader2, RotateCw, X } from "lucide-react";
import { memo } from "react";

import type { Generation, RecommendedErrorAction } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  deriveTaskItemPresentation,
  type TaskItemPresentation,
} from "./taskItemModel";

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
  const presentation = deriveTaskItemPresentation(gen);

  return (
    <TaskItemView
      gen={gen}
      presentation={presentation}
      onCancel={onCancel}
      onRetry={onRetry}
      onView={onView}
    />
  );
});

function TaskItemView({
  gen,
  presentation,
  onCancel,
  onRetry,
  onView,
}: TaskItemProps & { presentation: TaskItemPresentation }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "relative flex items-center gap-2.5 rounded-[var(--radius-card)] border p-2 transition-all sm:gap-3",
        "active:scale-[0.98] active:bg-[var(--bg-3)]",
        presentation.failed
          ? "border-danger-border bg-danger-soft pb-8"
          : "border-[var(--border)] bg-[var(--bg-2)]",
        presentation.showRecoveryActions &&
          !presentation.failed &&
          "pb-8",
      )}
    >
      <TaskThumbnail
        gen={gen}
        presentation={presentation}
        onView={onView}
      />
      <TaskSummary presentation={presentation} />
      <TaskControls
        gen={gen}
        presentation={presentation}
        onCancel={onCancel}
        onRetry={onRetry}
      />
      <TaskRecoveryBar
        gen={gen}
        presentation={presentation}
        onRetry={onRetry}
      />
    </div>
  );
}

function TaskThumbnail({
  gen,
  presentation,
  onView,
}: {
  gen: Generation;
  presentation: TaskItemPresentation;
  onView?: (gen: Generation) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onView?.(gen)}
      disabled={!onView || !presentation.succeeded}
      aria-label={presentation.succeeded ? "查看结果" : "缩略图"}
      className={cn(
        "relative h-11 w-11 shrink-0 overflow-hidden rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)]",
        "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
        presentation.succeeded && onView
          ? "cursor-pointer hover:opacity-90 active:scale-[0.92]"
          : "cursor-default",
      )}
    >
      {presentation.succeeded && gen.image ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={
            gen.image.thumb_url ?? gen.image.preview_url ?? gen.image.data_url
          }
          alt=""
          className="h-full w-full object-cover"
        />
      ) : (
        <div
          className={cn(
            "flex h-full w-full items-center justify-center",
            presentation.running && "animate-pulse bg-[var(--bg-2)]",
          )}
        >
          {presentation.failed && (
            <span className="text-lg leading-none text-danger">!</span>
          )}
        </div>
      )}
      {presentation.running && (
        <span className="absolute bottom-0.5 right-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-black/80">
          <ProgressRing ratio={presentation.ratio} size={12} stroke={2} />
        </span>
      )}
      {presentation.succeeded && (
        <span className="absolute bottom-0.5 right-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-[var(--ok)]">
          <Check className="h-2.5 w-2.5 text-white" strokeWidth={3} />
        </span>
      )}
    </button>
  );
}

function TaskSummary({
  presentation,
}: {
  presentation: TaskItemPresentation;
}) {
  return (
    <div className="min-w-0 flex-1">
      <p className="truncate text-[13px] font-medium leading-tight text-[var(--fg-0)]">
        {presentation.title}
      </p>
      <p
        aria-live="polite"
        className={cn(
          "mt-0.5 break-words text-[11px] sm:truncate",
          presentation.failed ? "text-danger" : "text-[var(--fg-2)]",
        )}
      >
        {presentation.running && (
          <span className="inline-flex flex-wrap items-center gap-x-1 gap-y-0.5">
            <Loader2 className="h-2.5 w-2.5 animate-spin" />
            <span>{presentation.statusText}</span>
            {!presentation.queued && (
              <span className="tabular-nums text-[var(--fg-3)]">
                {Math.round(presentation.ratio * 100)}%
              </span>
            )}
          </span>
        )}
        {!presentation.running && presentation.statusText}
      </p>
    </div>
  );
}

function TaskControls({
  gen,
  presentation,
  onCancel,
  onRetry,
}: {
  gen: Generation;
  presentation: TaskItemPresentation;
  onCancel?: (gen: Generation) => void;
  onRetry?: (gen: Generation) => void;
}) {
  const recoverable = presentation.failed || presentation.canceled;

  return (
    <div className="flex shrink-0 items-center gap-0.5">
      {presentation.running && onCancel && (
        <IconBtn
          onClick={() => onCancel(gen)}
          aria-label="取消任务"
          title="取消"
        >
          <X className="h-3.5 w-3.5" />
        </IconBtn>
      )}
      {recoverable && onRetry && !presentation.showRecoveryActions && (
        <IconBtn
          onClick={() => onRetry(gen)}
          aria-label="重试任务"
          title="重试"
          className="text-[var(--accent)] hover:bg-[var(--accent)]/15"
        >
          <RotateCw className="h-3.5 w-3.5" />
        </IconBtn>
      )}
    </div>
  );
}

function TaskRecoveryBar({
  gen,
  presentation,
  onRetry,
}: {
  gen: Generation;
  presentation: TaskItemPresentation;
  onRetry?: (gen: Generation) => void;
}) {
  if (!presentation.showRecoveryActions) return null;

  return (
    <div className="absolute bottom-1.5 left-[3.75rem] right-2 flex flex-wrap gap-1">
      {presentation.actions
        .slice(0, 2)
        .map((action) => (
          <TaskRecoveryAction
            key={action.id}
            action={action}
            onRetry={onRetry ? () => onRetry(gen) : undefined}
          />
        ))}
    </div>
  );
}

function TaskRecoveryAction({
  action,
  onRetry,
}: {
  action: RecommendedErrorAction;
  onRetry?: () => void;
}) {
  if (action.kind === "retry" && onRetry) {
    return (
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex min-h-11 items-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-1.5 text-[10px] text-[var(--fg-1)] hover:text-[var(--fg-0)]"
      >
        {action.label}
      </button>
    );
  }
  if (action.kind === "link" && action.href) {
    return (
      <a
        href={action.href}
        className="inline-flex min-h-11 items-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-1.5 text-[10px] text-[var(--fg-1)] hover:text-[var(--fg-0)]"
      >
        {action.label}
      </a>
    );
  }
  return (
    <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-1.5 py-0.5 text-[10px] text-[var(--fg-2)]">
      {action.label}
    </span>
  );
}

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
        "inline-flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-1)] transition-all hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] active:scale-[0.95] sm:h-7 sm:w-7",
        "outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60",
        className,
      )}
    >
      {children}
    </button>
  );
}

function ProgressRing({
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
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(1, ratio));
  const offset = circumference * (1 - clamped);

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
        r={radius}
        stroke="currentColor"
        strokeWidth={stroke}
        fill="none"
        className="text-[var(--border-strong)]"
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        stroke="currentColor"
        strokeWidth={stroke}
        strokeLinecap="round"
        fill="none"
        strokeDasharray={circumference}
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
