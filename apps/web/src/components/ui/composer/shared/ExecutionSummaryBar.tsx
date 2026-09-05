"use client";

import { Fragment } from "react";
import { SlidersHorizontal } from "lucide-react";
import { Button } from "@/components/ui/primitives";
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
        "type-caption text-[var(--fg-1)]",
        compact ? "mt-1" : "mt-1.5",
        "border-[var(--border-subtle)] bg-[var(--bg-2)]",
      )}
    >
      <div className={cn("flex min-w-0 flex-1 items-center gap-1.5", compact ? "overflow-x-auto overscroll-x-contain whitespace-nowrap no-scrollbar" : "flex-wrap")}>
      <span className="shrink-0 text-[var(--fg-2)]">将执行：</span>
      <span className="type-label shrink-0 text-[var(--fg-0)]">
        {summary.taskLabel}
      </span>
      {summary.parts.map((part, index) => (
        <Fragment key={`${part}-${index}`}>
          <span className="shrink-0 text-[var(--fg-3)]">·</span>
          <span
            className={cn(
              "shrink-0",
              summary.costWarning && index === summary.parts.length - 1
                ? "text-warning"
                : "text-[var(--fg-1)]",
            )}
          >
            {part}
          </span>
        </Fragment>
      ))}
      </div>
      {onAdjust ? (
        <>
          <span className="min-w-1 flex-1" aria-hidden />
          <Button
            size="sm"
            variant="ghost"
            onClick={onAdjust}
            aria-label="执行设置"
            aria-haspopup="dialog"
            className="h-11 min-h-11 shrink-0 px-2 text-[var(--fg-1)] md:h-8 md:min-h-8"
            leftIcon={<SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />}
          >
            调整
          </Button>
        </>
      ) : null}
    </div>
  );
}
