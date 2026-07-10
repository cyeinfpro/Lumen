"use client";

import { Fragment } from "react";
import { SlidersHorizontal } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ComposerExecutionSummary } from "./executionSummary";

export function ExecutionSummaryBar({
  summary,
  compact = false,
  onAdjust,
}: {
  summary: ComposerExecutionSummary;
  compact?: boolean;
  onAdjust?: () => void;
}) {
  return (
    <div
      aria-label={summary.text}
      title={summary.text}
      className={cn(
        "mx-3 flex min-h-7 items-center gap-1.5 rounded-[var(--radius-card)] border px-2.5 py-1.5",
        "text-[11px] leading-4 text-[var(--fg-1)]",
        "overflow-x-auto overscroll-x-contain no-scrollbar",
        compact ? "mt-1 whitespace-nowrap" : "mt-1.5 flex-wrap",
        "border-[var(--border-subtle)] bg-[var(--bg-2)]/55",
      )}
    >
      <span className="shrink-0 text-[var(--fg-2)]">将执行：</span>
      <span
        className={cn(
          "shrink-0 font-medium",
          summary.tone === "image"
            ? "text-[var(--accent)]"
            : "text-[var(--fg-0)]",
        )}
      >
        {summary.taskLabel}
      </span>
      {summary.parts.map((part, index) => (
        <Fragment key={`${part}-${index}`}>
          <span className="shrink-0 text-[var(--fg-3)]">·</span>
          <span
            className={cn(
              "shrink-0",
              summary.costWarning && index === summary.parts.length - 1
                ? "text-[var(--danger)]"
                : "text-[var(--fg-1)]",
            )}
          >
            {part}
          </span>
        </Fragment>
      ))}
      {onAdjust ? (
        <>
          <span className="min-w-1 flex-1" aria-hidden />
          <button
            type="button"
            onClick={onAdjust}
            className="inline-flex min-h-7 shrink-0 items-center gap-1 rounded-[var(--radius-control)] px-2 text-[11px] font-medium text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
          >
            <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
            调整
          </button>
        </>
      ) : null}
    </div>
  );
}
