"use client";

import { useMemo } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Clock3,
  Info,
  Rocket,
  RotateCcw,
  Undo2,
  X,
} from "lucide-react";

import type {
  AdminUpdateCheckOut,
  AdminUpdateStatusOut,
  AdminUpdateVersionOut,
} from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/primitives";
import { MarkdownPreview } from "@/components/markdown/MarkdownPreview";

type Props = {
  check?: AdminUpdateCheckOut | null;
  status?: AdminUpdateStatusOut | null;
  version?: AdminUpdateVersionOut | null;
  checking?: boolean;
  triggering?: boolean;
  onCheck: (force?: boolean) => void;
  onTrigger: () => void;
  onRollbackPrevious: () => void;
  compact?: boolean;
  showRollbackPrevious?: boolean;
};

type UpdateCardState =
  | "RUNNING"
  | "FAILED"
  | "UPDATE_AVAILABLE"
  | "UP_TO_DATE"
  | "UNKNOWN";

function stateFor(
  check?: AdminUpdateCheckOut | null,
  status?: AdminUpdateStatusOut | null,
): UpdateCardState {
  if (status?.running) return "RUNNING";
  if (status?.phases?.some((p) => p.status === "done" && (p.rc ?? 0) !== 0)) {
    return "FAILED";
  }
  if (!check) return "UNKNOWN";
  if (check.warning && !check.release) return "UNKNOWN";
  if (check.has_update) return "UPDATE_AVAILABLE";
  if (check.has_update === false) return "UP_TO_DATE";
  return "UNKNOWN";
}

function currentVersionFor(
  check?: AdminUpdateCheckOut | null,
  version?: AdminUpdateVersionOut | null,
): string {
  return check?.current_version ?? version?.version ?? "unknown";
}

function currentLineFor(
  check?: AdminUpdateCheckOut | null,
  version?: AdminUpdateVersionOut | null,
): string {
  const currentVersion = currentVersionFor(check, version);
  const channel = check?.channel ?? version?.channel ?? "stable";
  const buildType = check?.build_type ?? version?.build_type ?? "unknown";
  return `${currentVersion} · ${channel} · ${buildType}`;
}

function resolvedUpdateTag(check?: AdminUpdateCheckOut | null): string {
  return check?.resolved_image_tag ?? check?.latest_version ?? "";
}

function stateTitle(
  state: UpdateCardState,
  check?: AdminUpdateCheckOut | null,
): string {
  switch (state) {
    case "RUNNING":
      return "更新进行中";
    case "FAILED":
      return "更新失败";
    case "UPDATE_AVAILABLE":
      return `发现新版本 ${resolvedUpdateTag(check)}`;
    case "UP_TO_DATE":
      return "当前已是最新";
    default:
      return "更新状态未知";
  }
}

function stateIconClass(state: UpdateCardState): string {
  switch (state) {
    case "RUNNING":
      return "border-info-border bg-info-soft";
    case "FAILED":
      return "border-danger-border bg-danger-soft";
    case "UPDATE_AVAILABLE":
      return "border-warning-border bg-warning-soft";
    default:
      return "border-success-border bg-success-soft";
  }
}

function UpdateStateIcon({ state }: { state: UpdateCardState }) {
  switch (state) {
    case "RUNNING":
      return <Clock3 className="h-4 w-4 text-info" />;
    case "FAILED":
      return <X className="h-4 w-4 text-danger" />;
    case "UPDATE_AVAILABLE":
      return <AlertTriangle className="h-4 w-4 text-warning" />;
    default:
      return <Check className="h-4 w-4 text-success" />;
  }
}

function UpdateMetadata({
  check,
  version,
  compact,
}: {
  check?: AdminUpdateCheckOut | null;
  version?: AdminUpdateVersionOut | null;
  compact: boolean;
}) {
  const cache = check?.cache;
  return (
    <div className="mt-3 flex flex-wrap gap-1.5 text-[11px] text-[var(--fg-2)]">
      <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1">
        current {currentVersionFor(check, version)}
      </span>
      <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1">
        latest {check?.latest_version ?? "unknown"}
      </span>
      {!compact && (
        <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1">
          cache {cache ? (cache.cached ? "hit" : "miss") : "unknown"}
          {cache?.stale ? " · stale" : ""}
        </span>
      )}
      {check?.warm_pull?.state && (
        <span className="rounded-[var(--radius-control)] border border-info-border bg-info-soft px-2 py-1 text-info">
          warm pull {check.warm_pull.state}
        </span>
      )}
      {check?.warning && (
        <span className="rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-2 py-1 text-warning">
          {check.warning}
        </span>
      )}
    </div>
  );
}

