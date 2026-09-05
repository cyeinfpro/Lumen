"use client";

import { AlertCircle, ArrowRight, CheckCircle2, Loader2, Octagon } from "lucide-react";
import Link from "next/link";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type { AgentRun } from "../model/contracts";
import { agentRunErrorPresentation } from "../model/errors";
import { agentRunPresentation } from "./agentPresentation";

export function AgentRunStatus({
  run,
  onContinue,
}: {
  run: AgentRun;
  onContinue?: () => void;
}) {
  const presentation = agentRunPresentation(run);
  const uncertain = presentation.kind === "uncertain";
  const active = run.status === "queued" || run.status === "running";
  const failed = !uncertain && (run.status === "failed" || run.status === "partial");
  const error = failed ? agentRunErrorPresentation(run.error_code) : null;
  const assertive = failed && run.status === "failed";
  return (
    <div
      data-agent-run-state={presentation.kind}
      className={cn(
        "mt-3 flex min-h-10 flex-wrap items-center gap-2 border-l-2 px-3 py-2 type-caption",
        uncertain ? "border-warning-border text-[var(--warning-fg)]" : runStatusTone(active, failed),
      )}
    >
      <div
        role={assertive ? "alert" : "status"}
        aria-live={assertive ? "assertive" : "polite"}
        className="flex min-w-0 flex-1 flex-wrap items-center gap-2"
      >
        {uncertain ? <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden /> : <RunStatusIcon status={run.status} />}
        <span className="font-medium">{presentation.label}</span>
        {run.memory_state === "degraded" ? (
          <span className="text-[var(--fg-1)]">本轮记忆服务降级</span>
        ) : null}
        {error ? <span className="text-[var(--fg-1)]">{error.detail}</span> : null}
      </div>
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
    return (
      <Loader2
        className="h-3.5 w-3.5 animate-spin text-accent motion-reduce:animate-none"
        aria-hidden
      />
    );
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
        <Link
          href={error?.href ?? "/agent"}
          className="ml-auto inline-flex min-h-9 items-center gap-1 rounded-[var(--radius-control)] px-2 type-caption font-medium text-[var(--fg-0)] hover:bg-[var(--bg-2)] max-sm:min-h-11"
        >
          {error?.actionLabel}
          <ArrowRight className="h-3.5 w-3.5" aria-hidden />
        </Link>
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
