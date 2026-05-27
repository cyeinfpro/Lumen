"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Archive,
  CircleAlert,
  Database,
  FileArchive,
  FolderOpen,
  HardDrive,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
} from "lucide-react";

import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { Button, Card } from "@/components/ui/primitives";
import { apiFetch } from "@/lib/apiClient";
import {
  clearFailedDockerImportMarker,
  clearFailedRestoreMarker,
  clearPendingRestore,
  exportDesktopBackup,
  getDesktopDockerImportStatus,
  getDesktopRestoreStatus,
  isDesktopRuntime,
  restartDesktopApp,
  selectDockerImportBackup,
  revealDesktopDataDir,
  selectDesktopRestoreBackup,
  type DesktopBackupManifest,
} from "@/lib/desktop/runtime";

interface DesktopDiagnosticsOut {
  data_root: string;
  logs_root: string;
  settings_path: string;
  provider_metadata_path: string;
  bootstrap_complete: boolean;
  disk_free_bytes: number | null;
}

function formatBytes(value: number | null | undefined): string {
  if (value == null) return "未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatDate(ms: number | null | undefined): string {
  if (!ms) return "-";
  return new Date(ms).toLocaleString();
}

function dbSummary(manifest: DesktopBackupManifest | null | undefined): string {
  if (!manifest?.database.present) return "无数据库快照";
  return `${formatBytes(manifest.database.bytes)} · quick_check=${manifest.database.quick_check ?? "-"}`;
}

