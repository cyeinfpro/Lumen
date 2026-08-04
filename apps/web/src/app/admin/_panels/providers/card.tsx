"use client";

import { motion } from "framer-motion";
import {
  Activity,
  Check,
  ImageIcon,
  Loader2,
  PowerOff,
} from "lucide-react";
import type {
  ProviderItemOut,
  ProviderProbeResult,
  ProviderPurpose,
  ProviderStatsItem,
} from "@/lib/types";
import { StatusBadge, Switch } from "@/components/ui/primitives";
import {
  PROVIDER_PURPOSES,
  editTransportDisplayLabel,
  endpointDisplayLabel,
  normalizePurposes,
} from "./model";

type ProviderCardProps = {
  provider: ProviderItemOut;
  index: number;
  probe?: ProviderProbeResult;
  stats?: ProviderStatsItem;
  probing: boolean;
  onProbeSingle: (name: string) => void;
  onToggleEnabled: (name: string, enabled: boolean) => void;
  onSavePurposes: (name: string, purposes: ProviderPurpose[]) => void;
  quickSaving: boolean;
};

export function ProviderCard({
  provider,
  index,
  probe,
  stats,
  probing,
  onProbeSingle,
  onToggleEnabled,
  onSavePurposes,
  quickSaving,
}: ProviderCardProps) {
  const purposes = normalizePurposes(provider.purposes);

  const togglePurpose = (purpose: ProviderPurpose) => {
    const next = purposes.includes(purpose)
      ? purposes.filter((item) => item !== purpose)
      : [...purposes, purpose];
    if (next.length === 0) return;
    onSavePurposes(provider.name, next);
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, delay: Math.min(index * 0.04, 0.2) }}
      className={
        "surface-card group p-5 transition-colors " +
        (provider.enabled
          ? "border-[var(--border)] bg-[var(--bg-1)]/60 hover:border-[var(--border)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)]/30")
      }
    >
      <ProviderCardHeader
        provider={provider}
        probing={probing}
        quickSaving={quickSaving}
        onProbeSingle={onProbeSingle}
        onToggleEnabled={onToggleEnabled}
        probe={probe}
      />
      <ProviderPurposeSelector
        purposes={purposes}
        quickSaving={quickSaving}
        onToggle={togglePurpose}
      />
      <ProviderMetadata provider={provider} probe={probe} stats={stats} />
    </motion.div>
  );
}

function ProviderCardHeader({
  provider,
  probing,
  quickSaving,
  onProbeSingle,
  onToggleEnabled,
  probe,
}: {
  provider: ProviderItemOut;
  probing: boolean;
  quickSaving: boolean;
  onProbeSingle: (name: string) => void;
  onToggleEnabled: (name: string, enabled: boolean) => void;
  probe?: ProviderProbeResult;
}) {
  return (
    <div className="mb-3 flex items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={
              "text-sm font-medium " +
              (provider.enabled
                ? "text-[var(--fg-0)]"
                : "text-[var(--fg-1)]")
            }
          >
            {provider.name}
          </span>
          {!provider.enabled && <StatusBadge status="disabled" />}
          {provider.image_jobs_enabled && (
            <StatusBadge
              status="unknown"
              tone="info"
              label={
                <>
                  <ImageIcon className="h-3 w-3" /> 异步生图
                </>
              }
            />
          )}
        </div>
        <code className="mt-1 block break-all text-xs text-[var(--fg-2)]">
          {provider.base_url}
        </code>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Switch
          checked={provider.enabled}
          onCheckedChange={(checked) => onToggleEnabled(provider.name, checked)}
          disabled={quickSaving}
          aria-label={provider.enabled ? "停用供应商" : "启用供应商"}
          title={provider.enabled ? "停用供应商" : "启用供应商"}
        />
        <button
          type="button"
          onClick={() => onProbeSingle(provider.name)}
          disabled={probing || !provider.enabled}
          className="inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-30 max-sm:min-h-11 max-sm:min-w-11"
          aria-label="探活此供应商"
          title="探活此供应商"
        >
          <Activity className="h-3 w-3" />
        </button>
        <ProbeStatusBadge probe={probe} probing={probing} />
      </div>
    </div>
  );
}

