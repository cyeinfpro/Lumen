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
};

function stateFor(
  check?: AdminUpdateCheckOut | null,
  status?: AdminUpdateStatusOut | null,
): "RUNNING" | "FAILED" | "UPDATE_AVAILABLE" | "UP_TO_DATE" | "UNKNOWN" {
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

export function UpdateAvailableCard({
  check,
  status,
  version,
  checking,
  triggering,
  onCheck,
  onTrigger,
  onRollbackPrevious,
}: Props) {
  const state = stateFor(check, status);
  const running = state === "RUNNING";
  const failed = state === "FAILED";
  const hasUpdate = state === "UPDATE_AVAILABLE";
  const upToDate = state === "UP_TO_DATE";
  const unknown = state === "UNKNOWN";

  const releaseHtml = check?.release?.body_html ?? "";
  const currentLine = useMemo(() => {
    const cur = check?.current_version ?? version?.version ?? "unknown";
    return `${cur} · ${check?.channel ?? version?.channel ?? "stable"} · ${
      check?.build_type ?? version?.build_type ?? "unknown"
    }`;
  }, [check, version]);

  const buttonLabel = running
    ? "更新进行中"
    : hasUpdate
      ? `立即更新到 ${check?.resolved_image_tag ?? check?.latest_version ?? ""}`
      : upToDate
        ? "已是最新"
        : unknown
          ? "重新检查"
          : "检查更新";

  return (
    <section className="rounded-xl border border-white/10 bg-[var(--bg-1)]/60 p-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <div
              className={cn(
                "flex h-9 w-9 items-center justify-center rounded-xl border",
                running
                  ? "border-info-border bg-info-soft"
                  : failed
                    ? "border-danger-border bg-danger-soft"
                    : hasUpdate
                      ? "border-warning-border bg-warning-soft"
                      : "border-success-border bg-success-soft",
              )}
            >
              {running ? (
                <Clock3 className="h-4 w-4 text-info" />
              ) : failed ? (
                <X className="h-4 w-4 text-danger" />
              ) : hasUpdate ? (
                <AlertTriangle className="h-4 w-4 text-warning" />
              ) : (
                <Check className="h-4 w-4 text-success" />
              )}
            </div>
            <div className="min-w-0">
              <h3 className="text-sm font-medium text-neutral-100">
                {running
                  ? "更新进行中"
                  : failed
                    ? "更新失败"
                    : hasUpdate
                      ? `发现新版本 ${check?.resolved_image_tag ?? check?.latest_version}`
                      : upToDate
                        ? "当前已是最新"
                        : "更新状态未知"}
              </h3>
              <p className="mt-1 text-xs leading-5 text-neutral-500">{currentLine}</p>
            </div>
          </div>

          <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-neutral-400">
            <span className="rounded-md border border-white/10 bg-white/[0.04] px-2 py-1">
              current {check?.current_version ?? version?.version ?? "unknown"}
            </span>
            <span className="rounded-md border border-white/10 bg-white/[0.04] px-2 py-1">
              latest {check?.latest_version ?? "unknown"}
            </span>
            <span className="rounded-md border border-white/10 bg-white/[0.04] px-2 py-1">
              cache {check?.cache.cached ? "hit" : "miss"}
              {check?.cache.stale ? " · stale" : ""}
            </span>
            {check?.warm_pull?.state && (
              <span className="rounded-md border border-info-border bg-info-soft px-2 py-1 text-info">
                warm pull {check.warm_pull.state}
              </span>
            )}
            {check?.warning && (
              <span className="rounded-md border border-warning-border bg-warning-soft px-2 py-1 text-warning">
                {check.warning}
              </span>
            )}
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
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
            disabled={triggering || running || checking || !hasUpdate}
            loading={triggering || running}
            leftIcon={!(triggering || running) ? <Rocket className="h-3.5 w-3.5" /> : undefined}
          >
            {buttonLabel}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={onRollbackPrevious}
            disabled={triggering || running}
            leftIcon={<Undo2 className="h-3.5 w-3.5" />}
          >
            回滚上一版
          </Button>
        </div>
      </div>

      {hasUpdate && check?.release && (
        <details className="mt-4 rounded-lg border border-[var(--border)] bg-[var(--bg-0)]/70 p-3">
          <summary className="flex cursor-pointer list-none items-center gap-2 text-xs text-neutral-300">
            <Info className="h-3.5 w-3.5 text-neutral-400" />
            查看变更摘要
            <ChevronDown className="ml-auto h-3.5 w-3.5 text-neutral-500" />
          </summary>
          <div className="mt-3 space-y-3">
            <div className="text-[11px] text-neutral-500">
              发布于 {check.release.published_at ?? "unknown"}
            </div>
            <MarkdownPreview
              bodyHtml={releaseHtml}
              limitLines={6}
              className="overflow-auto rounded-md border border-white/8 bg-black/20 p-3 text-xs leading-6 text-[var(--fg-1)]"
            />
          </div>
        </details>
      )}
    </section>
  );
}
