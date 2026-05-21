"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  History,
  Loader2,
  Rocket,
  RotateCcw,
  Terminal,
  Undo2,
  X,
} from "lucide-react";

import {
  qk,
  useAdminProxiesQuery,
  useAdminCheckUpdateQuery,
  useAdminReleasesQuery,
  useAdminUpdateStatusQuery,
  useAdminUpdateVersionQuery,
  useRollbackPreviousMutation,
  useRollbackReleaseMutation,
  useSystemSettingsQuery,
  useTriggerAdminUpdateMutation,
  useUpdateSystemSettingsMutation,
} from "@/lib/queries";
import {
  adminUpdateStreamUrl,
  ApiError,
  checkAdminUpdate,
  type AdminUpdateStatusOut,
  type AdminUpdateVersionOut,
  type ReleaseInfo,
  type UpdateStepRecord,
} from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { Button, ConfirmDialog, IconButton } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { UpdateAvailableCard } from "@/components/admin/UpdateAvailableCard";

const UPDATE_USE_PROXY_POOL_KEY = "update.use_proxy_pool";
const UPDATE_PROXY_NAME_KEY = "update.proxy_name";

const PHASE_ORDER: readonly string[] = [
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

type AdminStreamStatus = "idle" | "connecting" | "open" | "error" | "broken";

interface AdminUpdateStreamHandle {
  logBuffer: string[];
  streamStatus: AdminStreamStatus;
  clearLogs: () => void;
}

interface LumenUpdateBlockProps {
  status: AdminUpdateStatusOut | undefined;
  loading: boolean;
  error: Error | null;
  triggering: boolean;
  banner: { kind: "success" | "error" | "info"; text: string } | null;
  releases: ReleaseInfo[] | undefined;
  releasesLoading: boolean;
  releasesError: Error | null;
  rollbackPendingId: string | null;
  logBuffer: string[];
  streamStatus: AdminStreamStatus;
  onTrigger: () => void;
  onRefresh: () => void;
  onRollbackPrevious: () => void;
  onRollback: (releaseId: string) => void;
  onClearBanner: () => void;
}

const LOG_BUFFER_MAX = 500;
const SSE_RETRY_DELAYS_MS = [1000, 2000, 5000, 15000, 15000];
const SSE_MAX_RETRIES = SSE_RETRY_DELAYS_MS.length;

export function AdminUpdatePanel() {
  const queryClient = useQueryClient();
  const settingsQ = useSystemSettingsQuery({ retry: false });
  const proxiesQ = useAdminProxiesQuery({ retry: false });
  const updateSettingsMut = useUpdateSystemSettingsMutation();
  const streamConnectedRef = useRef(false);
  const updateStatusQ = useAdminUpdateStatusQuery({
    retry: false,
    refetchInterval: (query) => {
      if (streamConnectedRef.current) return false;
      return query.state.data?.running ? 5000 : false;
    },
  });
  const updateVersionQ = useAdminUpdateVersionQuery({ retry: false });
  const updateCheckQ = useAdminCheckUpdateQuery(false, { retry: false });
  const releasesQ = useAdminReleasesQuery({ retry: false });

  const [updateBanner, setUpdateBanner] = useState<{
    kind: "success" | "error" | "info";
    text: string;
  } | null>(null);
  const [updateStreamArmed, setUpdateStreamArmed] = useState(false);
  const [manualCheckPending, setManualCheckPending] = useState(false);

  const triggerUpdateMut = useTriggerAdminUpdateMutation({
    onSuccess: (result) => {
      setUpdateStreamArmed(true);
      queryClient.setQueryData<AdminUpdateStatusOut | undefined>(
        qk.adminUpdateStatus(),
        (prev) => ({
          running: true,
          pid: result.pid ?? prev?.pid ?? null,
          unit: result.unit ?? prev?.unit ?? null,
          started_at: result.started_at,
          log_tail: prev?.log_tail ?? "",
          phases: prev?.phases ?? [],
          current_release: prev?.current_release ?? null,
          previous_release: prev?.previous_release ?? null,
          releases: prev?.releases ?? [],
        }),
      );
      const target = result.unit ? `任务 ${result.unit}` : `进程 ${result.pid ?? "-"}`;
      setUpdateBanner({
        kind: "success",
        text: `更新已启动，${target}${result.proxy_name ? `，代理 ${result.proxy_name}` : ""}${result.target_tag ? `，目标 ${result.target_tag}` : ""}`,
      });
    },
    onError: (err) => {
      setUpdateStreamArmed(false);
      const msg = err instanceof ApiError ? err.message : err.message || "触发更新失败";
      setUpdateBanner({ kind: "error", text: `触发更新失败：${msg}` });
    },
  });

  const previousRollbackMut = useRollbackPreviousMutation({
    onSuccess: (result) => {
      setUpdateStreamArmed(true);
      queryClient.setQueryData<AdminUpdateStatusOut | undefined>(
        qk.adminUpdateStatus(),
        (prev) => ({
          running: true,
          pid: prev?.pid ?? null,
          unit: prev?.unit ?? null,
          started_at: result.started_at,
          log_tail: prev?.log_tail ?? "",
          phases: prev?.phases ?? [],
          current_release: prev?.current_release ?? null,
          previous_release: prev?.previous_release ?? null,
          releases: prev?.releases ?? [],
        }),
      );
      setUpdateBanner({
        kind: "info",
        text: `已启动回滚到上一版本 ${result.target.id}`,
      });
    },
  });

  const rollbackMut = useRollbackReleaseMutation({
    onSuccess: (result) => {
      setUpdateStreamArmed(true);
      queryClient.setQueryData<AdminUpdateStatusOut | undefined>(
        qk.adminUpdateStatus(),
        (prev) => ({
          running: true,
          pid: prev?.pid ?? null,
          unit: prev?.unit ?? null,
          started_at: result.started_at,
          log_tail: prev?.log_tail ?? "",
          phases: prev?.phases ?? [],
          current_release: prev?.current_release ?? null,
          previous_release: prev?.previous_release ?? null,
          releases: prev?.releases ?? [],
        }),
      );
      setUpdateBanner({
        kind: "success",
        text: `回滚已启动，目标 release ${result.target.id}`,
      });
    },
    onError: (err) => {
      setUpdateStreamArmed(false);
      const msg = err instanceof ApiError ? err.message : err.message || "触发回滚失败";
      setUpdateBanner({ kind: "error", text: `触发回滚失败：${msg}` });
    },
  });

  const runUpdateCheck = useCallback(
    async (force = false) => {
      setManualCheckPending(true);
      try {
        const data = await checkAdminUpdate(force);
        queryClient.setQueryData(qk.adminUpdateCheck(false), data);
        queryClient.setQueryData<AdminUpdateVersionOut | undefined>(
          qk.adminUpdateVersion(),
          {
            version: data.current_version,
            image_tag: updateVersionQ.data?.image_tag ?? `v${data.current_version}`,
            release_id: updateVersionQ.data?.release_id ?? null,
            sha: updateVersionQ.data?.sha ?? null,
            channel: data.channel,
            build_type: data.build_type,
            degraded: updateVersionQ.data?.degraded ?? [],
          },
        );
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : err instanceof Error ? err.message : "检查更新失败";
        setUpdateBanner({ kind: "error", text: `检查更新失败：${msg}` });
      } finally {
        setManualCheckPending(false);
      }
    },
    [queryClient, updateVersionQ.data],
  );

  const updateRunning = Boolean(updateStatusQ.data?.running);
  const sseEnabled =
    updateRunning ||
    updateStreamArmed ||
    triggerUpdateMut.isPending ||
    rollbackMut.isPending ||
    previousRollbackMut.isPending;
  const { logBuffer, streamStatus, clearLogs } = useAdminUpdateStream(sseEnabled);

  useEffect(() => {
    streamConnectedRef.current = streamStatus === "open";
  }, [streamStatus]);

  useEffect(() => {
    if (!updateStreamArmed) return;
    if (
      triggerUpdateMut.isPending ||
      rollbackMut.isPending ||
      previousRollbackMut.isPending ||
      updateRunning
    )
      return;
    const t = setTimeout(() => setUpdateStreamArmed(false), 0);
    return () => clearTimeout(t);
  }, [
    rollbackMut.isPending,
    previousRollbackMut.isPending,
    triggerUpdateMut.isPending,
    updateRunning,
    updateStreamArmed,
  ]);

  const triggering =
    triggerUpdateMut.isPending ||
    rollbackMut.isPending ||
    previousRollbackMut.isPending;
  const triggerUpdate = () => {
    setUpdateBanner(null);
    clearLogs();
    triggerUpdateMut.mutate({
      target_tag: updateCheckQ.data?.resolved_image_tag ?? undefined,
      channel: updateCheckQ.data?.channel ?? undefined,
      force_redeploy: false,
    });
  };
  const rollbackPrevious = () => {
    setUpdateBanner(null);
    clearLogs();
    previousRollbackMut.mutate();
  };

  return (
    <section className="space-y-3">
      <UpdateNetworkSettingsCard
        settings={settingsQ.data?.items ?? []}
        proxies={proxiesQ.data?.items ?? []}
        loading={settingsQ.isLoading || proxiesQ.isLoading}
        saving={updateSettingsMut.isPending}
        error={settingsQ.error ?? proxiesQ.error ?? null}
        onRetry={() => {
          void settingsQ.refetch();
          void proxiesQ.refetch();
        }}
        onSave={(items) => updateSettingsMut.mutate(items)}
      />

      <UpdateAvailableCard
        check={updateCheckQ.data}
        status={updateStatusQ.data}
        version={updateVersionQ.data}
        checking={updateCheckQ.isLoading || manualCheckPending}
        triggering={triggering}
        onCheck={(force) => {
          void runUpdateCheck(force);
        }}
        onTrigger={triggerUpdate}
        onRollbackPrevious={rollbackPrevious}
      />

      <LumenUpdateBlock
        status={updateStatusQ.data}
        loading={updateStatusQ.isLoading}
        error={updateStatusQ.error}
        triggering={triggering}
        banner={updateBanner}
        releases={releasesQ.data}
        releasesLoading={releasesQ.isLoading}
        releasesError={releasesQ.error}
        rollbackPendingId={
          previousRollbackMut.isPending
            ? "__previous__"
            : rollbackMut.isPending
              ? rollbackMut.variables ?? null
              : null
        }
        logBuffer={logBuffer}
        streamStatus={streamStatus}
        onTrigger={triggerUpdate}
        onRefresh={() => {
          void updateStatusQ.refetch();
          void releasesQ.refetch();
        }}
        onRollbackPrevious={rollbackPrevious}
        onRollback={(releaseId) => {
          setUpdateBanner(null);
          clearLogs();
          rollbackMut.mutate(releaseId);
        }}
        onClearBanner={() => setUpdateBanner(null)}
      />
    </section>
  );
}

function UpdateNetworkSettingsCard({
  settings,
  proxies,
  loading,
  saving,
  error,
  onRetry,
  onSave,
}: {
  settings: Array<{ key: string; value: string | null; has_value?: boolean }>;
  proxies: Array<{
    name: string;
    enabled: boolean;
    in_cooldown: boolean;
    last_latency_ms: number | null;
  }>;
  loading: boolean;
  saving: boolean;
  error: Error | null;
  onRetry: () => void;
  onSave: (items: { key: string; value: string }[]) => void;
}) {
  const settingMap = useMemo(
    () => new Map(settings.map((item) => [item.key, item])),
    [settings],
  );
  const useProxyPool =
    (settingMap.get(UPDATE_USE_PROXY_POOL_KEY)?.value ?? "0") === "1";
  const proxyName = settingMap.get(UPDATE_PROXY_NAME_KEY)?.value ?? "";
  const enabledProxies = proxies.filter((proxy) => proxy.enabled);

  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)] backdrop-blur-sm">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="type-card-title">更新网络设置</p>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            控制一键更新拉取代码、依赖和镜像时是否走代理池。
          </p>
        </div>
        <div>
          {error && (
            <div role="alert">
              <Button
                variant="secondary"
                size="sm"
                onClick={onRetry}
                leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
              >
                {copy.action.retry}
              </Button>
            </div>
          )}
          {!error && loading && (
            <span
              role="status"
              className="inline-flex items-center gap-1.5 type-caption text-[var(--fg-2)]"
            >
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              读取中
            </span>
          )}
        </div>
      </div>

      {error ? (
        <p role="alert" className="mt-3 type-caption text-danger">
          读取更新网络设置失败：{error.message}
        </p>
      ) : (
        <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
          <div className="flex flex-wrap items-center gap-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-3">
            <button
              type="button"
              role="switch"
              aria-checked={useProxyPool}
              aria-label={`更新时使用代理池 ${useProxyPool ? "关闭" : "开启"}`}
              disabled={saving || loading}
              onClick={() =>
                onSave([
                  {
                    key: UPDATE_USE_PROXY_POOL_KEY,
                    value: useProxyPool ? "0" : "1",
                  },
                ])
              }
              className={cn(
                "relative inline-flex h-7 w-12 shrink-0 cursor-pointer items-center rounded-full border transition-colors focus:outline-none focus:ring-2 focus:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50",
                useProxyPool
                  ? "border-accent-border bg-accent"
                  : "border-[var(--border)] bg-[var(--bg-2)]",
              )}
            >
              <span
                className={cn(
                  "h-5 w-5 rounded-full bg-[var(--bg-0)] shadow-[var(--shadow-1)] transition-transform",
                  useProxyPool ? "translate-x-5" : "translate-x-1",
                )}
              />
            </button>
            <div className="min-w-0">
              <p className="type-body-sm font-medium text-[var(--fg-0)]">
                更新时使用代理池
              </p>
              <p className="type-caption text-[var(--fg-2)]">
                国内服务器拉取 GitHub、uv 或 npm 资源失败时开启。
              </p>
            </div>
          </div>

          <label className="block rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-3">
            <span className="type-caption text-[var(--fg-2)]">更新代理</span>
            <select
              value={proxyName}
              disabled={saving || loading}
              onChange={(event) =>
                onSave([
                  {
                    key: UPDATE_PROXY_NAME_KEY,
                    value: event.target.value,
                  },
                ])
              }
              className="mt-2 h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 type-body-sm text-[var(--fg-0)] outline-none transition-colors focus:border-accent-border focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <option value="">自动选择第一个启用代理</option>
              {enabledProxies.map((proxy) => (
                <option key={proxy.name} value={proxy.name}>
                  {proxy.name}
                  {proxy.in_cooldown ? " · 冷却中" : ""}
                  {proxy.last_latency_ms != null ? ` · ${Math.round(proxy.last_latency_ms)}ms` : ""}
                </option>
              ))}
              {proxyName &&
                !enabledProxies.some((proxy) => proxy.name === proxyName) && (
                  <option value={proxyName}>{proxyName} · 当前配置</option>
                )}
            </select>
            <p className="mt-2 type-caption text-[var(--fg-2)]">
              留空会交给后端选择第一个启用代理；这里保存后立即影响下一次更新。
            </p>
          </label>
        </div>
      )}
    </div>
  );
}

