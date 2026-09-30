"use client";

import { useEffect, useState } from "react";
import { textModelLabel } from "@/lib/textModelCapabilities";
import type { AgentRun } from "../model/contracts";
import { agentRunElapsedLabel } from "./agentPresentation";

export function AgentRunMetrics({ run }: { run: AgentRun }) {
  const active = run.status === "queued" || run.status === "running";
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, run.id]);
  const elapsed = agentRunElapsedLabel(run, now);
  if (!run.model && run.turn_count <= 0 && run.tool_call_count <= 0 && !elapsed) return null;
  return (
    <div role="group" aria-label="本轮执行信息" className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 type-caption text-[var(--fg-2)]">
      {run.model ? <span className="max-w-full truncate" title={run.model}>{textModelLabel(run.model)}</span> : null}
      {run.turn_count > 0 ? <span>已执行 {run.turn_count} 轮</span> : null}
      {run.tool_call_count > 0 ? <span>已调用 {run.tool_call_count} 次工具</span> : null}
      {elapsed ? <span className="tabular-nums">用时 {elapsed}</span> : null}
    </div>
  );
}
