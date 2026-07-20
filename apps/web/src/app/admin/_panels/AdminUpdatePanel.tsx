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
import {
  anyPending,
  effectiveUpdateBanner,
  formatDateTime,
  formatDuration,
  mutationErrorText,
  PHASE_ORDER,
  phaseLabel,
  phasesFor,
  progressPercent,
  rollbackPendingIdFor,
  rollbackStartedBanner,
  runningTargetFor,
  setRunningUpdateStatus,
  shortReleaseId,
  shortSha,
  triggerStartedText,
  updatePollInterval,
  updateRunningFor,
  type AdminStreamStatus,
  type PendingUpdateConfirm,
  type UpdateBanner,
} from "./AdminUpdatePanel.helpers";
import {
  useAdminUpdateStream,
  useDisarmUpdateStream,
} from "./AdminUpdatePanel.hooks";
import { UpdateNetworkSettingsCard } from "./AdminUpdatePanel.network";

interface LumenUpdateBlockProps {
  status: AdminUpdateStatusOut | undefined;
  loading: boolean;
  error: Error | null;
  triggering: boolean;
  banner: UpdateBanner | null;
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

export function AdminUpdatePanel() {
  return <AdminUpdatePanelInner />;
}

function AdminUpdatePanelInner() {
  const queryClient = useQueryClient();
  const settingsQ = useSystemSettingsQuery({ retry: false });
  const proxiesQ = useAdminProxiesQuery({ retry: false });
  const updateSettingsMut = useUpdateSystemSettingsMutation();
  const streamConnectedRef = useRef(false);
  const updateStatusQ = useAdminUpdateStatusQuery({
    retry: false,
    refetchInterval: (query) =>
      updatePollInterval(streamConnectedRef.current, query.state.data?.running),
  });
  const updateVersionQ = useAdminUpdateVersionQuery({ retry: false });
  const updateCheckQ = useAdminCheckUpdateQuery(false, { retry: false });
  const releasesQ = useAdminReleasesQuery({ retry: false });

  const [updateBanner, setUpdateBanner] = useState<UpdateBanner | null>(null);
  const [updateStreamArmed, setUpdateStreamArmed] = useState(false);
  const [manualCheckPending, setManualCheckPending] = useState(false);
  const [pendingUpdateConfirm, setPendingUpdateConfirm] =
    useState<PendingUpdateConfirm | null>(null);

  const triggerUpdateMut = useTriggerAdminUpdateMutation({
    onSuccess: (result) => {
      setUpdateStreamArmed(true);
      setRunningUpdateStatus(queryClient, result.started_at, result);
      setUpdateBanner({
        kind: "success",
        text: triggerStartedText(result),
      });
    },
    onError: (err) => {
      setUpdateStreamArmed(false);
      const msg = mutationErrorText(err, "触发更新失败");
      setUpdateBanner({ kind: "error", text: `触发更新失败：${msg}` });
    },
  });

  const previousRollbackMut = useRollbackPreviousMutation({
    onSuccess: (result) => {
      setUpdateStreamArmed(true);
      setRunningUpdateStatus(queryClient, result.started_at);
      setUpdateBanner(rollbackStartedBanner(result, true));
    },
  });

  const rollbackMut = useRollbackReleaseMutation({
    onSuccess: (result) => {
      setUpdateStreamArmed(true);
      setRunningUpdateStatus(queryClient, result.started_at);
      setUpdateBanner(rollbackStartedBanner(result, false));
    },
    onError: (err) => {
      setUpdateStreamArmed(false);
      const msg = mutationErrorText(err, "触发回滚失败");
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
  const triggering = anyPending(
    triggerUpdateMut.isPending,
    rollbackMut.isPending,
    previousRollbackMut.isPending,
  );
  const sseEnabled = anyPending(updateRunning, updateStreamArmed, triggering);
  const { logBuffer, streamStatus, clearLogs } = useAdminUpdateStream(sseEnabled);

  useEffect(() => {
    streamConnectedRef.current = streamStatus === "open";
  }, [streamStatus]);

  useDisarmUpdateStream(
    updateStreamArmed,
    setUpdateStreamArmed,
    triggering,
    updateRunning,
  );
  const requestUpdateConfirm = () => {
    const targetTag = updateCheckQ.data?.resolved_image_tag?.trim();
    if (!targetTag) {
      setUpdateBanner({
        kind: "error",
        text: "请先重新检查更新，确认目标版本后再运行更新脚本。",
      });
      return;
    }
    setPendingUpdateConfirm({
      targetTag,
      channel: updateCheckQ.data?.channel ?? null,
    });
  };
  const triggerConfirmedUpdate = () => {
    if (!pendingUpdateConfirm) return;
    setUpdateBanner(null);
    clearLogs();
    triggerUpdateMut.mutate({
      target_tag: pendingUpdateConfirm.targetTag,
      channel: pendingUpdateConfirm.channel ?? undefined,
      force_redeploy: false,
      confirm_update: true,
      confirmed_target_tag: pendingUpdateConfirm.targetTag,
    });
    setPendingUpdateConfirm(null);
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
        onTrigger={requestUpdateConfirm}
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
        rollbackPendingId={rollbackPendingIdFor(
          previousRollbackMut.isPending,
          rollbackMut.isPending,
          rollbackMut.variables,
        )}
        logBuffer={logBuffer}
        streamStatus={streamStatus}
        onTrigger={requestUpdateConfirm}
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

      <UpdateConfirmDialog
        pending={pendingUpdateConfirm}
        confirming={triggerUpdateMut.isPending}
        onClose={() => setPendingUpdateConfirm(null)}
        onConfirm={triggerConfirmedUpdate}
      />
    </section>
  );
}

function UpdateConfirmDialog({
  pending,
  confirming,
  onClose,
  onConfirm,
}: {
  pending: PendingUpdateConfirm | null;
  confirming: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const description = pending ? (
    <div className="space-y-2">
      <p>
        将更新到
        <span className="font-mono text-[var(--fg-0)]">
          {" "}
          {pending.targetTag}
        </span>
        ，期间服务会重启并短暂不可用。
      </p>
      <p className="text-[var(--fg-2)]">请确认目标版本无误后再继续。</p>
    </div>
  ) : null;
  return (
    <ConfirmDialog
      open={pending != null}
      onOpenChange={(open) => {
        if (!open && !confirming) onClose();
      }}
      title="确认运行更新？"
      description={description}
      confirmText="确认更新"
      cancelText="取消"
      tone="danger"
      confirming={confirming}
      onConfirm={onConfirm}
    />
  );
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
  const running = updateRunningFor(status);
  const isRollingBack = rollbackPendingId != null;
  const disabled = anyPending(triggering, running, isRollingBack);
  const runningTarget = runningTargetFor(status);
  const phases = useMemo(() => phasesFor(status), [status]);
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
  const progressPct = progressPercent(completedCount, totalCount);

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

  const effectiveBanner = effectiveUpdateBanner(banner, failed, running);

  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-3 shadow-[var(--shadow-1)] backdrop-blur-sm">
      <UpdateConsoleHeader
        running={running}
        failed={failed}
        isRollingBack={isRollingBack}
        phases={phases}
        runningTarget={runningTarget}
        activePhase={activePhase}
        startedAt={status?.started_at}
        loading={loading}
        disabled={disabled}
        detailsOpen={detailsOpen}
        onRefresh={onRefresh}
        onRollbackPrevious={onRollbackPrevious}
        onDetailsToggle={onDetailsToggle}
      />
      <UpdateConsoleMeta
        running={running}
        streamStatus={streamStatus}
        phaseCount={phases.length}
        completedCount={completedCount}
        totalCount={totalCount}
        logCount={logBuffer.length}
      />
      <UpdateStatusError error={error} />
      <UpdateBannerNotice banner={effectiveBanner} onClear={onClearBanner} />
      <ReloadNotice
        countdown={reloadCountdown}
        onCancel={cancelReload}
        onReload={reloadNow}
      />
      <UpdateProgress
        visible={anyPending(running, phases.length > 0)}
        running={running}
        failed={failed}
        activePhase={activePhase}
        progressPct={progressPct}
      />
      <UpdateDetails
        open={detailsOpen}
        triggering={triggering}
        running={running}
        disabled={disabled}
        phases={phases}
        completedCount={completedCount}
        totalCount={totalCount}
        checklist={checklist}
        phaseByName={phaseByName}
        logOpen={logOpen}
        logBuffer={logBuffer}
        logTail={status?.log_tail}
        logRef={logRef}
        releases={releases}
        releasesLoading={releasesLoading}
        releasesError={releasesError}
        rollbackPendingId={rollbackPendingId}
        onTrigger={onTrigger}
        onLogToggle={onLogToggle}
        onLogScroll={onLogScroll}
        onSelectRelease={setPendingRollback}
      />
      <RollbackConfirmDialog
        pending={pendingRollback}
        confirming={isRollingBack}
        onClose={() => setPendingRollback(null)}
        onConfirm={(releaseId) => {
          setPendingRollback(null);
          onRollback(releaseId);
        }}
      />
    </div>
  );
}

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
  return "步骤、实时输出和 release 历史已收起。";
}

function UpdateConsoleHeader({
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
            <h3 className="type-card-title text-sm">更新控制台</h3>
            <span
              className={cn(
                "rounded-[var(--radius-control)] border px-2 py-0.5 text-[11px]",
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
      return "中断，请刷新";
    case "error":
      return "重连中";
    default:
      return "未连接";
  }
}

function UpdateConsoleMeta({
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
    <div className="mt-3 flex flex-wrap gap-2 text-xs">
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
          log {logCount}
        </span>
      )}
    </div>
  );
}

function UpdateStatusError({ error }: { error: Error | null }) {
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

function UpdateBannerNotice({
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

function ReloadNotice({
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
          className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-[11px] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)]"
        >
          {copy.action.cancel}
        </button>
        <button
          type="button"
          onClick={onReload}
          className="rounded-[var(--radius-control)] bg-success px-2 py-1 text-[11px] font-medium text-[var(--success-on)] transition-[filter] hover:brightness-110"
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
  if (running) return `正在执行：${phaseLabel(activePhase?.phase ?? "")}`;
  if (failed) return `失败于：${phaseLabel(activePhase?.phase ?? "")}`;
  return "更新已完成";
}

function progressClass(running: boolean, failed: boolean): string {
  if (failed) return "bg-danger/80";
  if (running) return "bg-info/80";
  return "bg-success/80";
}

function UpdateProgress({
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
        <span className="truncate text-xs font-medium text-[var(--fg-1)]">
          {progressLabel(running, failed, activePhase)}
        </span>
        <span className="shrink-0 font-mono text-[11px] text-[var(--fg-2)]">
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

function PhaseChecklist({
  phases,
  completedCount,
  totalCount,
  checklist,
  phaseByName,
}: {
  phases: UpdateStepRecord[];
  completedCount: number;
  totalCount: number;
  checklist: string[];
  phaseByName: Map<string, UpdateStepRecord>;
}) {
  return (
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
  );
}

function logText(logBuffer: string[], logTail?: string): string {
  if (logBuffer.length > 0) return logBuffer.join("\n");
  if (logTail) return logTail;
  return "（暂无输出）";
}

function UpdateLogSection({
  open,
  logBuffer,
  logTail,
  logRef,
  onToggle,
  onScroll,
}: {
  open: boolean;
  logBuffer: string[];
  logTail?: string;
  logRef: React.RefObject<HTMLPreElement | null>;
  onToggle: () => void;
  onScroll: React.UIEventHandler<HTMLPreElement>;
}) {
  return (
    <div>
      <Button
        variant="secondary"
        size="sm"
        onClick={onToggle}
        leftIcon={<Terminal className="h-3.5 w-3.5" />}
        rightIcon={
          logBuffer.length > 0 ? (
            <span className="rounded-full bg-[var(--bg-2)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--fg-2)]">
              {logBuffer.length}
            </span>
          ) : undefined
        }
      >
        {open ? "收起实时输出" : "查看实时输出"}
      </Button>
      <AnimatePresence initial={false}>
        {open && (
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
              onScroll={onScroll}
              className="mt-2 max-h-72 overflow-auto rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/80 p-3 font-mono text-[11px] leading-5 text-[var(--fg-1)]"
            >
              {logText(logBuffer, logTail)}
            </pre>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function ReleaseHistoryBody({
  releases,
  loading,
  error,
  rollbackPendingId,
  disabled,
  onSelect,
}: {
  releases: ReleaseInfo[] | undefined;
  loading: boolean;
  error: Error | null;
  rollbackPendingId: string | null;
  disabled: boolean;
  onSelect: (release: ReleaseInfo) => void;
}) {
  if (error) {
    return (
      <p role="alert" className="px-3 py-3 type-caption text-danger">
        读取 release 列表失败：{error.message}
      </p>
    );
  }
  if (loading && !releases) {
    return (
      <div className="space-y-1.5 p-3">
        {[0, 1, 2].map((index) => (
          <div
            key={index}
            className="h-10 animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-2)]"
            style={{ animationDelay: `${index * 60}ms` }}
          />
        ))}
      </div>
    );
  }
  if (!releases || releases.length === 0) {
    return <p className="px-3 py-3 text-xs text-[var(--fg-2)]">暂无 release 记录。</p>;
  }
  return (
    <ul className="divide-y divide-[var(--border-subtle)]">
      {releases.map((release) => (
        <ReleaseRow
          key={release.id}
          release={release}
          rollingBack={rollbackPendingId === release.id}
          disabled={disabled}
          onRollback={() => onSelect(release)}
        />
      ))}
    </ul>
  );
}

function ReleaseHistory({
  releases,
  loading,
  error,
  rollbackPendingId,
  disabled,
  onSelect,
}: {
  releases: ReleaseInfo[] | undefined;
  loading: boolean;
  error: Error | null;
  rollbackPendingId: string | null;
  disabled: boolean;
  onSelect: (release: ReleaseInfo) => void;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60">
      <div className="flex items-center gap-2 border-b border-[var(--border-subtle)] px-3 py-2">
        <History className="h-3.5 w-3.5 text-[var(--fg-2)]" />
        <span className="text-xs font-medium text-[var(--fg-1)]">Release 历史</span>
        <span className="text-[11px] text-[var(--fg-2)]">最近 10 个版本</span>
      </div>
      <ReleaseHistoryBody
        releases={releases}
        loading={loading}
        error={error}
        rollbackPendingId={rollbackPendingId}
        disabled={disabled}
        onSelect={onSelect}
      />
    </div>
  );
}

function UpdateDetails({
  open,
  triggering,
  running,
  disabled,
  phases,
  completedCount,
  totalCount,
  checklist,
  phaseByName,
  logOpen,
  logBuffer,
  logTail,
  logRef,
  releases,
  releasesLoading,
  releasesError,
  rollbackPendingId,
  onTrigger,
  onLogToggle,
  onLogScroll,
  onSelectRelease,
}: {
  open: boolean;
  triggering: boolean;
  running: boolean;
  disabled: boolean;
  phases: UpdateStepRecord[];
  completedCount: number;
  totalCount: number;
  checklist: string[];
  phaseByName: Map<string, UpdateStepRecord>;
  logOpen: boolean;
  logBuffer: string[];
  logTail?: string;
  logRef: React.RefObject<HTMLPreElement | null>;
  releases: ReleaseInfo[] | undefined;
  releasesLoading: boolean;
  releasesError: Error | null;
  rollbackPendingId: string | null;
  onTrigger: () => void;
  onLogToggle: () => void;
  onLogScroll: React.UIEventHandler<HTMLPreElement>;
  onSelectRelease: (release: ReleaseInfo) => void;
}) {
  const busy = anyPending(triggering, running);
  return (
    <AnimatePresence initial={false}>
      {open && (
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
                loading={busy}
                leftIcon={!busy ? <Rocket className="h-3.5 w-3.5" /> : undefined}
              >
                {busy ? "更新中" : "运行更新脚本"}
              </Button>
            </div>
            <PhaseChecklist
              phases={phases}
              completedCount={completedCount}
              totalCount={totalCount}
              checklist={checklist}
              phaseByName={phaseByName}
            />
            <UpdateLogSection
              open={logOpen}
              logBuffer={logBuffer}
              logTail={logTail}
              logRef={logRef}
              onToggle={onLogToggle}
              onScroll={onLogScroll}
            />
            <ReleaseHistory
              releases={releases}
              loading={releasesLoading}
              error={releasesError}
              rollbackPendingId={rollbackPendingId}
              disabled={disabled}
              onSelect={onSelectRelease}
            />
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function RollbackConfirmDialog({
  pending,
  confirming,
  onClose,
  onConfirm,
}: {
  pending: ReleaseInfo | null;
  confirming: boolean;
  onClose: () => void;
  onConfirm: (releaseId: string) => void;
}) {
  const description = pending
    ? `回滚到 release ${pending.id}？将切回旧代码并重启 Lumen 服务（约 30 秒不可用）。数据库不会回滚，仅切代码。`
    : "";
  return (
    <ConfirmDialog
      open={pending != null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="回滚到此版本？"
      description={description}
      confirmText="确认回滚"
      cancelText="取消"
      tone="danger"
      confirming={confirming}
      onConfirm={() => {
        if (pending) onConfirm(pending.id);
      }}
    />
  );
}

type PhaseVisualState = "running" | "ok" | "failed" | "idle";

function phaseVisualState(
  status: UpdateStepRecord["status"] | undefined,
  rc: number | null | undefined,
): PhaseVisualState {
  if (status === "running") return "running";
  if (status !== "done") return "idle";
  if (rc != null && rc !== 0) return "failed";
  return "ok";
}

function phaseIconClass(state: PhaseVisualState): string {
  switch (state) {
    case "running":
      return "border-info-border bg-info-soft text-info";
    case "ok":
      return "border-success-border bg-success-soft text-success";
    case "failed":
      return "border-danger-border bg-danger-soft text-danger";
    default:
      return "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]";
  }
}

function PhaseStateIcon({ state }: { state: PhaseVisualState }) {
  switch (state) {
    case "running":
      return <Loader2 className="h-3 w-3 animate-spin" />;
    case "ok":
      return <Check className="h-3 w-3" />;
    case "failed":
      return <X className="h-3 w-3" />;
    default:
      return <Circle className="h-2 w-2" />;
  }
}

function phaseTextClass(state: PhaseVisualState): string {
  switch (state) {
    case "running":
      return "text-info";
    case "failed":
      return "text-danger";
    case "ok":
      return "text-[var(--fg-1)]";
    default:
      return "text-[var(--fg-2)]";
  }
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
  const visualState = phaseVisualState(status, rc);
  const dur = formatDuration(record?.dur_ms);
  const infoEntries = record?.info ? Object.entries(record.info) : [];

  return (
    <li className="flex items-start gap-3 px-3 py-2">
      <span
        className={cn(
          "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px]",
          phaseIconClass(visualState),
        )}
        aria-hidden="true"
      >
        <PhaseStateIcon state={visualState} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span
            className={cn(
              "text-xs",
              phaseTextClass(visualState),
            )}
          >
            {phaseLabel(phase)}
          </span>
          <span className="font-mono text-[10px] text-[var(--fg-3)]">{phase}</span>
          {visualState === "failed" && rc != null && (
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