function useAdminUpdateStream(enabled: boolean): AdminUpdateStreamHandle {
  const qc = useQueryClient();
  const [logBuffer, setLogBuffer] = useState<string[]>([]);
  const [streamStatus, setStreamStatus] = useState<AdminStreamStatus>("idle");

  const qcRef = useRef(qc);
  useEffect(() => {
    qcRef.current = qc;
  });

  const clearLogs = useCallback(() => {
    setLogBuffer([]);
  }, []);

  useEffect(() => {
    if (!enabled) {
      const t = setTimeout(() => setStreamStatus("idle"), 0);
      return () => clearTimeout(t);
    }
    if (typeof window === "undefined" || typeof EventSource === "undefined") {
      const t = setTimeout(() => setStreamStatus("idle"), 0);
      return () => clearTimeout(t);
    }

    let es: EventSource | null = null;
    let retryAttempt = 0;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;

    const clearRetry = () => {
      if (retryTimer) {
        clearTimeout(retryTimer);
        retryTimer = null;
      }
    };

    const close = () => {
      clearRetry();
      if (es) {
        try {
          es.close();
        } catch {
          /* ignore */
        }
        es = null;
      }
    };

    const mergeStep = (step: UpdateStepRecord) => {
      qcRef.current.setQueryData<AdminUpdateStatusOut | undefined>(
        qk.adminUpdateStatus(),
        (prev) => {
          if (!prev) {
            return {
              running: step.status === "running",
              log_tail: "",
              phases: [step],
            };
          }
          const phases = prev.phases ? [...prev.phases] : [];
          const idx = phases.findIndex((p) => p.phase === step.phase);
          if (idx >= 0) {
            phases[idx] = { ...phases[idx], ...step };
          } else {
            phases.push(step);
          }
          return { ...prev, phases };
        },
      );
    };

    const mergeInfo = (payload: { phase: string; key: string; value: string }) => {
      qcRef.current.setQueryData<AdminUpdateStatusOut | undefined>(
        qk.adminUpdateStatus(),
        (prev) => {
          if (!prev) return prev;
          const phases = prev.phases ? [...prev.phases] : [];
          const idx = phases.findIndex((p) => p.phase === payload.phase);
          if (idx < 0) return prev;
          const cur = phases[idx];
          phases[idx] = {
            ...cur,
            info: { ...(cur.info ?? {}), [payload.key]: payload.value },
          };
          return { ...prev, phases };
        },
      );
    };

    const scheduleRetry = () => {
      if (disposed) return;
      clearRetry();
      if (retryAttempt >= SSE_MAX_RETRIES) {
        setStreamStatus("broken");
        return;
      }
      const delay = SSE_RETRY_DELAYS_MS[retryAttempt] ?? 15000;
      retryAttempt += 1;
      retryTimer = setTimeout(() => {
        if (!disposed) open();
      }, delay);
    };

    const open = () => {
      if (disposed) return;
      close();
      setStreamStatus("connecting");
      try {
        es = new EventSource(adminUpdateStreamUrl(), { withCredentials: true });
      } catch {
        setStreamStatus("error");
        scheduleRetry();
        return;
      }

      es.onopen = () => {
        retryAttempt = 0;
        setStreamStatus("open");
      };

      const parseData = <T,>(raw: string): T | null => {
        try {
          return JSON.parse(raw) as T;
        } catch {
          return null;
        }
      };

      es.addEventListener("state", (ev: MessageEvent) => {
        const snapshot = parseData<AdminUpdateStatusOut>(ev.data);
        if (!snapshot) return;
        qcRef.current.setQueryData(qk.adminUpdateStatus(), snapshot);
        if (snapshot.releases) {
          qcRef.current.setQueryData(qk.adminReleases(), snapshot.releases);
        }
      });

      es.addEventListener("step", (ev: MessageEvent) => {
        const step = parseData<UpdateStepRecord>(ev.data);
        if (!step || !step.phase) return;
        mergeStep(step);
      });

      es.addEventListener("info", (ev: MessageEvent) => {
        const info = parseData<{ phase: string; key: string; value: string }>(
          ev.data,
        );
        if (!info || !info.phase || !info.key) return;
        mergeInfo(info);
      });

      es.addEventListener("log", (ev: MessageEvent) => {
        const payload = parseData<{ line?: string; lines?: string[] }>(ev.data);
        if (!payload) return;
        const lines = Array.isArray(payload.lines)
          ? payload.lines.filter((line): line is string => typeof line === "string")
          : typeof payload.line === "string"
            ? [payload.line]
            : [];
        if (lines.length === 0) return;
        setLogBuffer((prev) => {
          const next =
            prev.length >= LOG_BUFFER_MAX
              ? prev.slice(-(LOG_BUFFER_MAX - 1))
              : prev.slice();
          return [...next, ...lines].slice(-LOG_BUFFER_MAX);
        });
      });

      es.addEventListener("done", (ev: MessageEvent) => {
        const payload = parseData<{ final_status?: AdminUpdateStatusOut }>(
          ev.data,
        );
        if (payload?.final_status) {
          qcRef.current.setQueryData(
            qk.adminUpdateStatus(),
            payload.final_status,
          );
        }
        qcRef.current.invalidateQueries({ queryKey: qk.adminUpdateStatus() });
        qcRef.current.invalidateQueries({ queryKey: qk.adminReleases() });
        close();
        setStreamStatus("idle");
      });

      es.onerror = () => {
        setStreamStatus("error");
        close();
        scheduleRetry();
      };
    };

    open();

    return () => {
      disposed = true;
      close();
    };
  }, [enabled]);

  return { logBuffer, streamStatus, clearLogs };
}