function ProviderPurposeSelector({
  purposes,
  quickSaving,
  onToggle,
}: {
  purposes: ProviderPurpose[];
  quickSaving: boolean;
  onToggle: (purpose: ProviderPurpose) => void;
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-1.5">
      {PROVIDER_PURPOSES.map((option) => {
        const checked = purposes.includes(option.value);
        const disabled = quickSaving || (checked && purposes.length === 1);
        return (
          <button
            key={option.value}
            type="button"
            onClick={() => onToggle(option.value)}
            disabled={disabled}
            className={
              "inline-flex items-center gap-1.5 rounded-[var(--radius-control)] border px-2 py-1 type-caption transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
              (checked
                ? "border-accent-border bg-accent-soft text-accent"
                : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)] hover:text-[var(--fg-1)]")
            }
            title={
              disabled && checked
                ? "至少保留一个用途"
                : `切换 ${option.label} 用途`
            }
          >
            <span
              className={
                "flex h-3 w-3 items-center justify-center rounded border " +
                (checked
                  ? "border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-on)]"
                  : "border-[var(--border-strong)]")
              }
              aria-hidden
            >
              {checked ? <Check className="h-2.5 w-2.5" /> : null}
            </span>
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function ProviderMetadata({
  provider,
  probe,
  stats,
}: {
  provider: ProviderItemOut;
  probe?: ProviderProbeResult;
  stats?: ProviderStatsItem;
}) {
  return (
    <div
      className={
        "flex flex-wrap items-center gap-x-3 gap-y-1 text-xs " +
        (provider.enabled
          ? "text-[var(--fg-1)]"
          : "text-[var(--fg-2)]")
      }
    >
      <MetaItem
        label="密钥"
        value={provider.api_key_hint || "未保存"}
        mono
        color={provider.api_key_hint ? undefined : "text-danger"}
      />
      <MetaSep />
      <MetaItem label="优先级" value={String(provider.priority)} mono />
      <MetaSep />
      <MetaItem label="权重" value={String(provider.weight)} mono />
      <MetaSep />
      <MetaItem
        label="并发"
        value={String(Math.max(1, provider.image_concurrency ?? 1))}
        mono
      />
      <MetaSep />
      <MetaItem label="代理" value={provider.proxy ?? "直连"} mono />
      <ProviderImageJobMetadata provider={provider} />
      <ProviderProbeMetadata probe={probe} />
      <ProviderStatsMetadata stats={stats} />
    </div>
  );
}

function ProviderImageJobMetadata({
  provider,
}: {
  provider: ProviderItemOut;
}) {
  const endpoint = provider.image_jobs_endpoint ?? "auto";
  if (endpoint === "auto" && !provider.image_jobs_enabled) return null;
  const locked =
    provider.image_jobs_endpoint_lock && endpoint !== "auto";
  return (
    <>
      <MetaSep />
      <MetaItem
        label="接口"
        value={
          locked
            ? `${endpointDisplayLabel(endpoint)} · 已锁定`
            : endpointDisplayLabel(endpoint)
        }
        mono
        color={locked ? "text-warning" : "text-info"}
      />
      {provider.image_jobs_base_url && (
        <>
          <MetaSep />
          <MetaItem
            label="旁路地址"
            value={provider.image_jobs_base_url}
            mono
            color="text-info"
          />
        </>
      )}
      {provider.image_jobs_enabled && (
        <>
          <MetaSep />
          <MetaItem
            label="编辑输入"
            value={editTransportDisplayLabel(
              provider.image_edit_input_transport,
            )}
            mono
            color={
              provider.image_edit_input_transport === "file"
                ? "text-warning"
                : "text-info"
            }
          />
        </>
      )}
    </>
  );
}

function ProviderProbeMetadata({
  probe,
}: {
  probe?: ProviderProbeResult;
}) {
  if (probe?.latency_ms == null) return null;
  const color =
    probe.latency_ms < 500
      ? "text-success"
      : probe.latency_ms < 2000
      ? "text-warning"
        : "text-danger";
  return (
    <>
      <MetaSep />
      <MetaItem
        label="延迟"
        value={`${probe.latency_ms}ms`}
        mono
        color={color}
      />
    </>
  );
}

function ProviderStatsMetadata({
  stats,
}: {
  stats?: ProviderStatsItem;
}) {
  if (!stats || stats.total <= 0) return null;
  const rateColor =
    stats.success_rate >= 0.95
      ? "text-success"
      : stats.success_rate >= 0.8
        ? "text-warning"
        : "text-danger";
  return (
    <>
      <MetaSep />
      <MetaItem label="请求" value={String(stats.total)} mono />
      <MetaSep />
      <MetaItem
        label="成功率"
        value={`${Math.round(stats.success_rate * 100)}%`}
        mono
        color={rateColor}
      />
      <MetaSep />
      <MetaItem
        label="流量"
        value={`${Math.round(stats.traffic_pct * 100)}%`}
        mono
      />
    </>
  );
}

function MetaItem({
  label,
  value,
  mono,
  color,
}: {
  label: string;
  value: string;
  mono?: boolean;
  color?: string;
}) {
  return (
    <span>
      {label}:{" "}
      <code
        className={`${mono ? "tabular-nums" : ""} ${
          color ?? "text-[var(--fg-1)]"
        }`}
      >
        {value}
      </code>
    </span>
  );
}

function MetaSep() {
  return <span className="text-[var(--fg-3)]">·</span>;
}

function ProbeStatusBadge({
  probe,
  probing,
}: {
  probe?: ProviderProbeResult;
  probing: boolean;
}) {
  if (probing) {
    return <StatusBadge status="probing" label={<><Loader2 className="h-3 w-3 animate-spin" /> 探活中</>} />;
  }
  if (!probe) {
    return <StatusBadge status="unknown" label="未探测" />;
  }
  if (probe.status === "disabled") {
    return <StatusBadge status="disabled" label={<><PowerOff className="h-3 w-3" /> 跳过</>} />;
  }
  if (probe.ok) {
    return (
      <StatusBadge
        status="ok"
        label={
          <>
            健康
            {probe.latency_ms != null && (
              <span className="tabular-nums"> {probe.latency_ms} ms</span>
            )}
          </>
        }
      />
    );
  }
  return (
    <StatusBadge
      role="alert"
      className="max-w-[260px] truncate"
      title={probe.error ?? undefined}
      status="error"
      label={
        <>
          异常
          {probe.error ? <span className="truncate"> {probe.error}</span> : null}
        </>
      }
    />
  );
}
