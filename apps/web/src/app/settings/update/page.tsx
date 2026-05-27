"use client";

import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  CheckCircle2,
  CircleAlert,
  Download,
  RefreshCw,
  Tag,
} from "lucide-react";

import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { Button, Card } from "@/components/ui/primitives";
import {
  checkDesktopUpdate,
  installDesktopUpdate,
  isDesktopRuntime,
  listenDesktopEvent,
  type DesktopUpdateCheck,
  type DesktopUpdateProgress,
} from "@/lib/desktop/runtime";

function StatusCard({
  result,
  installing,
  onInstall,
  progress,
}: {
  result: DesktopUpdateCheck;
  installing: boolean;
  onInstall: () => void;
  progress: DesktopUpdateProgress | null;
}) {
  if (result.available) {
    return (
      <div className="rounded-[var(--radius-card)] border border-success-border bg-success-soft p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-medium text-success">
            <Download className="h-4 w-4" />
            可更新到 {result.version}
          </div>
          <Button
            variant="primary"
            size="sm"
            loading={installing}
            onClick={onInstall}
            leftIcon={!installing ? <Download className="h-3.5 w-3.5" /> : undefined}
          >
            下载并安装
          </Button>
        </div>
        <div className="mt-3 grid gap-2 text-[13px] text-[var(--fg-1)] sm:grid-cols-2">
          <div>
            当前 <span className="font-mono">{result.current_version}</span>
          </div>
          <div>
            目标 <span className="font-mono">{result.target ?? "-"}</span>
          </div>
          {result.date ? <div className="sm:col-span-2">发布时间 {result.date}</div> : null}
        </div>
        {result.body ? (
          <p className="mt-3 whitespace-pre-wrap text-[13px] text-[var(--fg-2)]">
            {result.body}
          </p>
        ) : null}
        {installing && progress ? (
          <div className="mt-4">
            <div className="h-2 overflow-hidden rounded-full bg-[var(--bg-2)]">
              <div
                className="h-full rounded-full bg-success"
                style={{
                  width: `${Math.min(100, Math.max(0, progress.percent ?? 0))}%`,
                }}
              />
            </div>
            <p className="mt-2 text-[12px] text-[var(--fg-2)]">
              {progress.percent != null
                ? `已下载 ${progress.percent.toFixed(1)}%`
                : `已下载 ${Math.round(progress.downloaded / 1024 / 1024)} MB`}
            </p>
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-4">
      <div className="flex items-center gap-2 text-sm font-medium text-[var(--fg-0)]">
        <CheckCircle2 className="h-4 w-4 text-success" />
        已是最新版本
      </div>
      <div className="mt-2 text-[13px] text-[var(--fg-2)]">
        当前 <span className="font-mono text-[var(--fg-0)]">{result.current_version}</span>
      </div>
    </div>
  );
}

export default function DesktopUpdatePage() {
  const desktop = isDesktopRuntime();
  const [progress, setProgress] = useState<DesktopUpdateProgress | null>(null);
  const checkMut = useMutation({
    mutationFn: checkDesktopUpdate,
  });
  const installMut = useMutation({
    mutationFn: installDesktopUpdate,
    onMutate: () => setProgress(null),
  });

  useEffect(() => {
    if (!desktop) return;
    let disposed = false;
    let unlisten: (() => void) | null = null;
    void listenDesktopEvent<DesktopUpdateProgress>("update://progress", (payload) => {
      if (!disposed) setProgress(payload);
    }).then((fn) => {
      if (disposed) fn();
      else unlisten = fn;
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [desktop]);

  return (
    <SettingsShell
      title="检查更新"
      subtitle="桌面端更新通道"
      maxWidth="max-w-3xl"
    >
      <div className="space-y-4">
        {!desktop ? (
          <Card padding="lg">
            <p className="text-sm text-[var(--fg-1)]">检查更新仅在桌面端启用。</p>
          </Card>
        ) : null}

        <Card padding="lg">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              <span className="flex h-9 w-9 items-center justify-center rounded-[var(--radius-control)] bg-[var(--bg-2)] text-[var(--fg-1)]">
                <Tag className="h-4 w-4" />
              </span>
              <div className="min-w-0">
                <h1 className="type-section-title">Lumen Desktop</h1>
                <p className="mt-1 text-[13px] text-[var(--fg-2)]">
                  stable · GitHub Releases
                </p>
              </div>
            </div>
            <Button
              variant="primary"
              size="sm"
              disabled={!desktop}
              loading={checkMut.isPending}
              onClick={() => checkMut.mutate()}
              leftIcon={!checkMut.isPending ? <RefreshCw className="h-3.5 w-3.5" /> : undefined}
            >
              检查更新
            </Button>
          </div>

          <div className="mt-5">
            {checkMut.data ? (
              <StatusCard
                result={checkMut.data}
                installing={installMut.isPending}
                progress={progress}
                onInstall={() => installMut.mutate()}
              />
            ) : null}
            {checkMut.error || installMut.error ? (
              <div
                role="alert"
                className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-4 text-[13px] text-danger"
              >
                <div className="mb-1 flex items-center gap-2 font-medium">
                  <CircleAlert className="h-4 w-4" />
                  {installMut.error ? "安装失败" : "检查失败"}
                </div>
                {(installMut.error ?? checkMut.error)?.message}
              </div>
            ) : null}
          </div>
        </Card>
      </div>
    </SettingsShell>
  );
}
