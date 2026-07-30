"use client";

import {
  Loader2,
  RotateCcw,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import { getAdminContextHealth } from "@/lib/apiClient";
import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { cn } from "@/lib/utils";
import { formatCircuitState } from "./model";

export function ContextHealthBlock({
  data,
  loading,
  error,
  onRetry,
}: {
  data: Awaited<ReturnType<typeof getAdminContextHealth>> | undefined;
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
}) {
  const last24h = data?.last_24h;
  const successRate =
    last24h?.summary_success_rate == null
      ? null
      : `${Math.round(last24h.summary_success_rate * 1000) / 10}%`;
  const state = formatCircuitState(
    data?.circuit_breaker_state ??
      (data as { state?: string } | undefined)?.state,
  );

  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)] backdrop-blur-sm">
      <ContextHealthHeader
        loading={loading}
        error={error}
        state={state}
        onRetry={onRetry}
      />
      <ContextHealthBody
        data={data}
        last24h={last24h}
        successRate={successRate}
        error={error}
      />
      {data?.circuit_breaker_until && (
        <p className="mt-3 type-caption text-warning">
          自动摘要预计恢复时间：{data.circuit_breaker_until}
        </p>
      )}
    </div>
  );
}

type ContextHealthData = Awaited<ReturnType<typeof getAdminContextHealth>>;

function ContextHealthHeader({
  loading,
  error,
  state,
  onRetry,
}: {
  loading: boolean;
  error: Error | null;
  state: { label: string; tone: "success" | "warning" | "danger" };
  onRetry: () => void;
}) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex min-w-0 gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-info-border bg-info-soft">
          <ShieldCheck className="h-4 w-4 text-info" />
        </div>
        <div className="min-w-0">
          <h3 className="type-card-title text-sm">长对话摘要状态</h3>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            用来判断自动摘要是否稳定。这里是只读状态，不需要手动保存。
          </p>
        </div>
      </div>
      {loading ? (
        <span className="inline-flex items-center gap-1.5 text-xs text-[var(--fg-1)]">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> 读取中
        </span>
      ) : error ? (
        <Button
          variant="secondary"
          size="sm"
          onClick={onRetry}
          leftIcon={<RotateCcw className="h-3 w-3" />}
        >
          {copy.action.retry}
        </Button>
      ) : (
        <span
          className={cn(
            "inline-flex items-center rounded-[var(--radius-control)] border px-2 py-0.5 text-xs",
            state.tone === "danger"
              ? "border-danger-border bg-danger-soft text-danger"
              : state.tone === "warning"
                ? "border-warning-border bg-warning-soft text-warning"
                : "border-success-border bg-success-soft text-success",
          )}
        >
          {state.label}
        </span>
      )}
    </div>
  );
}

function ContextHealthBody({
  data,
  last24h,
  successRate,
  error,
}: {
  data: ContextHealthData | undefined;
  last24h: ContextHealthData["last_24h"] | undefined;
  successRate: string | null;
  error: Error | null;
}) {
  if (error) {
    return (
      <p role="alert" className="mt-3 type-caption text-[var(--fg-2)]">
        暂时读不到摘要状态：{error.message}
      </p>
    );
  }
  if (!data) return null;
  return (
    <div className="mt-4 grid grid-cols-2 gap-2 md:grid-cols-4">
      <HealthMetric label="摘要成功率" value={successRate ?? "暂无数据"} />
      <HealthMetric
        label="自动摘要次数"
        value={String(last24h?.summary_attempts ?? (data as { total?: number }).total ?? 0)}
      />
      <HealthMetric
        label="P95 响应时间"
        value={
          last24h?.summary_p95_latency_ms == null
            ? "暂无数据"
            : `${last24h.summary_p95_latency_ms}ms`
        }
      />
      <HealthMetric
        label="手动压缩次数"
        value={String(last24h?.manual_compact_calls ?? 0)}
      />
    </div>
  );
}

export function OverviewMetric({
  icon: Icon,
  label,
  value,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-2.5">
      <div className="flex items-center gap-2 text-[11px] text-[var(--fg-2)]">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <p className="mt-1 truncate text-sm font-medium text-[var(--fg-0)]">
        {value}
      </p>
    </div>
  );
}

export function HealthMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-2">
      <p className="text-[11px] text-[var(--fg-2)]">{label}</p>
      <p className="mt-1 font-mono text-sm text-[var(--fg-0)]">{value}</p>
    </div>
  );
}

export function DependencyNotice({
  icon: Icon,
  title,
  body,
}: {
  icon: LucideIcon;
  title: string;
  body: string;
}) {
  return (
    <div className="flex items-start gap-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 py-3 text-sm text-[var(--fg-1)]">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]">
        <Icon className="h-4 w-4 text-[var(--fg-2)]" />
      </div>
      <div>
        <p className="font-medium text-[var(--fg-0)]">{title}</p>
        <p className="mt-1 type-caption text-[var(--fg-2)]">{body}</p>
      </div>
    </div>
  );
}

export function SourceBadge({
  hasDbOverride,
  hasAnyValue,
}: {
  hasDbOverride: boolean;
  hasAnyValue: boolean;
}) {
  if (hasDbOverride) {
    return (
      <span className="rounded-[var(--radius-control)] border border-accent-border bg-accent-soft px-2 py-0.5 text-[11px] text-accent">
        已覆盖默认
      </span>
    );
  }
  if (hasAnyValue) {
    return (
      <span className="rounded-[var(--radius-control)] border border-info-border bg-info-soft px-2 py-0.5 text-[11px] text-info">
        使用环境变量
      </span>
    );
  }
  return (
    <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 text-[11px] text-[var(--fg-2)]">
      使用程序默认
    </span>
  );
}
