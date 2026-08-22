"use client";

import { AlertCircle, ArrowRight, CheckCircle2, Loader2, Octagon } from "lucide-react";
import Link from "next/link";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type { AgentRun } from "../model/contracts";
import { agentRunErrorPresentation } from "../model/errors";

const STATUS_TEXT: Record<AgentRun["status"], string> = {
  queued: "等待运行",
  running: "Agent 运行中",
  succeeded: "运行完成",
  partial: "部分完成",
  failed: "运行失败",
  cancelled: "已取消",
};

export function AgentRunStatus({
  run,
  onContinue,
}: {
  run: AgentRun;
  onContinue?: () => void;
}) {
  const active = run.status === "queued" || run.status === "running";
  const failed = run.status === "failed" || run.status === "partial";
  const error = failed ? agentRunErrorPresentation(run.error_code) : null;
  const assertive = run.status === "failed";
  return (
    <div
      role={assertive ? "alert" : "status"}
      aria-live={assertive ? "assertive" : "polite"}
      className={cn(
        "mt-3 flex min-h-10 flex-wrap items-center gap-2 border-l-2 px-3 py-2 type-caption",
        runStatusTone(active, failed),
      )}
    >
      <RunStatusIcon status={run.status} />
      <span className="font-medium">{STATUS_TEXT[run.status]}</span>
      {error ? <span className="text-[var(--fg-1)]">{error.detail}</span> : null}
      <RunRecoveryActions error={error} failed={failed} onContinue={onContinue} />
    </div>
  );
}

function runStatusTone(active: boolean, failed: boolean): string {
  // The caller owns the dynamic role="alert"/role="status" live region.
  if (failed) return "border-danger-border bg-danger-soft text-[var(--danger-fg)]";
  if (active) return "border-accent-border bg-accent-soft text-[var(--fg-1)]";
  return "border-[var(--border-subtle)] text-[var(--fg-2)]";
}

function RunStatusIcon({ status }: { status: AgentRun["status"] }) {
  if (status === "queued" || status === "running") {
    return <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" aria-hidden />;
  }
  if (status === "succeeded") {
    return <CheckCircle2 className="h-3.5 w-3.5 text-success" aria-hidden />;
  }
  if (status === "cancelled") {
    return <Octagon className="h-3.5 w-3.5" aria-hidden />;
  }
  return <AlertCircle className="h-3.5 w-3.5" aria-hidden />;
}

function RunRecoveryActions({
  error,
  failed,
  onContinue,
}: {
  error: ReturnType<typeof agentRunErrorPresentation> | null;
  failed: boolean;
  onContinue?: () => void;
}) {
  const hasLink = Boolean(error?.href && error.actionLabel);
  return (
    <>
      {hasLink ? (
        <span role="status" className="ml-auto inline-flex">
          <Link
            href={error?.href ?? "/agent"}
            className="inline-flex min-h-9 items-center gap-1 rounded-[var(--radius-control)] px-2 type-caption font-medium text-[var(--fg-0)] hover:bg-[var(--bg-2)] max-sm:min-h-11"
          >
            {error?.actionLabel}
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Link>
        </span>
      ) : null}
      {onContinue && failed ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={onContinue}
          className={hasLink ? undefined : "ml-auto"}
        >
          继续
        </Button>
      ) : null}
    </>
  );
}