function LumenUpdateBlock({
  status,
  loading,
  error,
  triggering,
  banner,
  releases,
  releasesLoading,
  releasesError,
  rollbackPendingId,
  logBuffer,
  streamStatus,
  onTrigger,
  onRefresh,
  onRollbackPrevious,
  onRollback,
  onClearBanner,
}: LumenUpdateBlockProps) {
  const running = Boolean(status?.running);
  const isRollingBack = rollbackPendingId != null;
  const disabled = triggering || running || isRollingBack;
  const runningTarget = status?.unit
    ? `unit ${status.unit}`
    : `pid ${status?.pid ?? "-"}`;
  const phases = useMemo(() => status?.phases ?? [], [status?.phases]);
  const phaseByName = useMemo(() => {
    const m = new Map<string, UpdateStepRecord>();
    for (const p of phases) m.set(p.phase, p);
    return m;
  }, [phases]);

  const checklist = useMemo<string[]>(() => {
    const order = [...PHASE_ORDER];
    const seen = new Set(order);
    for (const p of phases) {
      if (!seen.has(p.phase)) {
        order.push(p.phase);
        seen.add(p.phase);
      }
    }
    return order;
  }, [phases]);

  const failed = useMemo(
    () => phases.some((p) => p.status === "done" && p.rc != null && p.rc !== 0),
    [phases],
  );

  const activePhase = useMemo(() => {
    const runningIdx = phases.findIndex((p) => p.status === "running");
    if (runningIdx >= 0) return phases[runningIdx];
    if (phases.length === 0) return null;
    return phases[phases.length - 1];
  }, [phases]);

  const completedCount = useMemo(
    () => phases.filter((p) => p.status === "done" && (p.rc ?? 0) === 0).length,
    [phases],
  );
  const totalCount = checklist.length;
  const progressPct = totalCount > 0
    ? Math.round((completedCount / totalCount) * 100)
    : 0;

  const [userLogOpen, setUserLogOpen] = useState<boolean | null>(null);
  const logOpen = userLogOpen ?? running;
  const onLogToggle = useCallback(() => {
    setUserLogOpen((prev) => !(prev ?? running));
  }, [running]);
  const [userDetailsOpen, setUserDetailsOpen] = useState<boolean | null>(null);
  const detailsOpen = userDetailsOpen ?? (running || failed);
  const onDetailsToggle = useCallback(() => {
    setUserDetailsOpen((prev) => !(prev ?? (running || failed)));
  }, [failed, running]);

  const RELOAD_DELAY_SEC = 6;
  const [reloadCountdown, setReloadCountdown] = useState<number | null>(null);
  const reloadNow = useCallback(() => {
    if (typeof window !== "undefined") window.location.reload();
  }, []);
  const cancelReload = useCallback(() => setReloadCountdown(null), []);

  const prevRunningRef = useRef(running);
  useEffect(() => {
    const wasRunning = prevRunningRef.current;
    prevRunningRef.current = running;
    if (!wasRunning || running || failed) return;
    const t = setTimeout(() => setReloadCountdown(RELOAD_DELAY_SEC), 0);
    return () => clearTimeout(t);
  }, [running, failed]);

  useEffect(() => {
    if (reloadCountdown == null) return;
    if (reloadCountdown <= 0) {
      reloadNow();
      return;
    }
    const t = setTimeout(
      () => setReloadCountdown((c) => (c == null ? null : c - 1)),
      1000,
    );
    return () => clearTimeout(t);
  }, [reloadCountdown, reloadNow]);

  const logRef = useRef<HTMLPreElement | null>(null);
  const userScrolledRef = useRef(false);
  useEffect(() => {
    if (!logOpen) return;
    const el = logRef.current;
    if (!el) return;
    if (!userScrolledRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [logOpen, logBuffer]);
  const onLogScroll: React.UIEventHandler<HTMLPreElement> = (e) => {
    const el = e.currentTarget;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    userScrolledRef.current = distanceFromBottom > 16;
  };

  const [pendingRollback, setPendingRollback] = useState<ReleaseInfo | null>(null);

  useEffect(() => {
    if (!banner) return;
    if (banner.kind === "error") return;
    const t = setTimeout(() => onClearBanner(), 6000);
    return () => clearTimeout(t);
  }, [banner, onClearBanner]);

  const effectiveBanner =
    banner ??
    (failed && !running
      ? {
          kind: "error" as const,
          text: "上次更新失败，请查看 checklist 中的红色 phase 或日志。",
        }
      : null);

  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-3 shadow-[var(--shadow-1)] backdrop-blur-sm">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="flex min-w-0 gap-3">
          <div
            className={cn(
              "flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-card)] border",
              running
                ? "border-info-border bg-info-soft"
                : failed
                  ? "border-danger-border bg-danger-soft"
                  : "border-[var(--border)] bg-[var(--bg-2)]",
            )}
          >
            {running ? (
              <Loader2 className="h-4 w-4 animate-spin text-info" />
            ) : failed ? (
              <X className="h-4 w-4 text-danger" />
            ) : (
              <Terminal className="h-4 w-4 text-[var(--fg-2)]" />
            )}
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="type-card-title text-sm">更新控制台</h3>
              <span
                className={cn(
                  "rounded-[var(--radius-control)] border px-2 py-0.5 text-[11px]",
                  running
                    ? isRollingBack
                      ? "border-warning-border bg-warning-soft text-warning"
                      : "border-info-border bg-info-soft text-info"
                    : failed
                      ? "border-danger-border bg-danger-soft text-danger"
                      : phases.length > 0
                        ? "border-success-border bg-success-soft text-success"
                        : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]",
                )}
              >
                {running
                  ? isRollingBack
                    ? "回滚运行中"
                    : "更新运行中"
                  : failed
                    ? "上次失败"
                    : phases.length > 0
                      ? "上次完成"
                      : "空闲"}
              </span>
            </div>
            <p className="mt-1 truncate type-caption text-[var(--fg-2)]">
              {running
                ? `${runningTarget} · ${phaseLabel(activePhase?.phase ?? "")}`
                : failed
                  ? `失败于 ${phaseLabel(activePhase?.phase ?? "")}`
                  : status?.started_at
                    ? `最近任务 ${formatDateTime(status.started_at)}`
                    : "步骤、实时输出和 release 历史已收起。"}
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

      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        {running && (
          <span
            className={cn(
              "rounded-[var(--radius-control)] border px-2 py-1",
              streamStatus === "open"
                ? "border-success-border bg-success-soft text-success"
                : streamStatus === "connecting"
                  ? "border-info-border bg-info-soft text-info"
                  : streamStatus === "broken"
                    ? "border-danger-border bg-danger-soft text-danger"
                    : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]",
            )}
          >
            实时流：
            {streamStatus === "open"
              ? "已连接"
              : streamStatus === "connecting"
                ? "连接中"
                : streamStatus === "broken"
                  ? "中断，请刷新"
                  : streamStatus === "error"
                    ? "重连中"
                    : "未连接"}
          </span>
        )}
        {phases.length > 0 && (
          <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-[var(--fg-1)]">
            步骤 {completedCount}/{totalCount}
          </span>
        )}
        {logBuffer.length > 0 && (
          <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 font-mono text-[var(--fg-2)]">
            log {logBuffer.length}
          </span>
        )}
      </div>

      {error && (
        <p className="mt-3 type-caption text-danger">
          更新状态读取失败：{error.message}
        </p>
      )}

      {effectiveBanner && (
        <div
          className={cn(
            "mt-3 flex items-start justify-between gap-3 rounded-[var(--radius-card)] border px-3 py-2 type-body-sm",
            effectiveBanner.kind === "success"
              ? "border-success-border bg-success-soft text-success"
              : effectiveBanner.kind === "error"
                ? "border-danger-border bg-danger-soft text-danger"
                : "border-info-border bg-info-soft text-info",
          )}
        >
          <span className="min-w-0 break-words">{effectiveBanner.text}</span>
          <IconButton
            variant="ghost"
            size="sm"
            onClick={onClearBanner}
            aria-label={copy.action.close}
            className="shrink-0"
          >
            <X className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      )}

      {reloadCountdown != null && (
        <div className="mt-3 flex items-center justify-between gap-3 rounded-[var(--radius-card)] border border-success-border bg-success-soft px-3 py-2.5 type-body-sm text-success">
          <div className="flex min-w-0 items-center gap-2">
            <Check className="h-4 w-4 shrink-0 text-success" />
            <span className="min-w-0">
              更新成功 · <span className="font-mono">{reloadCountdown}s</span>{" "}
              后自动刷新页面以加载新版本
            </span>
          </div>
          <div className="flex shrink-0 gap-1.5">
            <button
              type="button"
              onClick={cancelReload}
              className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-[11px] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)]"
            >
              {copy.action.cancel}
            </button>
            <button
              type="button"
              onClick={reloadNow}
              className="rounded-[var(--radius-control)] bg-success px-2 py-1 text-[11px] font-medium text-[var(--success-on)] transition-[filter] hover:brightness-110"
            >
              立即刷新
            </button>
          </div>
        </div>
      )}

      {(running || phases.length > 0) && (
        <div className="mt-3">
          <div className="flex items-center justify-between gap-3">
            <span className="truncate text-xs font-medium text-[var(--fg-1)]">
              {running
                ? `正在执行：${phaseLabel(activePhase?.phase ?? "")}`
                : failed
                  ? `失败于：${phaseLabel(activePhase?.phase ?? "")}`
                  : "更新已完成"}
            </span>
            <span className="shrink-0 font-mono text-[11px] text-[var(--fg-2)]">
              {progressPct}%
            </span>
          </div>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--bg-2)]">
            <div
              className={cn(
                "h-full transition-[width] duration-500 ease-out",
                failed ? "bg-danger/80" : running ? "bg-info/80" : "bg-success/80",
              )}
              style={{ width: `${progressPct}%` }}
            />
          </div>
        </div>
      )}

      <AnimatePresence initial={false}>
        {detailsOpen && (
          <motion.div
            key="update-details"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="mt-4 space-y-4 border-t border-[var(--border-subtle)] pt-4">
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={onTrigger}
                  disabled={disabled}
                  loading={triggering || running}
                  leftIcon={
                    !(triggering || running) ? (
                      <Rocket className="h-3.5 w-3.5" />
                    ) : undefined
                  }
                >
                  {triggering || running ? "更新中" : "运行更新脚本"}
                </Button>
              </div>

              <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60">
                <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-3 py-2">
                  <span className="text-xs font-medium text-[var(--fg-1)]">执行步骤</span>
                  {phases.length > 0 && (
                    <span className="text-[11px] text-[var(--fg-2)]">
                      {completedCount} / {totalCount} 完成
                    </span>
                  )}
                </div>
                <ol className="divide-y divide-[var(--border-subtle)]">
                  {checklist.map((phase) => (
                    <PhaseRow key={phase} phase={phase} record={phaseByName.get(phase)} />
                  ))}
                </ol>
              </div>

              <div>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={onLogToggle}
                  leftIcon={<Terminal className="h-3.5 w-3.5" />}
                  rightIcon={
                    logBuffer.length > 0 ? (
                      <span className="rounded-full bg-[var(--bg-2)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--fg-2)]">
                        {logBuffer.length}
                      </span>
                    ) : undefined
                  }
                >
                  {logOpen ? "收起实时输出" : "查看实时输出"}
                </Button>
                <AnimatePresence initial={false}>
                  {logOpen && (
                    <motion.div
                      key="log-panel"
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: "auto" }}
                      exit={{ opacity: 0, height: 0 }}
                      transition={{ duration: 0.18 }}
                      className="overflow-hidden"
                    >
                      <pre
                        ref={logRef}
                        onScroll={onLogScroll}
                        className="mt-2 max-h-72 overflow-auto rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/80 p-3 font-mono text-[11px] leading-5 text-[var(--fg-1)]"
                      >
                        {logBuffer.length > 0
                          ? logBuffer.join("\n")
                          : status?.log_tail
                            ? status.log_tail
                            : "（暂无输出）"}
                      </pre>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60">
                <div className="flex items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2">
                  <History className="h-3.5 w-3.5 text-[var(--fg-2)]" />
                  <span className="text-xs font-medium text-[var(--fg-1)]">Release 历史</span>
                  <span className="text-[11px] text-[var(--fg-2)]">最近 10 个版本</span>
                </div>
                {releasesError ? (
                  <p role="alert" className="px-3 py-3 type-caption text-danger">
                    读取 release 列表失败：{releasesError.message}
                  </p>
                ) : releasesLoading && !releases ? (
                  <div className="space-y-1.5 p-3">
                    {[0, 1, 2].map((i) => (
                      <div
                        key={i}
                        className="h-10 animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-2)]"
                        style={{ animationDelay: `${i * 60}ms` }}
                      />
                    ))}
                  </div>
                ) : !releases || releases.length === 0 ? (
                  <p className="px-3 py-3 text-xs text-[var(--fg-2)]">暂无 release 记录。</p>
                ) : (
                  <ul className="divide-y divide-[var(--border-subtle)]">
                    {releases.map((r) => (
                      <ReleaseRow
                        key={r.id}
                        release={r}
                        rollingBack={rollbackPendingId === r.id}
                        disabled={disabled}
                        onRollback={() => setPendingRollback(r)}
                      />
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <ConfirmDialog
        open={pendingRollback != null}
        onOpenChange={(open) => {
          if (!open) setPendingRollback(null);
        }}
        title="回滚到此版本？"
        description={
          pendingRollback
            ? `回滚到 release ${pendingRollback.id}？将切回旧代码并重启 Lumen 服务（约 30 秒不可用）。数据库不会回滚，仅切代码。`
            : ""
        }
        confirmText="确认回滚"
        cancelText="取消"
        tone="danger"
        confirming={isRollingBack}
        onConfirm={() => {
          if (!pendingRollback) return;
          const id = pendingRollback.id;
          setPendingRollback(null);
          onRollback(id);
        }}
      />
    </div>
  );
}

function PhaseRow({
  phase,
  record,
}: {
  phase: string;
  record: UpdateStepRecord | undefined;
}) {
  const status = record?.status;
  const rc = record?.rc;
  const isDone = status === "done";
  const isRunning = status === "running";
  const isFailed = isDone && rc != null && rc !== 0;
  const isOk = isDone && (rc == null || rc === 0);
  const dur = formatDuration(record?.dur_ms);
  const infoEntries = record?.info ? Object.entries(record.info) : [];

  return (
    <li className="flex items-start gap-3 px-3 py-2">
      <span
        className={cn(
          "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px]",
          isRunning
            ? "border-info-border bg-info-soft text-info"
            : isOk
              ? "border-success-border bg-success-soft text-success"
              : isFailed
                ? "border-danger-border bg-danger-soft text-danger"
                : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]",
        )}
        aria-hidden="true"
      >
        {isRunning ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : isOk ? (
          <Check className="h-3 w-3" />
        ) : isFailed ? (
          <X className="h-3 w-3" />
        ) : (
          <Circle className="h-2 w-2" />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span
            className={cn(
              "text-xs",
              isRunning
                ? "text-info"
                : isFailed
                  ? "text-danger"
                  : isOk
                    ? "text-[var(--fg-1)]"
                    : "text-[var(--fg-2)]",
            )}
          >
            {phaseLabel(phase)}
          </span>
          <span className="font-mono text-[10px] text-[var(--fg-3)]">{phase}</span>
          {isFailed && rc != null && (
            <span className="rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-1.5 py-0.5 font-mono text-[10px] text-danger">
              rc={rc}
            </span>
          )}
        </div>
        {infoEntries.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-[var(--fg-2)]">
            {infoEntries.map(([k, v]) => (
              <span key={k} className="font-mono">
                {k}={v}
              </span>
            ))}
          </div>
        )}
      </div>
      {dur && (
        <span className="ml-2 shrink-0 self-center text-[11px] tabular-nums text-[var(--fg-2)]">
          {dur}
        </span>
      )}
    </li>
  );
}

function ReleaseRow({
  release,
  rollingBack,
  disabled,
  onRollback,
}: {
  release: ReleaseInfo;
  rollingBack: boolean;
  disabled: boolean;
  onRollback: () => void;
}) {
  const alembic = release.alembic_head_applied || release.alembic_head_expected;
  const showRollback = !release.is_current;
  return (
    <li className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center sm:gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="font-mono text-xs text-[var(--fg-1)]" title={release.id}>
            {shortReleaseId(release.id)}
          </span>
          {release.is_current && (
            <span className="rounded-[var(--radius-control)] border border-success-border bg-success-soft px-1.5 py-0.5 text-[10px] text-success">
              当前
            </span>
          )}
          {release.is_previous && !release.is_current && (
            <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-1.5 py-0.5 text-[10px] text-[var(--fg-2)]">
              上一个
            </span>
          )}
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-[var(--fg-2)]">
          <span>{formatDateTime(release.created_at)}</span>
          <span className="font-mono" title={release.sha ?? undefined}>
            sha {shortSha(release.sha)}
          </span>
          {release.branch && <span>分支 {release.branch}</span>}
          {alembic && (
            <span className="font-mono" title={alembic}>
              alembic {alembic.slice(0, 12)}
            </span>
          )}
        </div>
      </div>
      {showRollback && (
        <Button
          variant="secondary"
          size="sm"
          onClick={onRollback}
          disabled={disabled}
          loading={rollingBack}
          leftIcon={!rollingBack ? <Undo2 className="h-3 w-3" /> : undefined}
          className="self-start sm:self-center"
        >
          {rollingBack ? "回滚中" : "回滚到此版本"}
        </Button>
      )}
    </li>
  );
}

function phaseLabel(phase: string): string {
  return PHASE_LABEL[phase] ?? phase;
}

function formatDuration(ms: number | null | undefined): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const totalSec = ms / 1000;
  if (totalSec < 60) return `${totalSec.toFixed(1)}s`;
  const m = Math.floor(totalSec / 60);
  const s = Math.round(totalSec - m * 60);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function shortReleaseId(id: string): string {
  if (id.length <= 28) return id;
  return id.slice(0, 28) + "…";
}

function shortSha(sha?: string | null): string {
  if (!sha) return "未知";
  return sha.length > 7 ? sha.slice(0, 7) : sha;
}

function formatDateTime(value: string) {
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
