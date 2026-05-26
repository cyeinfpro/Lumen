"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  CheckCircle2,
  CircleAlert,
  Database,
  FileArchive,
  HardDrive,
  KeyRound,
  Loader2,
  Palette,
  RefreshCw,
  RotateCcw,
} from "lucide-react";

import { Button, Card, Input } from "@/components/ui/primitives";
import {
  apiFetch,
  probeProviders,
  updateProviders,
} from "@/lib/apiClient";
import {
  isDesktopRuntime,
  restartDesktopApp,
  selectDockerImportBackup,
  selectDesktopRestoreBackup,
  type DesktopDockerImportPlan,
  type DesktopRestorePlan,
} from "@/lib/desktop/runtime";
import type { ProvidersProbeOut } from "@/lib/types";
import { cn } from "@/lib/utils";

interface BootstrapStatus {
  complete: boolean;
  data_root: string;
  disk_free_bytes: number | null;
  settings: Record<string, unknown>;
}

const STEPS = [
  { id: "data", label: "数据", icon: HardDrive },
  { id: "mode", label: "起步", icon: Database },
  { id: "provider", label: "供应商", icon: KeyRound },
  { id: "prefs", label: "偏好", icon: Palette },
] as const;

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

export function DesktopBootstrapGate() {
  const desktop = isDesktopRuntime();
  const [step, setStep] = useState(0);
  const [providerName, setProviderName] = useState("OpenAI 官方");
  const [baseUrl, setBaseUrl] = useState("https://api.openai.com/v1");
  const [apiKey, setApiKey] = useState("");
  const [theme, setTheme] = useState("system");
  const [language, setLanguage] = useState("zh-CN");
  const [autoUpdate, setAutoUpdate] = useState(true);
  const [crashReports, setCrashReports] = useState(false);
  const [probeResult, setProbeResult] = useState<ProvidersProbeOut | null>(null);
  const [providerSkipped, setProviderSkipped] = useState(false);
  const [restorePlan, setRestorePlan] = useState<DesktopRestorePlan | null>(null);
  const [dockerUserId, setDockerUserId] = useState("");
  const [dockerPlan, setDockerPlan] = useState<DesktopDockerImportPlan | null>(null);

  const q = useQuery({
    queryKey: ["desktop", "bootstrap-status"],
    queryFn: () => apiFetch<BootstrapStatus>("/settings/bootstrap-status"),
    enabled: desktop,
    retry: false,
    staleTime: 10_000,
  });

  const providerMut = useMutation({
    mutationFn: async () => {
      setProviderSkipped(false);
      await updateProviders({
        items: [
          {
            name: providerName.trim() || "OpenAI 官方",
            base_url: baseUrl.trim(),
            api_key: apiKey.trim(),
            priority: 100,
            weight: 1,
            enabled: Boolean(apiKey.trim()),
            purposes: ["chat", "image", "embedding"],
            image_jobs_enabled: false,
            image_jobs_endpoint: "auto",
            image_jobs_endpoint_lock: false,
            image_jobs_base_url: "",
            image_edit_input_transport: "url",
            image_concurrency: 1,
          },
        ],
        proxies: [],
      });
      const out = await probeProviders([providerName.trim() || "OpenAI 官方"]);
      setProbeResult(out);
      return out;
    },
  });

  const completeMut = useMutation({
    mutationFn: () =>
      apiFetch<BootstrapStatus>("/settings/bootstrap-complete", {
        method: "POST",
        body: JSON.stringify({
          settings: {
            theme,
            language,
            auto_check_updates: autoUpdate,
            crash_reports_enabled: crashReports,
          },
        }),
      }),
    onSuccess: () => {
      void q.refetch();
    },
  });
  const restoreMut = useMutation({
    mutationFn: selectDesktopRestoreBackup,
    onSuccess: (plan) => {
      if (plan) setRestorePlan(plan);
    },
  });
  const dockerImportMut = useMutation({
    mutationFn: () => selectDockerImportBackup(dockerUserId),
    onSuccess: (plan) => {
      if (plan) setDockerPlan(plan);
    },
  });
  const restartMut = useMutation({
    mutationFn: restartDesktopApp,
  });

  const canFinish = useMemo(() => {
    return providerSkipped || Boolean(apiKey.trim() && probeResult?.items.some((item) => item.ok));
  }, [apiKey, probeResult, providerSkipped]);

  const canLeaveProviderStep = step !== 2 || canFinish;

  if (!desktop || q.data?.complete) return null;

  const active = STEPS[step];

  return (
    <div className="mobile-dialog-shell fixed inset-0 z-[100] flex items-center justify-center bg-[var(--bg-0)]/92 px-4 py-6 backdrop-blur-xl">
      <Card
        variant="default"
        elevation={3}
        padding="none"
        className="mobile-dialog-panel grid max-h-[min(720px,92dvh)] w-full max-w-[980px] overflow-hidden md:grid-cols-[240px_minmax(0,1fr)]"
      >
        <aside className="border-b border-[var(--border-subtle)] bg-[var(--bg-1)] p-5 md:border-b-0 md:border-r">
          <div className="mb-6">
            <div className="type-section-title">Lumen Desktop</div>
            <p className="mt-1 text-[12px] text-[var(--fg-2)]">
              本机运行时初始化
            </p>
          </div>
          <ol className="space-y-2">
            {STEPS.map((item, index) => {
              const Icon = item.icon;
              const current = item.id === active.id;
              const done = index < step;
              return (
                <li key={item.id}>
                  <button
                    type="button"
                    onClick={() => setStep(index)}
                    className={cn(
                      "flex h-10 w-full items-center gap-2 rounded-[var(--radius-control)] px-3 text-left text-sm transition-colors",
                      current
                        ? "bg-[var(--bg-3)] text-[var(--fg-0)]"
                        : "text-[var(--fg-2)] hover:bg-[var(--bg-2)]",
                    )}
                  >
                    {done ? (
                      <CheckCircle2 className="h-4 w-4 text-[var(--success)]" />
                    ) : (
                      <Icon className="h-4 w-4" />
                    )}
                    {item.label}
                  </button>
                </li>
              );
            })}
          </ol>
        </aside>

        <main className="mobile-dialog-scroll min-h-0 overflow-y-auto p-5 md:p-7">
          {q.isLoading ? (
            <div className="flex min-h-[360px] items-center justify-center">
              <Loader2 className="h-5 w-5 animate-spin text-[var(--fg-2)]" />
            </div>
          ) : step === 0 ? (
            <section className="space-y-5">
              <div>
                <h1 className="type-page-title-sm">数据目录</h1>
                <p className="mt-2 text-sm text-[var(--fg-2)]">
                  所有数据库、缓存、图片和日志都会写入本机目录。
                </p>
              </div>
              <div className="grid gap-3 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-4">
                <div className="text-[12px] text-[var(--fg-2)]">当前目录</div>
                <code className="break-all rounded-[var(--radius-control)] bg-[var(--bg-0)] px-3 py-2 font-mono text-[12px] text-[var(--fg-0)]">
                  {q.data?.data_root ?? "未解析"}
                </code>
                <div className="text-[12px] text-[var(--fg-2)]">
                  可用空间：{formatBytes(q.data?.disk_free_bytes ?? null)}
                </div>
              </div>
            </section>
          ) : step === 1 ? (
            <section className="space-y-5">
              <div>
                <h1 className="type-page-title-sm">起步方式</h1>
                <p className="mt-2 text-sm text-[var(--fg-2)]">
                  选择本机数据的初始化方式。
                </p>
              </div>
              <div className="grid gap-3">
                <div className="rounded-[var(--radius-card)] border border-[var(--accent)]/35 bg-[var(--bg-1)] p-4">
                  <div className="text-sm font-medium text-[var(--fg-0)]">全新使用</div>
                  <div className="mt-1 text-[13px] text-[var(--fg-2)]">
                    创建本机 local-user、SQLite 数据库和独立存储目录。
                  </div>
                </div>
                <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-4">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-sm font-medium text-[var(--fg-0)]">
                        从桌面端备份恢复
                      </div>
                      <div className="mt-1 text-[13px] text-[var(--fg-2)]">
                        选择 `.lumen-backup.zip`，重启后在 sidecar 启动前恢复。
                      </div>
                    </div>
                    <Button
                      variant="secondary"
                      size="sm"
                      loading={restoreMut.isPending}
                      onClick={() => restoreMut.mutate()}
                      leftIcon={!restoreMut.isPending ? <RotateCcw className="h-3.5 w-3.5" /> : undefined}
                    >
                      选择备份
                    </Button>
                  </div>
                  {restorePlan ? (
                    <div className="mt-3 rounded-[var(--radius-card)] border border-[var(--accent)]/35 bg-[var(--bg-0)] p-3">
                      <div className="break-all font-mono text-[12px] text-[var(--fg-0)]">
                        {restorePlan.source_path}
                      </div>
                      <div className="mt-3 flex justify-end">
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
                  {restoreMut.error ? (
                    <div
                      role="alert"
                      className="mt-3 flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-3 text-[13px] text-danger"
                    >
                      <CircleAlert className="mt-0.5 h-4 w-4" />
                      <span>{restoreMut.error.message}</span>
                    </div>
                  ) : null}
                </div>
                <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 text-sm font-medium text-[var(--fg-0)]">
                        <FileArchive className="h-4 w-4 text-[var(--fg-2)]" />
                        从 Docker 备份导入
                      </div>
                      <div className="mt-1 text-[13px] text-[var(--fg-2)]">
                        选择 `lumen-export.dump` 或 `lumen-export.copy.sql`；随后可选 `lumen-storage.tar.gz`，导入会在重启后执行。
                      </div>
                    </div>
                    <Button
                      variant="secondary"
                      size="sm"
                      loading={dockerImportMut.isPending}
                      onClick={() => dockerImportMut.mutate()}
                      leftIcon={!dockerImportMut.isPending ? <FileArchive className="h-3.5 w-3.5" /> : undefined}
                    >
                      选择导出
                    </Button>
                  </div>
                  <div className="mt-3 max-w-sm">
                    <Input
                      label="Docker 用户 ID（多用户导出时填写）"
                      value={dockerUserId}
                      onChange={(e) => setDockerUserId(e.target.value)}
                      placeholder="留空表示单用户导出"
                    />
                  </div>
                  {dockerPlan ? (
                    <div className="mt-3 rounded-[var(--radius-card)] border border-[var(--accent)]/35 bg-[var(--bg-0)] p-3">
                      <div className="break-all font-mono text-[12px] text-[var(--fg-0)]">
                        {dockerPlan.source_dump_path}
                      </div>
                      {dockerPlan.source_storage_tar_path ? (
                        <div className="mt-1 break-all font-mono text-[12px] text-[var(--fg-2)]">
                          {dockerPlan.source_storage_tar_path}
                        </div>
                      ) : null}
                      <div className="mt-2 text-[12px] text-[var(--fg-2)]">
                        已排队。重启后会先做安全备份，再替换 SQLite 与 storage。
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
                  {dockerImportMut.error ? (
                    <div
                      role="alert"
                      className="mt-3 flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-3 text-[13px] text-danger"
                    >
                      <CircleAlert className="mt-0.5 h-4 w-4" />
                      <span>{dockerImportMut.error.message}</span>
                    </div>
                  ) : null}
                </div>
              </div>
            </section>
          ) : step === 2 ? (
            <section className="space-y-5">
              <div>
                <h1 className="type-page-title-sm">第一个供应商</h1>
                <p className="mt-2 text-sm text-[var(--fg-2)]">
                  支持 OpenAI 官方、中转站、new-api、Ollama 或 LM Studio 的 OpenAI 兼容地址。
                </p>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <Input label="名称" value={providerName} onChange={(e) => setProviderName(e.target.value)} />
                <Input label="Base URL" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
                <Input
                  label="API Key"
                  type="password"
                  value={apiKey}
                  onChange={(e) => {
                    setApiKey(e.target.value);
                    setProbeResult(null);
                    setProviderSkipped(false);
                  }}
                  wrapperClassName="sm:col-span-2"
                />
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="secondary"
                  onClick={() => providerMut.mutate()}
                  loading={providerMut.isPending}
                  disabled={!baseUrl.trim() || !providerName.trim() || !apiKey.trim()}
                >
                  测试连接
                </Button>
                {probeResult ? (
                  <span className="text-[13px] text-[var(--fg-2)]">
                    {probeResult.items.some((item) => item.ok)
                      ? "连接成功"
                      : probeResult.items[0]?.error ?? "连接失败"}
                  </span>
                ) : null}
                <Button
                  variant="ghost"
                  onClick={() => {
                    setProviderSkipped(true);
                    setProbeResult(null);
                  }}
                >
                  稍后配置
                </Button>
              </div>
              {providerSkipped ? (
                <div
                  role="status"
                  className="flex items-start gap-2 rounded-[var(--radius-card)] border border-[var(--accent)]/35 bg-[var(--bg-1)] p-3 text-[13px] text-[var(--fg-2)]"
                >
                  <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-[var(--accent)]" />
                  <span>
                    已选择稍后配置。进入主界面后仍可在设置里的供应商池补充 Key；补齐之前对话和生图会提示没有可用供应商。
                  </span>
                </div>
              ) : null}
              {providerMut.error ? (
                <div
                  role="alert"
                  className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-3 text-[13px] text-danger"
                >
                  <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{providerMut.error.message}</span>
                </div>
              ) : null}
            </section>
          ) : (
            <section className="space-y-5">
              <div>
                <h1 className="type-page-title-sm">偏好</h1>
                <p className="mt-2 text-sm text-[var(--fg-2)]">
                  这些设置保存在本机 settings.json。
                </p>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="grid gap-1 text-xs text-[var(--fg-1)]">
                  主题
                  <select
                    value={theme}
                    onChange={(e) => setTheme(e.target.value)}
                    className="h-9 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)]"
                  >
                    <option value="system">跟随系统</option>
                    <option value="light">浅色</option>
                    <option value="dark">深色</option>
                  </select>
                </label>
                <label className="grid gap-1 text-xs text-[var(--fg-1)]">
                  语言
                  <select
                    value={language}
                    onChange={(e) => setLanguage(e.target.value)}
                    className="h-9 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm text-[var(--fg-0)]"
                  >
                    <option value="zh-CN">中文</option>
                    <option value="en">English</option>
                  </select>
                </label>
              </div>
              <label className="flex items-center gap-2 text-sm text-[var(--fg-1)]">
                <input
                  type="checkbox"
                  checked={autoUpdate}
                  onChange={(e) => setAutoUpdate(e.target.checked)}
                />
                自动检查更新
              </label>
              <label className="flex items-center gap-2 text-sm text-[var(--fg-1)]">
                <input
                  type="checkbox"
                  checked={crashReports}
                  onChange={(e) => setCrashReports(e.target.checked)}
                />
                允许发送脱敏崩溃报告
              </label>
            </section>
          )}

          <div className="mobile-dialog-footer mt-8 flex items-center justify-between border-t border-[var(--border-subtle)] pt-4">
            <Button
              variant="ghost"
              disabled={step === 0 || completeMut.isPending}
              onClick={() => setStep((v) => Math.max(0, v - 1))}
            >
              上一步
            </Button>
            {step < STEPS.length - 1 ? (
              <Button
                variant="primary"
                disabled={!canLeaveProviderStep}
                onClick={() => setStep((v) => Math.min(STEPS.length - 1, v + 1))}
              >
                下一步
              </Button>
            ) : (
              <Button
                variant="primary"
                loading={completeMut.isPending}
                disabled={!canFinish}
                onClick={() => completeMut.mutate()}
              >
                完成
              </Button>
            )}
          </div>
        </main>
      </Card>
    </div>
  );
}
