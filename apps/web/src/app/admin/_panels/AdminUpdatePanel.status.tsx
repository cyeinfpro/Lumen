"use client";

import {
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  RotateCcw,
  Terminal,
  Undo2,
  X,
} from "lucide-react";

import type { UpdateStepRecord } from "@/lib/apiClient";
import { Button, IconButton } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { cn } from "@/lib/utils";
import {
  formatDateTime,
  phaseLabel,
  type AdminStreamStatus,
  type UpdateBanner,
} from "./AdminUpdatePanel.helpers";

type UpdateConsoleState = "rollback" | "running" | "failed" | "complete" | "idle";

function updateConsoleState(
  running: boolean,
  failed: boolean,
  isRollingBack: boolean,
  hasPhases: boolean,
): UpdateConsoleState {
  if (running && isRollingBack) return "rollback";
  if (running) return "running";
  if (failed) return "failed";
  if (hasPhases) return "complete";
  return "idle";
}

function updateConsoleIconClass(state: UpdateConsoleState): string {
  switch (state) {
    case "rollback":
    case "running":
      return "border-info-border bg-info-soft";
    case "failed":
      return "border-danger-border bg-danger-soft";
    default:
      return "border-[var(--border)] bg-[var(--bg-2)]";
  }
}

function UpdateConsoleIcon({ state }: { state: UpdateConsoleState }) {
  switch (state) {
    case "rollback":
    case "running":
      return <Loader2 className="h-4 w-4 animate-spin text-info" />;
    case "failed":
      return <X className="h-4 w-4 text-danger" />;
    default:
      return <Terminal className="h-4 w-4 text-[var(--fg-2)]" />;
  }
}

function updateConsolePillClass(state: UpdateConsoleState): string {
  switch (state) {
    case "rollback":
      return "border-warning-border bg-warning-soft text-warning";
    case "running":
      return "border-info-border bg-info-soft text-info";
    case "failed":
      return "border-danger-border bg-danger-soft text-danger";
    case "complete":
      return "border-success-border bg-success-soft text-success";
    default:
      return "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]";
  }
}

function updateConsolePillLabel(state: UpdateConsoleState): string {
  switch (state) {
    case "rollback":
      return "回滚运行中";
    case "running":
      return "更新运行中";
    case "failed":
      return "上次失败";
    case "complete":
      return "上次完成";
    default:
      return "空闲";
  }
}

function updateConsoleSubtitle({
  state,
  runningTarget,
  activePhase,
  startedAt,
}: {
  state: UpdateConsoleState;
  runningTarget: string;
  activePhase: UpdateStepRecord | null;
  startedAt?: string | null;
}): string {
  if (state === "rollback" || state === "running") {
    return `${runningTarget} · ${phaseLabel(activePhase?.phase ?? "")}`;
  }
  if (state === "failed") {
    return `失败于 ${phaseLabel(activePhase?.phase ?? "")}`;
  }
  if (startedAt) return `最近任务 ${formatDateTime(startedAt)}`;
  return "步骤、实时输出和发布历史已收起。";
}

export function UpdateConsoleHeader({
  running,
  failed,
  isRollingBack,
  phases,
  runningTarget,
  activePhase,
  startedAt,
  loading,
  disabled,
  detailsOpen,
  onRefresh,
  onRollbackPrevious,
  onDetailsToggle,
}: {
  running: boolean;
  failed: boolean;
  isRollingBack: boolean;
  phases: UpdateStepRecord[];
  runningTarget: string;
  activePhase: UpdateStepRecord | null;
  startedAt?: string | null;
  loading: boolean;
  disabled: boolean;
  detailsOpen: boolean;
  onRefresh: () => void;
  onRollbackPrevious: () => void;
  onDetailsToggle: () => void;
}) {
  const state = updateConsoleState(running, failed, isRollingBack, phases.length > 0);
  return (
    <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
      <div className="flex min-w-0 gap-3">
        <div
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-card)] border",
            updateConsoleIconClass(state),
          )}
        >
          <UpdateConsoleIcon state={state} />
        </div>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="type-card-title ">更新控制台</h3>
            <span
              className={cn(
                "rounded-[var(--radius-control)] border px-2 py-0.5 type-caption",
                updateConsolePillClass(state),
              )}
            >
              {updateConsolePillLabel(state)}
            </span>
          </div>
          <p className="mt-1 truncate type-caption text-[var(--fg-2)]">
            {updateConsoleSubtitle({
              state,
              runningTarget,
              activePhase,
              startedAt,
            })}
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-2 md:justify-end">
        <Button
          variant="secondary"
          size="sm"
          onClick={onRefresh}
          disabled={loading}
          loading={loading}
          leftIcon={!loading ? <RotateCcw className="h-3.5 w-3.5" /> : undefined}
        >
          刷新
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={onRollbackPrevious}
          disabled={disabled}
          loading={isRollingBack}
          leftIcon={!isRollingBack ? <Undo2 className="h-3.5 w-3.5" /> : undefined}
        >
          回滚上一版
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={onDetailsToggle}
          leftIcon={
            detailsOpen ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )
          }
        >
          {detailsOpen ? "收起详情" : "查看详情"}
        </Button>
      </div>
    </div>
  );
}

