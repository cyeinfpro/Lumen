"use client";

import { type ReactNode, type RefObject } from "react";
import { motion } from "framer-motion";
import {
  Activity,
  Cloud,
  Loader2,
  Server,
} from "lucide-react";
import type {
  ProviderItemOut,
  ProviderPurpose,
  ProviderProbeResult,
  ProviderProxyOut,
  ProviderStatsItem,
} from "@/lib/types";
import { EmptyBlock } from "../../_components/AdminFeedback";
import {
  WEIGHT_COLORS,
  type Draft,
  type FieldErrors,
  type PriorityGroup,
  relativeTime,
} from "./model";
import { ProviderCard } from "./card";
import { DraftCard } from "./editor";

// ---------------------------------------------------------------------------
// 统计行
// ---------------------------------------------------------------------------

export function StatsRow({
  total,
  enabled,
  healthy,
  probing,
  probedAt,
  source,
}: {
  total: number;
  enabled: number;
  healthy: number | null;
  probing: boolean;
  probedAt: string | null;
  source: string;
}) {
  const sourceLabel =
    source === "db"
      ? "数据库"
      : source === "env"
        ? "环境变量"
        : "未配置";
  const sourceIcon =
    source === "db" ? (
      <Server className="w-3 h-3" />
    ) : (
      <Cloud className="w-3 h-3" />
    );

  return (
    <div className="grid grid-cols-3 gap-3">
      <StatCard
        label="供应商"
        value={total}
        sub={
          <span className="inline-flex items-center gap-1 text-[var(--fg-2)]">
            {sourceIcon} {sourceLabel}
          </span>
        }
      />
      <StatCard
        label="已启用"
        value={enabled}
        sub={
          enabled < total ? (
            <span className="text-[var(--fg-2)]">
              {total - enabled} 已禁用
            </span>
          ) : (
            <span className="text-success">全部启用</span>
          )
        }
        accent={enabled === total ? "green" : undefined}
      />
      <StatCard
        label="探活"
        value={
          probing ? (
            <Loader2 className="w-4 h-4 animate-spin text-[var(--color-lumen-amber)]" />
          ) : healthy !== null ? (
            `${healthy}/${enabled}`
          ) : (
            "—"
          )
        }
        sub={
          probedAt ? (
            <span className="text-[var(--fg-2)]">{relativeTime(probedAt)}</span>
          ) : (
            <span className="text-[var(--fg-2)]">未探测</span>
          )
        }
        accent={
          healthy !== null
            ? healthy === enabled
              ? "green"
              : healthy === 0
                ? "red"
                : "amber"
            : undefined
        }
      />
    </div>
  );
}

function StatCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  accent?: "green" | "red" | "amber";
}) {
  const ring =
    accent === "green"
      ? "border-success-border"
      : accent === "red"
        ? "border-danger-border"
        : accent === "amber"
          ? "border-[var(--color-lumen-amber)]/20"
          : "border-[var(--border)]";

  return (
    <div
      className={`rounded-[var(--radius-panel)] border bg-[var(--bg-1)]/60 backdrop-blur-sm px-4 py-3 ${ring}`}
    >
      <div className="text-[10px] uppercase tracking-wider text-[var(--fg-2)] mb-1">
        {label}
      </div>
      <div className="text-lg font-semibold text-[var(--fg-0)] tabular-nums leading-tight">
        {value}
      </div>
      {sub && <div className="text-[11px] mt-1">{sub}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 流量分配可视化
// ---------------------------------------------------------------------------

export function WeightBar({ items }: { items: ProviderItemOut[] }) {
  const enabled = items.filter((p) => p.enabled);
  if (enabled.length < 2) return null;

  // 取最高优先级组
  const maxPriority = Math.max(...enabled.map((p) => p.priority));
  const topGroup = enabled.filter((p) => p.priority === maxPriority);
  if (topGroup.length < 2) return null;

  const totalWeight = topGroup.reduce((s, p) => s + p.weight, 0);

  return (
    <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-panel)] p-4">
      <div className="text-[10px] uppercase tracking-wider text-[var(--fg-2)] mb-2.5">
        流量分配
        {items.some((p) => p.enabled && p.priority < maxPriority) && (
          <span className="normal-case tracking-normal ml-1.5 text-[var(--fg-2)]">
            (Priority {maxPriority} 活跃组)
          </span>
        )}
      </div>
      <div className="flex rounded-[var(--radius-card)] overflow-hidden h-3 gap-px">
        {topGroup.map((p, i) => {
          const pct = (p.weight / totalWeight) * 100;
          return (
            <motion.div
              key={p.name}
              initial={{ width: 0 }}
              animate={{ width: `${pct}%` }}
              transition={{ duration: 0.5, delay: i * 0.08, ease: "easeOut" }}
              className="h-full rounded-[var(--radius-control)]"
              style={{
                backgroundColor: WEIGHT_COLORS[i % WEIGHT_COLORS.length],
                opacity: 0.8,
              }}
              title={`${p.name}: ${Math.round(pct)}%`}
            />
          );
        })}
      </div>
      <div className="flex mt-2 gap-x-4 gap-y-1 flex-wrap">
        {topGroup.map((p, i) => {
          const pct = Math.round((p.weight / totalWeight) * 100);
          return (
            <span key={p.name} className="inline-flex items-center gap-1.5 text-xs">
              <span
                className="w-2 h-2 rounded-[var(--radius-control)] shrink-0"
                style={{
                  backgroundColor: WEIGHT_COLORS[i % WEIGHT_COLORS.length],
                }}
              />
              <span className="text-[var(--fg-1)]">{p.name}</span>
              <span className="text-[var(--fg-2)] tabular-nums">{pct}%</span>
              <span className="text-[var(--fg-2)] tabular-nums">(w={p.weight})</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 自动探活设置
// ---------------------------------------------------------------------------

const PROBE_INTERVAL_OPTIONS = [
  { label: "关闭", value: 0 },
  { label: "30s", value: 30 },
  { label: "1 分钟", value: 60 },
  { label: "2 分钟", value: 120 },
  { label: "5 分钟", value: 300 },
  { label: "10 分钟", value: 600 },
];

export function AutoProbeSettings({
  interval,
  onChangeInterval,
  saving,
}: {
  interval: number;
  onChangeInterval: (v: number) => void;
  saving: boolean;
}) {
  const isOff = interval <= 0;
  return (
    <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-panel)] p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <Activity className="w-4 h-4 text-[var(--fg-1)]" />
          <div>
            <div className="text-xs font-medium text-[var(--fg-0)]">
              自动探活
            </div>
            <div className="text-[11px] text-[var(--fg-2)] mt-0.5">
              {isOff
                ? "已关闭，仅手动探活"
                : `每 ${interval >= 60 ? `${interval / 60} 分钟` : `${interval} 秒`}自动检测`}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {saving && <Loader2 className="w-3 h-3 animate-spin text-[var(--fg-2)]" />}
          <select
            value={interval}
            onChange={(e) => onChangeInterval(Number(e.target.value))}
            disabled={saving}
            className="min-h-[36px] sm:h-8 px-2.5 pr-7 rounded-[var(--radius-card)] bg-[var(--bg-0)]/70 border border-[var(--border)] text-xs text-[var(--fg-0)] focus:outline-none focus:border-[var(--color-lumen-amber)]/50 disabled:opacity-50 transition-colors appearance-none cursor-pointer"
            style={{
              backgroundImage: `url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23666' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E")`,
              backgroundRepeat: "no-repeat",
              backgroundPosition: "right 8px center",
            }}
          >
            {PROBE_INTERVAL_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 请求统计面板
// ---------------------------------------------------------------------------

export function RequestStatsPanel({ items }: { items: ProviderStatsItem[] }) {
  const grandTotal = items.reduce((s, i) => s + i.total, 0);
  if (grandTotal === 0) return null;

  return (
    <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-[var(--border)] rounded-[var(--radius-panel)] p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="text-[10px] uppercase tracking-wider text-[var(--fg-2)]">
          请求统计
        </div>
        <span className="text-[11px] text-[var(--fg-2)] tabular-nums">
          总计 {grandTotal.toLocaleString()} 次请求
        </span>
      </div>
      <div className="space-y-2.5">
        {items.map((s) => {
          const pct = grandTotal > 0 ? (s.total / grandTotal) * 100 : 0;
          const rate = s.total > 0 ? s.success_rate * 100 : 0;
          return (
            <div key={s.name} className="space-y-1.5">
              <div className="flex items-center justify-between text-xs">
                <span className="text-[var(--fg-1)] font-medium">{s.name}</span>
                <div className="flex items-center gap-3 text-[var(--fg-1)]">
                  <span className="tabular-nums">
                    {s.total.toLocaleString()} 次
                  </span>
                  <span className="tabular-nums">
                    流量 {Math.round(pct)}%
                  </span>
                  <span
                    className={`tabular-nums ${
                      rate >= 95
                        ? "text-success"
                        : rate >= 80
                          ? "text-[var(--color-lumen-amber)]"
                          : "text-danger"
                    }`}
                  >
                    成功 {Math.round(rate)}%
                  </span>
                </div>
              </div>
              <div className="flex rounded-[var(--radius-control)] overflow-hidden h-1.5 bg-white/5">
                {s.success > 0 && (
                  <div
                    className="h-full bg-success/70"
                    style={{ width: `${(s.success / s.total) * 100}%` }}
                  />
                )}
                {s.fail > 0 && (
                  <div
                    className="h-full bg-danger/70"
                    style={{ width: `${(s.fail / s.total) * 100}%` }}
                  />
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 优先级分组 + 只读卡片
// ---------------------------------------------------------------------------

export function PriorityGroupView({
  group,
  probeMap,
  statsMap,
  probing,
  totalGroups,
  onProbeSingle,
  onToggleEnabled,
  onSavePurposes,
  quickSaving,
}: {
  group: PriorityGroup;
  probeMap: Map<string, ProviderProbeResult>;
  statsMap: Map<string, ProviderStatsItem>;
  probing: boolean;
  totalGroups: number;
  onProbeSingle: (name: string) => void;
  onToggleEnabled: (name: string, enabled: boolean) => void;
  onSavePurposes: (name: string, purposes: ProviderPurpose[]) => void;
  quickSaving: boolean;
}) {
  return (
    <div className="space-y-3">
      {totalGroups > 1 && (
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wider text-[var(--fg-1)] font-medium whitespace-nowrap">
            Priority {group.priority}
            {group.label && (
              <span className="ml-1.5 text-[var(--fg-2)] normal-case tracking-normal">
                ({group.label})
              </span>
            )}
          </span>
          <div className="flex-1 h-px bg-white/8" />
          <span className="text-[10px] text-[var(--fg-2)] tabular-nums">
            {group.items.length} 个供应商
          </span>
        </div>
      )}
      {group.items.map((p, i) => (
        <ProviderCard
          key={p.name}
          provider={p}
          index={i}
          probe={probeMap.get(p.name)}
          stats={statsMap.get(p.name)}
          probing={probing}
          onProbeSingle={onProbeSingle}
          onToggleEnabled={onToggleEnabled}
          onSavePurposes={onSavePurposes}
          quickSaving={quickSaving}
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 编辑态
// ---------------------------------------------------------------------------

export function DraftList({
  drafts,
  proxies,
  editingIdx,
  deleteConfirmIdx,
  fieldErrors,
  serverKeyHints,
  newCardRef,
  onEdit,
  onUpdate,
  onRemove,
  onMove,
  onDeleteConfirm,
}: {
  drafts: Draft[];
  proxies: ProviderProxyOut[];
  editingIdx: number | null;
  deleteConfirmIdx: number | null;
  fieldErrors: Record<number, FieldErrors>;
  serverKeyHints: Map<string, string>;
  newCardRef: RefObject<HTMLDivElement | null>;
  onEdit: (idx: number | null) => void;
  onUpdate: (idx: number, patch: Partial<Draft>) => void;
  onRemove: (idx: number) => void;
  onMove: (idx: number, dir: -1 | 1) => void;
  onDeleteConfirm: (idx: number | null) => void;
}) {
  if (drafts.length === 0) {
    return (
      <EmptyBlock
        title="暂无供应商"
        description="点击底部「添加」新增一个上游供应商"
      />
    );
  }

  return (
    <div className="space-y-3">
      {drafts.map((d, i) => (
        <DraftCard
          key={d._key}
          ref={i === drafts.length - 1 ? newCardRef : undefined}
          draft={d}
          proxies={proxies}
          index={i}
          total={drafts.length}
          expanded={editingIdx === i}
          showDeleteConfirm={deleteConfirmIdx === i}
          errors={fieldErrors[i]}
          isExisting={serverKeyHints.has(d.name.trim())}
          hasExistingKey={Boolean(serverKeyHints.get(d.name.trim())?.trim())}
          onToggle={() => onEdit(editingIdx === i ? null : i)}
          onUpdate={(patch) => onUpdate(i, patch)}
          onRemove={() => onRemove(i)}
          onMove={(dir) => onMove(i, dir)}
          onDeleteConfirm={(show) => onDeleteConfirm(show ? i : null)}
        />
      ))}
    </div>
  );
}