function UpdateSummary({
  check,
  version,
  state,
  currentLine,
  compact,
}: {
  check?: AdminUpdateCheckOut | null;
  version?: AdminUpdateVersionOut | null;
  state: UpdateCardState;
  currentLine: string;
  compact: boolean;
}) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        <div
          className={cn(
            "flex shrink-0 items-center justify-center rounded-[var(--radius-card)] border",
            compact ? "h-8 w-8" : "h-9 w-9",
            stateIconClass(state),
          )}
        >
          <UpdateStateIcon state={state} />
        </div>
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium text-[var(--fg-0)]">
            {stateTitle(state, check)}
          </h3>
          <p className="mt-1 truncate text-xs leading-5 text-[var(--fg-2)]">
            {currentLine}
          </p>
        </div>
      </div>
      <UpdateMetadata check={check} version={version} compact={compact} />
    </div>
  );
}

function updateButtonLabel(
  state: UpdateCardState,
  check?: AdminUpdateCheckOut | null,
): string {
  switch (state) {
    case "RUNNING":
      return "更新进行中";
    case "UPDATE_AVAILABLE":
      return `立即更新到 ${resolvedUpdateTag(check)}`;
    case "UP_TO_DATE":
      return "已是最新";
    case "UNKNOWN":
      return "重新检查";
    default:
      return "检查更新";
  }
}

function UpdateActions({
  check,
  state,
  checking,
  triggering,
  showRollbackPrevious,
  onCheck,
  onTrigger,
  onRollbackPrevious,
}: {
  check?: AdminUpdateCheckOut | null;
  state: UpdateCardState;
  checking?: boolean;
  triggering?: boolean;
  showRollbackPrevious: boolean;
  onCheck: (force?: boolean) => void;
  onTrigger: () => void;
  onRollbackPrevious: () => void;
}) {
  const running = state === "RUNNING";
  const hasUpdate = state === "UPDATE_AVAILABLE";
  const busy = Boolean(triggering || running);
  return (
    <div className="flex flex-wrap gap-2 lg:justify-end">
      <Button
        variant="secondary"
        size="sm"
        onClick={() => onCheck(true)}
        disabled={checking}
        loading={checking}
        leftIcon={!checking ? <RotateCcw className="h-3.5 w-3.5" /> : undefined}
      >
        重新检查
      </Button>
      <Button
        variant={hasUpdate ? "primary" : "secondary"}
        size="sm"
        onClick={hasUpdate ? onTrigger : undefined}
        disabled={busy || checking || !hasUpdate}
        loading={busy}
        leftIcon={!busy ? <Rocket className="h-3.5 w-3.5" /> : undefined}
      >
        {updateButtonLabel(state, check)}
      </Button>
      {showRollbackPrevious && (
        <Button
          variant="secondary"
          size="sm"
          onClick={onRollbackPrevious}
          disabled={busy}
          leftIcon={<Undo2 className="h-3.5 w-3.5" />}
        >
          回滚上一版
        </Button>
      )}
    </div>
  );
}

function ReleaseSummary({
  check,
  state,
  compact,
}: {
  check?: AdminUpdateCheckOut | null;
  state: UpdateCardState;
  compact: boolean;
}) {
  if (state !== "UPDATE_AVAILABLE" || !check?.release || compact) return null;
  return (
    <details className="mt-4 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/70 p-3">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-xs text-[var(--fg-1)]">
        <Info className="h-3.5 w-3.5 text-[var(--fg-2)]" />
        查看变更摘要
        <ChevronDown className="ml-auto h-3.5 w-3.5 text-[var(--fg-2)]" />
      </summary>
      <div className="mt-3 space-y-3">
        <div className="text-[11px] text-[var(--fg-2)]">
          发布于 {check.release.published_at ?? "unknown"}
        </div>
        <MarkdownPreview
          bodyHtml={check.release.body_html ?? ""}
          limitLines={6}
          className="overflow-auto rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] p-3 text-xs leading-6 text-[var(--fg-1)]"
        />
      </div>
    </details>
  );
}

export function UpdateAvailableCard({
  check,
  status,
  version,
  checking,
  triggering,
  onCheck,
  onTrigger,
  onRollbackPrevious,
  compact = false,
  showRollbackPrevious = true,
}: Props) {
  const state = stateFor(check, status);
  const currentLine = useMemo(() => currentLineFor(check, version), [check, version]);

  return (
    <section
      className={cn(
        "rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/70 shadow-[var(--shadow-1)] backdrop-blur-sm",
        compact ? "p-3" : "p-4",
      )}
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <UpdateSummary
          check={check}
          version={version}
          state={state}
          currentLine={currentLine}
          compact={compact}
        />
        <UpdateActions
          check={check}
          state={state}
          checking={checking}
          triggering={triggering}
          showRollbackPrevious={showRollbackPrevious}
          onCheck={onCheck}
          onTrigger={onTrigger}
          onRollbackPrevious={onRollbackPrevious}
        />
      </div>

      <ReleaseSummary check={check} state={state} compact={compact} />
    </section>
  );
}
