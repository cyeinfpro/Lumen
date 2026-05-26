"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import {
  Activity,
  CheckCircle2,
  CircleAlert,
  Database,
  FileArchive,
  FolderOpen,
  HardDrive,
  RefreshCw,
  Server,
  ShieldCheck,
} from "lucide-react";

import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { Button, Card } from "@/components/ui/primitives";
import { apiFetch } from "@/lib/apiClient";
import {
  getDesktopStatus,
  exportDiagnosticsBundle,
  isDesktopRuntime,
  revealDesktopDataDir,
  type DesktopStatus,
} from "@/lib/desktop/runtime";

interface DesktopDiagnosticsOut {
  data_root: string;
  logs_root: string;
  settings_path: string;
  provider_metadata_path: string;
  bootstrap_complete: boolean;
  disk_free_bytes: number | null;
}

function formatBytes(value: number | null): string {
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

function Row({
  icon,
  label,
  value,
}: {
  icon: ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="grid grid-cols-[28px_120px_minmax(0,1fr)] items-center gap-3 border-b border-[var(--border-subtle)] py-3 last:border-b-0">
      <span className="flex h-7 w-7 items-center justify-center rounded-[var(--radius-control)] bg-[var(--bg-2)] text-[var(--fg-1)]">
        {icon}
      </span>
      <span className="text-[13px] text-[var(--fg-2)]">{label}</span>
      <code className="min-w-0 truncate rounded-[var(--radius-control)] bg-[var(--bg-1)] px-2 py-1 font-mono text-[12px] text-[var(--fg-0)]">
        {value}
      </code>
    </div>
  );
}

function RuntimeComponentPanel({ status }: { status: DesktopStatus | undefined }) {
  const expected = [
    { name: "redis", label: "本机缓存", port: status?.runtime.redis_port },
    { name: "api", label: "业务接口", port: status?.runtime.api_port },
    { name: "worker", label: "任务引擎", port: status?.runtime.worker_metrics_port },
    { name: "web", label: "界面服务", port: status?.runtime.web_port },
  ];
  const byName = new Map((status?.sidecars ?? []).map((item) => [item.name, item]));

  return (
    <div className="grid gap-3 md:grid-cols-2">
      {expected.map((item) => {
        const sidecar = byName.get(item.name);
        const running = Boolean(sidecar?.running);
        const ready = Boolean(sidecar?.ready);
        return (
          <div
            key={item.name}
            className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-4"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <span className="flex h-8 w-8 items-center justify-center rounded-[var(--radius-control)] bg-[var(--bg-2)] text-[var(--fg-1)]">
                  <Server className="h-4 w-4" />
                </span>
                <div className="min-w-0">
                  <div className="text-sm font-medium text-[var(--fg-0)]">{item.label}</div>
                  <div className="mt-0.5 text-[12px] text-[var(--fg-2)]">
                    {item.port ? `127.0.0.1:${item.port}` : "端口未分配"}
                  </div>
                </div>
              </div>
              <span
                className={
                  ready
                    ? "inline-flex items-center gap-1 rounded-full border border-success-border bg-success-soft px-2 py-1 text-[12px] text-success"
                    : "inline-flex items-center gap-1 rounded-full border border-danger-border bg-danger-soft px-2 py-1 text-[12px] text-danger"
                }
              >
                {ready ? <CheckCircle2 className="h-3.5 w-3.5" /> : <CircleAlert className="h-3.5 w-3.5" />}
                {ready ? "正常" : running ? "启动中" : "异常"}
              </span>
            </div>
            <div className="mt-3 grid grid-cols-2 gap-2 text-[12px] text-[var(--fg-2)]">
              <div>
                PID <span className="font-mono text-[var(--fg-0)]">{sidecar?.pid ?? "-"}</span>
              </div>
              <div className="truncate text-right">
                RSS <span className="font-mono text-[var(--fg-0)]">{formatBytes(sidecar?.rss_bytes ?? null)}</span>
              </div>
              <div>
                重启 <span className="font-mono text-[var(--fg-0)]">{sidecar?.restart_count ?? 0}</span>
              </div>
              <div className="truncate text-right" title={sidecar?.last_restart_reason ?? sidecar?.exit_status ?? ""}>
                {sidecar?.last_restart_reason ?? sidecar?.exit_status ?? "正常"}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function DiagnosticsPage() {
  const desktop = isDesktopRuntime();
  const q = useQuery({
    queryKey: ["desktop", "diagnostics"],
    queryFn: () => apiFetch<DesktopDiagnosticsOut>("/settings/diagnostics"),
    enabled: desktop,
    retry: false,
  });
  const statusQ = useQuery({
    queryKey: ["desktop", "runtime-status"],
    queryFn: getDesktopStatus,
    enabled: desktop,
    retry: false,
    refetchInterval: 5_000,
  });
  const exportMut = useMutation({
    mutationFn: exportDiagnosticsBundle,
  });

  return (
    <SettingsShell
      title="诊断"
      subtitle="本机数据、日志与内置运行时状态"
      maxWidth="max-w-4xl"
    >
      <div className="space-y-4">
        {!isDesktopRuntime() ? (
          <Card padding="lg">
            <p className="text-sm text-[var(--fg-1)]">
              诊断面板仅在桌面端启用。
            </p>
          </Card>
        ) : null}

        <Card padding="lg" className="overflow-hidden">
          <div className="mb-5 flex items-center justify-between gap-3">
            <div className="min-w-0">
              <h1 className="type-section-title">运行状态</h1>
              <p className="mt-1 text-[13px] text-[var(--fg-2)]">
                用于定位本机数据库、日志和供应商配置文件。
              </p>
            </div>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                void q.refetch();
                void statusQ.refetch();
              }}
              loading={q.isFetching || statusQ.isFetching}
              leftIcon={!q.isFetching && !statusQ.isFetching ? <RefreshCw className="h-3.5 w-3.5" /> : undefined}
            >
              刷新
            </Button>
          </div>

          {q.data ? (
            <div>
              <Row
                icon={<ShieldCheck className="h-4 w-4" />}
                label="引导状态"
                value={q.data.bootstrap_complete ? "已完成" : "未完成"}
              />
              <Row
                icon={<HardDrive className="h-4 w-4" />}
                label="可用空间"
                value={formatBytes(q.data.disk_free_bytes)}
              />
              <Row
                icon={<Database className="h-4 w-4" />}
                label="数据目录"
                value={q.data.data_root}
              />
              <Row
                icon={<Activity className="h-4 w-4" />}
                label="日志目录"
                value={q.data.logs_root}
              />
              <Row
                icon={<Database className="h-4 w-4" />}
                label="设置文件"
                value={q.data.settings_path}
              />
              <Row
                icon={<Database className="h-4 w-4" />}
                label="供应商配置"
                value={q.data.provider_metadata_path}
              />
              <div className="mt-5 flex justify-end">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => exportMut.mutate()}
                  loading={exportMut.isPending}
                  leftIcon={!exportMut.isPending ? <FileArchive className="h-3.5 w-3.5" /> : undefined}
                >
                  生成诊断包
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => void revealDesktopDataDir()}
                  leftIcon={<FolderOpen className="h-3.5 w-3.5" />}
                  className="ml-2"
                >
                  打开数据目录
                </Button>
              </div>
              {exportMut.data ? (
                <div className="mt-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-3 text-[13px] text-[var(--fg-2)]">
                  已生成：<code className="font-mono text-[var(--fg-0)]">{exportMut.data.path}</code>
                  <span className="ml-2">({formatBytes(exportMut.data.bytes)})</span>
                </div>
              ) : null}
              {exportMut.error ? (
                <div
                  role="alert"
                  className="mt-3 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-3 text-[13px] text-danger"
                >
                  {exportMut.error.message}
                </div>
              ) : null}
            </div>
          ) : (
            <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-4 text-sm text-[var(--fg-2)]">
              {q.isLoading ? "正在读取诊断信息…" : q.error?.message ?? "无法读取诊断信息"}
            </div>
          )}
        </Card>

        {desktop ? (
          <Card padding="lg">
            <div className="mb-5">
              <h2 className="type-section-title">内置运行时</h2>
              <p className="mt-1 text-[13px] text-[var(--fg-2)]">
                应用内部组件的就绪状态、端口与最近恢复记录。
              </p>
            </div>
            <RuntimeComponentPanel status={statusQ.data} />
            {statusQ.error ? (
              <div
                role="alert"
                className="mt-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-3 text-[13px] text-[var(--danger)]"
              >
                {statusQ.error.message}
              </div>
            ) : null}
          </Card>
        ) : null}
      </div>
    </SettingsShell>
  );
}
