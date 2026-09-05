"use client";

import { RefreshCw } from "lucide-react";
import Link from "next/link";
import { Button } from "@/components/ui/primitives";

export function AgentComposerFeedback({
  submitting, stopping, submissionUncertain, checkingSubmission, onReconcileSubmission, error, action,
}: {
  submitting: boolean;
  stopping: boolean;
  submissionUncertain: boolean;
  checkingSubmission: boolean;
  onReconcileSubmission?: () => void;
  error: string | null;
  action: { href: string; label: string } | null;
}) {
  return (
    <>
      {submissionUncertain ? (
        <div className="flex min-h-11 items-center gap-2 border-t border-warning-border px-3 py-2 type-caption text-[var(--warning-fg)]">
          <span role="status" className="min-w-0 flex-1">提交待确认</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={onReconcileSubmission}
            disabled={!onReconcileSubmission}
            loading={checkingSubmission}
            leftIcon={<RefreshCw className="h-4 w-4" aria-hidden />}
          >
            核对任务
          </Button>
        </div>
      ) : null}
      {submitting || stopping ? (
        <p role="status" className="border-t border-[var(--border-subtle)] px-3 py-2 type-caption text-[var(--fg-1)]">
          {stopping ? "停止请求中，等待服务端确认" : "提交中"}
        </p>
      ) : null}
      {error ? <ComposerError error={error} action={action} /> : null}
    </>
  );
}

function ComposerError({ error, action }: {
  error: string;
  action: { href: string; label: string } | null;
}) {
  return (
    <div className="flex min-h-10 items-center gap-2 border-t border-danger-border bg-danger-soft px-3 py-2 type-caption text-[var(--danger-fg)]">
      <span role="alert" className="min-w-0 flex-1 break-words [overflow-wrap:anywhere]">{error}</span>
      {action ? (
        <Link
          href={action.href}
          className="shrink-0 rounded-[var(--radius-control)] px-2 py-1 font-medium text-[var(--fg-0)] hover:bg-[var(--bg-2)]"
        >
          {action.label}
        </Link>
      ) : null}
    </div>
  );
}
