"use client";

import { SlidersHorizontal } from "lucide-react";
import { Button, Tooltip } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export function AgentExecutionSummary({
  runActive,
  summary,
  costLabel,
  costWarning,
  costLoading,
  settingsOpen,
  onOpenSettings,
}: {
  runActive: boolean;
  summary: string;
  costLabel: string | null;
  costWarning: boolean;
  costLoading: boolean;
  settingsOpen: boolean;
  onOpenSettings: () => void;
}) {
  return (
    <div
      data-testid="agent-execution-summary"
      className="flex min-h-11 min-w-0 flex-wrap items-center gap-x-2 border-t border-[var(--border-subtle)] px-2 py-1"
    >
      <div className="min-w-0 flex-1 [&>span]:flex [&>span]:min-w-0 [&>span]:w-full">
      <Tooltip content="调整下一轮参数">
        <Button
          variant="ghost"
          size="sm"
          aria-label={`调整执行参数：${summary}`}
          aria-expanded={settingsOpen}
          onClick={onOpenSettings}
          className="w-full min-w-0 flex-1 justify-start px-1 text-[var(--fg-2)]"
          leftIcon={<SlidersHorizontal className="h-4 w-4 shrink-0" aria-hidden />}
        >
          <span className="truncate type-caption">
            {runActive ? "下一轮 · " : ""}{summary}
          </span>
        </Button>
      </Tooltip>
      </div>
      {costLabel ? (
        <span
          aria-live="polite"
          data-agent-cost-estimate
          className={cn(
            "ml-auto max-w-full break-words text-right type-caption tabular-nums",
            costWarning ? "text-[var(--warning-fg)]" : "text-[var(--fg-2)]",
            costLoading && "opacity-70",
          )}
        >
          {costLabel}
        </span>
      ) : null}
    </div>
  );
}
