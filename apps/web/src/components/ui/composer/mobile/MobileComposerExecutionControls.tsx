"use client";

import { ExecutionSummaryBar } from "../shared/ExecutionSummaryBar";
import type { ComposerExecutionSummary } from "../shared/executionSummary";

export function MobileComposerExecutionControls({
  summary,
  onAdjust,
}: {
  summary: ComposerExecutionSummary;
  onAdjust: () => void;
}) {
  return (
    <ExecutionSummaryBar
      summary={summary}
      compact
      onAdjust={onAdjust}
    />
  );
}
