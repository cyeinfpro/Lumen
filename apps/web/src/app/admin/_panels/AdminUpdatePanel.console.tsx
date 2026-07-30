"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type UIEventHandler,
} from "react";

import type {
  AdminUpdateStatusOut,
  ReleaseInfo,
  UpdateStepRecord,
} from "@/lib/apiClient";
import {
  anyPending,
  effectiveUpdateBanner,
  PHASE_ORDER,
  phasesFor,
  progressPercent,
  runningTargetFor,
  updateRunningFor,
  type AdminStreamStatus,
  type UpdateBanner,
} from "./AdminUpdatePanel.helpers";
import { UpdateDetails } from "./AdminUpdatePanel.details";
import { RollbackConfirmDialog } from "./AdminUpdatePanel.dialogs";
import {
  ReloadNotice,
  UpdateBannerNotice,
  UpdateConsoleHeader,
  UpdateConsoleMeta,
  UpdateProgress,
  UpdateStatusError,
} from "./AdminUpdatePanel.status";

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

const RELOAD_DELAY_SEC = 6;

export function LumenUpdateBlock({
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
    const records = new Map<string, UpdateStepRecord>();
    for (const phase of phases) records.set(phase.phase, phase);
    return records;
  }, [phases]);
  const checklist = useMemo<string[]>(() => {
    const order = [...PHASE_ORDER];
    const seen = new Set(order);
    for (const phase of phases) {
      if (!seen.has(phase.phase)) {
        order.push(phase.phase);
        seen.add(phase.phase);
      }
    }
    return order;
  }, [phases]);
  const failed = useMemo(
    () => phases.some((phase) => phase.status === "done" && phase.rc != null && phase.rc !== 0),
    [phases],
  );
  const activePhase = useMemo(() => {
    const runningIndex = phases.findIndex((phase) => phase.status === "running");
    if (runningIndex >= 0) return phases[runningIndex];
    if (phases.length === 0) return null;
    return phases[phases.length - 1];
  }, [phases]);
  const completedCount = useMemo(
    () =>
      phases.filter(
        (phase) => phase.status === "done" && (phase.rc ?? 0) === 0,
      ).length,
    [phases],
  );
  const totalCount = checklist.length;
  const progressPct = progressPercent(completedCount, totalCount);

  const [userLogOpen, setUserLogOpen] = useState<boolean | null>(null);
  const logOpen = userLogOpen ?? running;
  const onLogToggle = useCallback(() => {
    setUserLogOpen((previous) => !(previous ?? running));
  }, [running]);
  const [userDetailsOpen, setUserDetailsOpen] = useState<boolean | null>(null);
  const detailsOpen = userDetailsOpen ?? (running || failed);
  const onDetailsToggle = useCallback(() => {
    setUserDetailsOpen((previous) => !(previous ?? (running || failed)));
  }, [failed, running]);

  const [reloadCountdown, setReloadCountdown] = useState<number | null>(null);
  const reloadNow = useCallback(() => {
    if (typeof window !== "undefined") window.location.reload();
  }, []);
  const cancelReload = useCallback(() => setReloadCountdown(null), []);
  const previousRunningRef = useRef(running);
  useEffect(() => {
    const wasRunning = previousRunningRef.current;
    previousRunningRef.current = running;
    if (!wasRunning || running || failed) return;
    const timeout = setTimeout(() => setReloadCountdown(RELOAD_DELAY_SEC), 0);
    return () => clearTimeout(timeout);
  }, [running, failed]);
  useEffect(() => {
    if (reloadCountdown == null) return;
    if (reloadCountdown <= 0) {
      reloadNow();
      return;
    }
    const timeout = setTimeout(
      () => setReloadCountdown((count) => (count == null ? null : count - 1)),
      1000,
    );
    return () => clearTimeout(timeout);
  }, [reloadCountdown, reloadNow]);

  const logRef = useRef<HTMLPreElement | null>(null);
  const userScrolledRef = useRef(false);
  useEffect(() => {
    if (!logOpen) return;
    const element = logRef.current;
    if (!element) return;
    if (!userScrolledRef.current) element.scrollTop = element.scrollHeight;
  }, [logOpen, logBuffer]);
  const onLogScroll: UIEventHandler<HTMLPreElement> = (event) => {
    const element = event.currentTarget;
    const distanceFromBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight;
    userScrolledRef.current = distanceFromBottom > 16;
  };

  const [pendingRollback, setPendingRollback] = useState<ReleaseInfo | null>(null);
  useEffect(() => {
    if (!banner || banner.kind === "error") return;
    const timeout = setTimeout(() => onClearBanner(), 6000);
    return () => clearTimeout(timeout);
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
