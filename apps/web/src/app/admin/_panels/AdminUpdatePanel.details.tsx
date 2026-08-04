"use client";

import type { RefObject, UIEventHandler } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  Check,
  Circle,
  History,
  Loader2,
  Rocket,
  Terminal,
  Undo2,
  X,
} from "lucide-react";

import type { ReleaseInfo, UpdateStepRecord } from "@/lib/apiClient";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import {
  anyPending,
  formatDateTime,
  formatDuration,
  phaseLabel,
  shortReleaseId,
  shortSha,
} from "./AdminUpdatePanel.helpers";

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
        <span className="type-caption font-medium text-[var(--fg-1)]">执行步骤</span>
        {phases.length > 0 && (
          <span className="type-caption text-[var(--fg-2)]">
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
  logRef: RefObject<HTMLPreElement | null>;
  onToggle: () => void;
  onScroll: UIEventHandler<HTMLPreElement>;
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
            <span className="rounded-full bg-[var(--bg-2)] px-1.5 py-0.5 font-mono type-caption text-[var(--fg-2)]">
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
              className="mt-2 max-h-72 overflow-auto rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/80 p-3 font-mono type-caption leading-5 text-[var(--fg-1)]"
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
        发布列表读取失败：{error.message}
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
    return <p className="px-3 py-3 type-caption text-[var(--fg-2)]">暂无发布记录。</p>;
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
        <span className="type-caption font-medium text-[var(--fg-1)]">发布历史</span>
        <span className="type-caption text-[var(--fg-2)]">最近 10 个版本</span>
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

export function UpdateDetails({
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
  logRef: RefObject<HTMLPreElement | null>;
  releases: ReleaseInfo[] | undefined;
  releasesLoading: boolean;
  releasesError: Error | null;
  rollbackPendingId: string | null;
  onTrigger: () => void;
  onLogToggle: () => void;
  onLogScroll: UIEventHandler<HTMLPreElement>;
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
  const duration = formatDuration(record?.dur_ms);
  const infoEntries = record?.info ? Object.entries(record.info) : [];

  return (
    <li className="flex items-start gap-3 px-3 py-2">
      <span
        className={cn(
          "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border type-caption",
          phaseIconClass(visualState),
        )}
        aria-hidden="true"
      >
        <PhaseStateIcon state={visualState} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className={cn("type-caption", phaseTextClass(visualState))}>
            {phaseLabel(phase)}
          </span>
          <span className="font-mono type-caption text-[var(--fg-3)]">{phase}</span>
          {visualState === "failed" && rc != null && (
            <span className="rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-1.5 py-0.5 font-mono type-caption text-danger">
              rc={rc}
            </span>
          )}
        </div>
        {infoEntries.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 type-caption text-[var(--fg-2)]">
            {infoEntries.map(([key, value]) => (
              <span key={key} className="font-mono">
                {key}={value}
              </span>
            ))}
          </div>
        )}
      </div>
      {duration && (
        <span className="ml-2 shrink-0 self-center type-caption tabular-nums text-[var(--fg-2)]">
          {duration}
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
          <span className="font-mono type-caption text-[var(--fg-1)]" title={release.id}>
            {shortReleaseId(release.id)}
          </span>
          {release.is_current && (
            <span className="rounded-[var(--radius-control)] border border-success-border bg-success-soft px-1.5 py-0.5 type-caption text-success">
              当前
            </span>
          )}
          {release.is_previous && !release.is_current && (
            <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-1.5 py-0.5 type-caption text-[var(--fg-2)]">
              上一个
            </span>
          )}
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 type-caption text-[var(--fg-2)]">
          <span>{formatDateTime(release.created_at)}</span>
          <span className="font-mono" title={release.sha ?? undefined}>
            SHA {shortSha(release.sha)}
          </span>
          {release.branch && <span>分支 {release.branch}</span>}
          {alembic && (
            <span className="font-mono" title={alembic}>
              迁移 {alembic.slice(0, 12)}
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