function streamStatusClass(status: AdminStreamStatus): string {
  switch (status) {
    case "open":
      return "border-success-border bg-success-soft text-success";
    case "connecting":
      return "border-info-border bg-info-soft text-info";
    case "broken":
      return "border-danger-border bg-danger-soft text-danger";
    default:
      return "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]";
  }
}

function streamStatusLabel(status: AdminStreamStatus): string {
  switch (status) {
    case "open":
      return "已连接";
    case "connecting":
      return "连接中";
    case "broken":
      return "中断，刷新";
    case "error":
      return "重连中";
    default:
      return "未连接";
  }
}

export function UpdateConsoleMeta({
  running,
  streamStatus,
  phaseCount,
  completedCount,
  totalCount,
  logCount,
}: {
  running: boolean;
  streamStatus: AdminStreamStatus;
  phaseCount: number;
  completedCount: number;
  totalCount: number;
  logCount: number;
}) {
  return (
    <div className="mt-3 flex flex-wrap gap-2 type-caption">
      {running && (
        <span
          className={cn(
            "rounded-[var(--radius-control)] border px-2 py-1",
            streamStatusClass(streamStatus),
          )}
        >
          实时流：{streamStatusLabel(streamStatus)}
        </span>
      )}
      {phaseCount > 0 && (
        <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-[var(--fg-1)]">
          步骤 {completedCount}/{totalCount}
        </span>
      )}
      {logCount > 0 && (
        <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 font-mono text-[var(--fg-2)]">
          日志 {logCount}
        </span>
      )}
    </div>
  );
}

export function UpdateStatusError({ error }: { error: Error | null }) {
  if (!error) return null;
  return (
    <p className="mt-3 type-caption text-danger">
      更新状态读取失败：{error.message}
    </p>
  );
}

function bannerClass(kind: UpdateBanner["kind"]): string {
  switch (kind) {
    case "success":
      return "border-success-border bg-success-soft text-success";
    case "error":
      return "border-danger-border bg-danger-soft text-danger";
    default:
      return "border-info-border bg-info-soft text-info";
  }
}

export function UpdateBannerNotice({
  banner,
  onClear,
}: {
  banner: UpdateBanner | null;
  onClear: () => void;
}) {
  if (!banner) return null;
  return (
    <div
      className={cn(
        "mt-3 flex items-start justify-between gap-3 rounded-[var(--radius-card)] border px-3 py-2 type-body-sm",
        bannerClass(banner.kind),
      )}
    >
      <span className="min-w-0 break-words">{banner.text}</span>
      <IconButton
        variant="ghost"
        size="sm"
        onClick={onClear}
        aria-label={copy.action.close}
        className="shrink-0"
      >
        <X className="h-3.5 w-3.5" />
      </IconButton>
    </div>
  );
}

export function ReloadNotice({
  countdown,
  onCancel,
  onReload,
}: {
  countdown: number | null;
  onCancel: () => void;
  onReload: () => void;
}) {
  if (countdown == null) return null;
  return (
    <div className="mt-3 flex items-center justify-between gap-3 rounded-[var(--radius-card)] border border-success-border bg-success-soft px-3 py-2.5 type-body-sm text-success">
      <div className="flex min-w-0 items-center gap-2">
        <Check className="h-4 w-4 shrink-0 text-success" />
        <span className="min-w-0">
          更新成功 · <span className="font-mono">{countdown}s</span>{" "}
          后自动刷新页面以加载新版本
        </span>
      </div>
      <div className="flex shrink-0 gap-1.5">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 type-caption text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)]"
        >
          {copy.action.cancel}
        </button>
        <button
          type="button"
          onClick={onReload}
          className="rounded-[var(--radius-control)] bg-success px-2 py-1 type-caption font-medium text-[var(--success-on)] transition-[filter] hover:brightness-110"
        >
          立即刷新
        </button>
      </div>
    </div>
  );
}

function progressLabel(
  running: boolean,
  failed: boolean,
  activePhase: UpdateStepRecord | null,
): string {
  if (running) return `执行中：${phaseLabel(activePhase?.phase ?? "")}`;
  if (failed) return `失败于：${phaseLabel(activePhase?.phase ?? "")}`;
  return "更新已完成";
}

function progressClass(running: boolean, failed: boolean): string {
  if (failed) return "bg-danger/80";
  if (running) return "bg-info/80";
  return "bg-success/80";
}

export function UpdateProgress({
  visible,
  running,
  failed,
  activePhase,
  progressPct,
}: {
  visible: boolean;
  running: boolean;
  failed: boolean;
  activePhase: UpdateStepRecord | null;
  progressPct: number;
}) {
  if (!visible) return null;
  return (
    <div className="mt-3">
      <div className="flex items-center justify-between gap-3">
        <span className="truncate type-caption font-medium text-[var(--fg-1)]">
          {progressLabel(running, failed, activePhase)}
        </span>
        <span className="shrink-0 font-mono type-caption text-[var(--fg-2)]">
          {progressPct}%
        </span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <div
          className={cn(
            "h-full transition-[width] duration-500 ease-out",
            progressClass(running, failed),
          )}
          style={{ width: `${progressPct}%` }}
        />
      </div>
    </div>
  );
}
