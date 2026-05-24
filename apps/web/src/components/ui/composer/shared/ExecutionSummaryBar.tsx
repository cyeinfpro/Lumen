"use client";

import { Fragment } from "react";
import { cn } from "@/lib/utils";
import type { ComposerExecutionSummary } from "./executionSummary";

export function ExecutionSummaryBar({
  summary,
  compact = false,
}: {
  summary: ComposerExecutionSummary;
  compact?: boolean;
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
        summary.tone === "image"
          ? "border-[var(--border-amber)] bg-[var(--amber-400)]/10"
          : "border-[var(--border-subtle)] bg-[var(--bg-2)]/55",
      )}
    >
      <span className="shrink-0 text-[var(--fg-2)]">将执行：</span>
      <span
        className={cn(
          "shrink-0 font-medium",
          summary.tone === "image"
            ? "text-[var(--amber-300)]"
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
    </div>
  );
}
