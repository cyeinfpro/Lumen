import type { QueryClient } from "@tanstack/react-query";

import { qk } from "@/lib/queries";
import {
  ApiError,
  type AdminRollbackOut,
  type AdminUpdateStatusOut,
  type AdminUpdateTriggerOut,
  type UpdateStepRecord,
} from "@/lib/apiClient";

export const PHASE_ORDER: readonly string[] = [
  "lock",
  "self_update_scripts",
  "check",
  "preflight",
  "backup_preflight",
  "fetch_release",
  "set_image_tag",
  "pull_images",
  "start_infra",
  "migrate_db",
  "switch",
  "check_storage",
  "restart_services",
  "refresh_update_runner",
  "health_check",
  "cleanup",
];

const PHASE_LABEL: Record<string, string> = {
  lock: "获取锁",
  self_update_scripts: "刷新脚本",
  check: "检查版本",
  preflight: "预检查",
  backup_preflight: "更新前备份",
  fetch_release: "准备发布目录",
  set_image_tag: "写入镜像标签",
  pull_images: "拉取镜像",
  start_infra: "启动基础设施",
  migrate_db: "数据库迁移",
  switch: "原子切换",
  check_storage: "检查存储",
  restart_services: "重启服务",
  refresh_update_runner: "刷新更新入口",
  health_check: "健康检查",
  health_post: "健康检查",
  cleanup: "清理旧版本",
  rollback: "回滚",
};

export type AdminStreamStatus =
  | "idle"
  | "connecting"
  | "open"
  | "error"
  | "broken";

export interface AdminUpdateStreamHandle {
  logBuffer: string[];
  streamStatus: AdminStreamStatus;
  clearLogs: () => void;
}

export type UpdateBanner = {
  kind: "success" | "error" | "info";
  text: string;
};

export type PendingUpdateConfirm = {
  targetTag: string;
  channel: string | null;
};

function valueOr<T>(value: T | null | undefined, fallback: T): T {
  return value ?? fallback;
}

function runningStatus(
  previous: AdminUpdateStatusOut | undefined,
  startedAt: string,
  identity?: { pid?: number | null; unit?: string | null },
): AdminUpdateStatusOut {
  const previousPid = valueOr(previous?.pid, null);
  const previousUnit = valueOr(previous?.unit, null);
  return {
    running: true,
    pid: valueOr(identity?.pid, previousPid),
    unit: valueOr(identity?.unit, previousUnit),
    started_at: startedAt,
    log_tail: valueOr(previous?.log_tail, ""),
    phases: valueOr(previous?.phases, []),
    current_release: valueOr(previous?.current_release, null),
    previous_release: valueOr(previous?.previous_release, null),
    releases: valueOr(previous?.releases, []),
  };
}

export function setRunningUpdateStatus(
  queryClient: QueryClient,
  startedAt: string,
  identity?: { pid?: number | null; unit?: string | null },
) {
  queryClient.setQueryData<AdminUpdateStatusOut | undefined>(
    qk.adminUpdateStatus(),
    (previous) => runningStatus(previous, startedAt, identity),
  );
}

export function triggerStartedText(result: AdminUpdateTriggerOut): string {
  const target = result.unit ? `任务 ${result.unit}` : `进程 ${result.pid ?? "-"}`;
  const details = [`更新已启动，${target}`];
  if (result.proxy_name) details.push(`代理 ${result.proxy_name}`);
  if (result.target_tag) details.push(`目标 ${result.target_tag}`);
  return details.join("，");
}

export function rollbackStartedBanner(
  result: AdminRollbackOut,
  previous: boolean,
): UpdateBanner {
  if (previous) {
    return {
      kind: "info",
      text: `已启动回滚到上一版本 ${result.target.id}`,
    };
  }
  return {
    kind: "success",
    text: `回滚已启动，目标 release ${result.target.id}`,
  };
}

export function mutationErrorText(error: Error, fallback: string): string {
  if (error instanceof ApiError) return error.message;
  return error.message || fallback;
}

export function anyPending(...values: boolean[]): boolean {
  return values.some(Boolean);
}

export function updatePollInterval(
  streamConnected: boolean,
  running?: boolean,
): number | false {
  if (streamConnected) return false;
  return running ? 5000 : false;
}

export function rollbackPendingIdFor(
  previousPending: boolean,
  rollbackPending: boolean,
  releaseId: string | undefined,
): string | null {
  if (previousPending) return "__previous__";
  if (rollbackPending) return releaseId ?? null;
  return null;
}

export function updateRunningFor(
  status: AdminUpdateStatusOut | undefined,
): boolean {
  return Boolean(status?.running);
}

export function runningTargetFor(
  status: AdminUpdateStatusOut | undefined,
): string {
  if (status?.unit) return `unit ${status.unit}`;
  return `pid ${status?.pid ?? "-"}`;
}

export function phasesFor(
  status: AdminUpdateStatusOut | undefined,
): UpdateStepRecord[] {
  return status?.phases ?? [];
}

export function progressPercent(completed: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((completed / total) * 100);
}

export function effectiveUpdateBanner(
  banner: UpdateBanner | null,
  failed: boolean,
  running: boolean,
): UpdateBanner | null {
  if (banner) return banner;
  if (failed && !running) {
    return {
      kind: "error",
      text: "上次更新失败，请查看 checklist 中的红色 phase 或日志。",
    };
  }
  return null;
}

export function phaseLabel(phase: string): string {
  return PHASE_LABEL[phase] ?? phase;
}

export function formatDuration(
  ms: number | null | undefined,
): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const totalSec = ms / 1000;
  if (totalSec < 60) return `${totalSec.toFixed(1)}s`;
  const minutes = Math.floor(totalSec / 60);
  const seconds = Math.round(totalSec - minutes * 60);
  return `${minutes}m${seconds.toString().padStart(2, "0")}s`;
}

export function shortReleaseId(id: string): string {
  if (id.length <= 28) return id;
  return `${id.slice(0, 28)}…`;
}

export function shortSha(sha?: string | null): string {
  if (!sha) return "未知";
  return sha.length > 7 ? sha.slice(0, 7) : sha;
}

export function formatDateTime(value: string): string {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(value));
  } catch {
    return value;
  }
}