export default function DesktopStoragePage() {
  const desktop = isDesktopRuntime();
  const [dockerUserId, setDockerUserId] = useState("");
  const diagnosticsQ = useQuery({
    queryKey: ["desktop", "storage-diagnostics"],
    queryFn: () => apiFetch<DesktopDiagnosticsOut>("/settings/diagnostics"),
    enabled: desktop,
    retry: false,
  });
  const restoreQ = useQuery({
    queryKey: ["desktop", "restore-status"],
    queryFn: getDesktopRestoreStatus,
    enabled: desktop,
    retry: false,
  });
  const dockerImportQ = useQuery({
    queryKey: ["desktop", "docker-import-status"],
    queryFn: getDesktopDockerImportStatus,
    enabled: desktop,
    retry: false,
  });
  const backupMut = useMutation({
    mutationFn: exportDesktopBackup,
  });
  const restoreMut = useMutation({
    mutationFn: selectDesktopRestoreBackup,
    onSuccess: () => {
      void restoreQ.refetch();
    },
  });
  const dockerImportMut = useMutation({
    mutationFn: () => selectDockerImportBackup(dockerUserId),
    onSuccess: () => {
      void dockerImportQ.refetch();
    },
  });
  const restartMut = useMutation({
    mutationFn: restartDesktopApp,
  });
  const clearRestoreFailureMut = useMutation({
    mutationFn: clearFailedRestoreMarker,
    onSuccess: () => {
      void restoreQ.refetch();
    },
  });
  const clearPendingRestoreMut = useMutation({
    mutationFn: clearPendingRestore,
    onSuccess: () => {
      void restoreQ.refetch();
    },
  });
  const clearDockerFailureMut = useMutation({
    mutationFn: clearFailedDockerImportMarker,
    onSuccess: () => {
      void dockerImportQ.refetch();
    },
  });

  const latestManifest = backupMut.data?.manifest ?? restoreMut.data?.manifest ?? restoreQ.data?.manifest;

  return (
    <SettingsShell
      title="存储与备份"
      subtitle="本机数据目录、SQLite 快照与恢复任务"
      maxWidth="max-w-4xl"
    >
      <div className="space-y-4">
        {!desktop ? (
          <Card padding="lg">
            <p className="text-sm text-[var(--fg-1)]">
              存储管理仅在桌面端启用。
            </p>
          </Card>
        ) : null}

        <Card padding="lg">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <HardDrive className="h-4 w-4 text-[var(--fg-2)]" />
                <h1 className="type-section-title">数据目录</h1>
              </div>
              <p className="mt-2 break-all font-mono text-[12px] text-[var(--fg-1)]">
                {diagnosticsQ.data?.data_root ?? "未解析"}
              </p>
            </div>
            <Button
              variant="secondary"
              size="sm"
              disabled={!desktop}
              onClick={() => void revealDesktopDataDir()}
              leftIcon={<FolderOpen className="h-3.5 w-3.5" />}
            >
              打开
            </Button>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-3">
            <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-3">
              <div className="text-[12px] text-[var(--fg-2)]">可用空间</div>
              <div className="mt-1 font-mono text-sm text-[var(--fg-0)]">
                {formatBytes(diagnosticsQ.data?.disk_free_bytes)}
              </div>
            </div>
            <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-3">
              <div className="text-[12px] text-[var(--fg-2)]">引导状态</div>
              <div className="mt-1 text-sm text-[var(--fg-0)]">
                {diagnosticsQ.data?.bootstrap_complete ? "已完成" : "未完成"}
              </div>
            </div>
            <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-3">
              <div className="text-[12px] text-[var(--fg-2)]">恢复任务</div>
              <div className="mt-1 text-sm text-[var(--fg-0)]">
                {restoreQ.data?.pending ? "等待重启" : restoreQ.data?.failed ? "上次失败" : "无"}
              </div>
            </div>
          </div>
        </Card>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card padding="lg">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <Archive className="h-4 w-4 text-[var(--fg-2)]" />
                  <h2 className="type-section-title">备份</h2>
                </div>
                <p className="mt-2 text-[13px] text-[var(--fg-2)]">
                  生成 `.lumen-backup.zip`，包含 SQLite 快照、storage、settings 和供应商元数据。
                </p>
              </div>
              <Button
                variant="primary"
                size="sm"
                disabled={!desktop}
                loading={backupMut.isPending}
                onClick={() => backupMut.mutate()}
                leftIcon={!backupMut.isPending ? <Archive className="h-3.5 w-3.5" /> : undefined}
              >
                生成备份
              </Button>
            </div>
            {backupMut.data ? (
              <div className="mt-4 rounded-[var(--radius-card)] border border-success-border bg-success-soft p-3 text-[13px] text-[var(--fg-1)]">
                <div className="flex items-center gap-2 font-medium text-success">
                  <ShieldCheck className="h-4 w-4" />
                  备份完成
                </div>
                <div className="mt-2 break-all font-mono text-[12px] text-[var(--fg-0)]">
                  {backupMut.data.path}
                </div>
                <div className="mt-2 text-[12px] text-[var(--fg-2)]">
                  {formatBytes(backupMut.data.bytes)} · {dbSummary(backupMut.data.manifest)}
                </div>
              </div>
            ) : null}
            {backupMut.isPending ? (
              <div
                role="status"
                className="mt-4 rounded-[var(--radius-card)] border border-[var(--accent)]/35 bg-[var(--bg-1)] p-3 text-[13px] text-[var(--fg-2)]"
              >
                正在生成备份。大目录可能需要几十秒，请保持应用打开。
              </div>
            ) : null}
            {backupMut.error ? (
              <div
                role="alert"
                className="mt-4 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-3 text-[13px] text-danger"
              >
                {backupMut.error.message}
              </div>
            ) : null}
          </Card>

          <Card padding="lg">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <RotateCcw className="h-4 w-4 text-[var(--fg-2)]" />
                  <h2 className="type-section-title">恢复</h2>
                </div>
                <p className="mt-2 text-[13px] text-[var(--fg-2)]">
                  备份包校验通过后会排队，重启时在应用启动前恢复。
                </p>
              </div>
              <Button
                variant="secondary"
                size="sm"
                disabled={!desktop}
                loading={restoreMut.isPending}
                onClick={() => restoreMut.mutate()}
                leftIcon={!restoreMut.isPending ? <RotateCcw className="h-3.5 w-3.5" /> : undefined}
              >
                选择备份
              </Button>
            </div>

            {restoreMut.data ? (
              <div className="mt-4 rounded-[var(--radius-card)] border border-[var(--accent)]/35 bg-[var(--bg-1)] p-3 text-[13px] text-[var(--fg-1)]">
                <div className="font-medium text-[var(--fg-0)]">恢复已排队</div>
                <div className="mt-2 break-all font-mono text-[12px] text-[var(--fg-0)]">
                  {restoreMut.data.source_path}
                </div>
                <div className="mt-2 text-[12px] text-[var(--fg-2)]">
                  {dbSummary(restoreMut.data.manifest)}
                </div>
              </div>
            ) : null}

            {restoreQ.data?.pending ? (
              <div className="mt-4 rounded-[var(--radius-card)] border border-[var(--accent)]/35 bg-[var(--bg-1)] p-3">
                <div className="flex items-start gap-2 text-[13px] text-[var(--fg-1)]">
                  <Database className="mt-0.5 h-4 w-4 text-[var(--fg-2)]" />
                  <div className="min-w-0">
                    <div className="font-medium text-[var(--fg-0)]">等待重启恢复</div>
                    <div className="mt-1 text-[12px] text-[var(--fg-2)]">
                      {formatDate(restoreQ.data.scheduled_at_ms)}
                    </div>
                    <div className="mt-2 break-all font-mono text-[12px] text-[var(--fg-0)]">
                      {restoreQ.data.source_path}
                    </div>
                  </div>
                </div>
                <div className="mt-3 flex flex-wrap justify-end gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    loading={clearPendingRestoreMut.isPending}
                    onClick={() => clearPendingRestoreMut.mutate()}
                  >
                    取消恢复
                  </Button>
                  <Button
                    variant="primary"
                    size="sm"
                    loading={restartMut.isPending}
                    onClick={() => restartMut.mutate()}
                    leftIcon={!restartMut.isPending ? <RefreshCw className="h-3.5 w-3.5" /> : undefined}
                  >
                    重启并恢复
                  </Button>
                </div>
              </div>
            ) : null}

            {restoreQ.data?.failed ? (
              <div className="mt-4 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-3 text-[13px] text-danger">
                <div className="mb-1 flex items-center gap-2 font-medium">
                  <CircleAlert className="h-4 w-4" />
                  上次恢复失败
                </div>
                <div>{restoreQ.data.last_error ?? "未知错误"}</div>
                <div className="mt-3">
                  <Button
                    variant="secondary"
                    size="sm"
                    loading={clearRestoreFailureMut.isPending}
                    onClick={() => clearRestoreFailureMut.mutate()}
                  >
                    清除记录
                  </Button>
                </div>
              </div>
            ) : null}
            {restoreMut.error ? (
              <div
                role="alert"
                className="mt-4 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-3 text-[13px] text-danger"
              >
                {restoreMut.error.message}
              </div>
            ) : null}
          </Card>
        </div>

        <Card padding="lg">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <FileArchive className="h-4 w-4 text-[var(--fg-2)]" />
                <h2 className="type-section-title">Docker 版迁移</h2>
              </div>
              <p className="mt-2 text-[13px] text-[var(--fg-2)]">
                选择 `desktop-export.sh` 生成的数据库导出与 storage 归档；导入会排队到下次启动。
              </p>
            </div>
            <Button
              variant="secondary"
              size="sm"
              disabled={!desktop}
              loading={dockerImportMut.isPending}
              onClick={() => dockerImportMut.mutate()}
              leftIcon={!dockerImportMut.isPending ? <FileArchive className="h-3.5 w-3.5" /> : undefined}
            >
              选择导出
            </Button>
          </div>
          <div className="mt-4 max-w-sm">
            <label className="grid gap-1 text-xs text-[var(--fg-1)]">
              Docker 用户 ID
              <input
                value={dockerUserId}
                onChange={(event) => setDockerUserId(event.target.value)}
                placeholder="单用户导出可留空"
                className="h-9 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)] outline-none transition focus:border-[var(--accent)]"
              />
            </label>
          </div>
          {dockerImportMut.data ? (
            <div className="mt-4 rounded-[var(--radius-card)] border border-[var(--accent)]/35 bg-[var(--bg-1)] p-3 text-[13px] text-[var(--fg-1)]">
              <div className="font-medium text-[var(--fg-0)]">Docker 导入已排队</div>
              <div className="mt-2 break-all font-mono text-[12px] text-[var(--fg-0)]">
                {dockerImportMut.data.source_dump_path}
              </div>
              {dockerImportMut.data.source_storage_tar_path ? (
                <div className="mt-1 break-all font-mono text-[12px] text-[var(--fg-2)]">
                  {dockerImportMut.data.source_storage_tar_path}
                </div>
              ) : null}
            </div>
          ) : null}
          {dockerImportQ.data?.pending ? (
            <div className="mt-4 rounded-[var(--radius-card)] border border-[var(--accent)]/35 bg-[var(--bg-1)] p-3">
              <div className="flex items-start gap-2 text-[13px] text-[var(--fg-1)]">
                <Database className="mt-0.5 h-4 w-4 text-[var(--fg-2)]" />
                <div className="min-w-0">
                  <div className="font-medium text-[var(--fg-0)]">等待重启导入</div>
                  <div className="mt-1 text-[12px] text-[var(--fg-2)]">
                    {formatDate(dockerImportQ.data.scheduled_at_ms)}
                  </div>
                  <div className="mt-2 break-all font-mono text-[12px] text-[var(--fg-0)]">
                    {dockerImportQ.data.source_dump_path}
                  </div>
                </div>
              </div>
              <div className="mt-3 flex justify-end">
                <Button
                  variant="primary"
                  size="sm"
                  loading={restartMut.isPending}
                  onClick={() => restartMut.mutate()}
                  leftIcon={!restartMut.isPending ? <RefreshCw className="h-3.5 w-3.5" /> : undefined}
                >
                  重启并导入
                </Button>
              </div>
            </div>
          ) : null}
          {dockerImportQ.data?.failed ? (
            <div className="mt-4 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-3 text-[13px] text-danger">
              <div className="mb-1 flex items-center gap-2 font-medium">
                <CircleAlert className="h-4 w-4" />
                上次 Docker 导入失败
              </div>
              <div>{dockerImportQ.data.last_error ?? "未知错误"}</div>
              <div className="mt-3">
                <Button
                  variant="secondary"
                  size="sm"
                  loading={clearDockerFailureMut.isPending}
                  onClick={() => clearDockerFailureMut.mutate()}
                >
                  清除记录
                </Button>
              </div>
            </div>
          ) : null}
          {dockerImportMut.error ? (
            <div
              role="alert"
              className="mt-4 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-3 text-[13px] text-danger"
            >
              {dockerImportMut.error.message}
            </div>
          ) : null}
        </Card>

        {latestManifest ? (
          <Card padding="lg">
            <div className="flex items-center gap-2">
              <Database className="h-4 w-4 text-[var(--fg-2)]" />
              <h2 className="type-section-title">最近备份信息</h2>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-3">
                <div className="text-[12px] text-[var(--fg-2)]">版本</div>
                <div className="mt-1 font-mono text-sm text-[var(--fg-0)]">
                  {latestManifest.app_version}
                </div>
              </div>
              <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-3">
                <div className="text-[12px] text-[var(--fg-2)]">创建时间</div>
                <div className="mt-1 text-sm text-[var(--fg-0)]">
                  {formatDate(latestManifest.created_at_ms)}
                </div>
              </div>
              <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-3">
                <div className="text-[12px] text-[var(--fg-2)]">文件数</div>
                <div className="mt-1 font-mono text-sm text-[var(--fg-0)]">
                  {latestManifest.entries.length}
                </div>
              </div>
            </div>
          </Card>
        ) : null}
      </div>
    </SettingsShell>
  );
}
