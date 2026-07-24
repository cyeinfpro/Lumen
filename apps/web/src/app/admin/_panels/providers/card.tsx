"use client";

import { motion } from "framer-motion";
import {
  Activity,
  Check,
  ImageIcon,
  Loader2,
  Power,
  PowerOff,
} from "lucide-react";
import type {
  ProviderItemOut,
  ProviderProbeResult,
  ProviderPurpose,
  ProviderStatsItem,
} from "@/lib/types";
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
        "group rounded-[var(--radius-dialog)] border p-5 backdrop-blur-sm transition-colors " +
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
          {!provider.enabled && (
            <span className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--fg-2)]/10 px-1.5 py-0.5 text-[10px] text-[var(--fg-2)]">
              <PowerOff className="h-2.5 w-2.5" /> 已禁用
            </span>
          )}
          {provider.image_jobs_enabled && (
            <span className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-info-border bg-info-soft px-1.5 py-0.5 text-[10px] text-info">
              <ImageIcon className="h-2.5 w-2.5" /> 异步生图
            </span>
          )}
        </div>
        <code className="mt-1 block break-all text-xs text-[var(--fg-2)]">
          {provider.base_url}
        </code>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <button
          type="button"
          onClick={() => onToggleEnabled(provider.name, !provider.enabled)}
          disabled={quickSaving}
          className={
            "inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-control)] border transition-colors max-sm:min-h-11 max-sm:min-w-11 " +
            (provider.enabled
              ? "border-success-border bg-success-soft text-success hover:bg-success/20"
              : "border-[var(--border-strong)] bg-[var(--bg-3)] text-[var(--fg-2)] hover:bg-[var(--bg-3)]")
          }
          aria-label={provider.enabled ? "停用供应商" : "启用供应商"}
          title={provider.enabled ? "停用供应商" : "启用供应商"}
        >
          {provider.enabled ? (
            <Power className="h-3 w-3" />
          ) : (
            <PowerOff className="h-3 w-3" />
          )}
        </button>
        <button
          type="button"
          onClick={() => onProbeSingle(provider.name)}
          disabled={probing || !provider.enabled}
          className="inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)] opacity-0 transition-all hover:bg-[var(--bg-3)] focus:opacity-100 group-hover:opacity-100 disabled:opacity-30 max-sm:min-h-11 max-sm:min-w-11"
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
              "inline-flex items-center gap-1.5 rounded-[var(--radius-card)] border px-2 py-1 text-[11px] transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
              (checked
                ? "border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/10 text-[var(--color-lumen-amber)]"
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
                  ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)] text-black"
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
        ? "text-[var(--color-lumen-amber)]"
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
        ? "text-[var(--color-lumen-amber)]"
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
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-[var(--radius-control)] border border-[var(--color-lumen-amber)]/30 bg-[var(--color-lumen-amber)]/10 px-2 py-0.5 text-xs text-[var(--color-lumen-amber)]">
        <Loader2 className="h-3 w-3 animate-spin" />
      </span>
    );
  }
  if (!probe) {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--fg-2)]/10 px-2 py-0.5 text-xs text-[var(--fg-2)]">
        <span className="h-1.5 w-1.5 rounded-full bg-[var(--fg-2)]" />
        未探测
      </span>
    );
  }
  if (probe.status === "disabled") {
    return (
      <span className="inline-flex shrink-0 items-center gap-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--fg-2)]/10 px-2 py-0.5 text-xs text-[var(--fg-2)]">
        <PowerOff className="h-3 w-3" /> 跳过
      </span>
    );
  }
  if (probe.ok) {
    return (
      <span className="inline-flex shrink-0 items-center gap-1.5 rounded-[var(--radius-control)] border border-success-border bg-success-soft px-2 py-0.5 text-xs text-success">
        <span className="h-1.5 w-1.5 rounded-full bg-success shadow-[var(--shadow-2)]" />
        健康
        {probe.latency_ms != null && (
          <span className="tabular-nums text-success/80">
            {probe.latency_ms}ms
          </span>
        )}
      </span>
    );
  }
  return (
    <span
      role="alert"
      className="inline-flex max-w-[260px] shrink-0 items-center gap-1.5 truncate rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-2 py-0.5 text-xs text-danger"
      title={probe.error ?? undefined}
    >
      <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-danger shadow-[var(--shadow-2)]" />
      异常
      {probe.error ? (
        <span role="alert" className="truncate text-danger/85">
          {probe.error}
        </span>
      ) : null}
    </span>
  );
}
