"use client";

import { type RefObject } from "react";
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
import {
  MetricCard,
  Select,
} from "@/components/ui/primitives";
import { EmptyBlock } from "../../_components/AdminFeedback";
import {
  WEIGHT_SEGMENT_CLASSES,
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
      <MetricCard
        label="供应商"
        value={total}
        icon={sourceIcon}
        sub={sourceLabel}
      />
      <MetricCard
        label="已启用"
        value={enabled}
        sub={
          enabled < total ? (
            <span>
              {total - enabled} 已禁用
            </span>
          ) : (
            <span className="text-success">全部启用</span>
          )
        }
        className={enabled === total ? "border-success-border" : undefined}
      />
      <MetricCard
        label="探活"
        value={
          probing ? (
            <Loader2 className="h-4 w-4 animate-spin text-accent" />
          ) : healthy !== null ? (
            `${healthy}/${enabled}`
          ) : (
            "—"
          )
        }
        sub={
          probedAt ? (
            <span>{relativeTime(probedAt)}</span>
          ) : (
            <span>未探测</span>
          )
        }
        className={
          healthy === enabled
            ? "border-success-border"
            : healthy === 0
              ? "border-danger-border"
              : undefined
        }
      />
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
    <div className="surface-card p-4">
      <div className="type-caption mb-2.5">
        流量分配
        {items.some((p) => p.enabled && p.priority < maxPriority) && (
          <span className="ml-1.5 text-[var(--fg-2)]">
            （优先级 {maxPriority} 活跃组）
          </span>
        )}
      </div>
      <div className="flex h-3 gap-px overflow-hidden rounded-[var(--radius-card)]">
        {topGroup.map((p, i) => {
          const pct = (p.weight / totalWeight) * 100;
          return (
            <motion.div
              key={p.name}
              initial={{ width: 0 }}
              animate={{ width: `${pct}%` }}
              transition={{ duration: 0.5, delay: i * 0.08, ease: "easeOut" }}
              className={`h-full rounded-[var(--radius-control)] opacity-80 ${
                WEIGHT_SEGMENT_CLASSES[i % WEIGHT_SEGMENT_CLASSES.length]
              }`}
              title={`${p.name}: ${Math.round(pct)}%`}
            />
          );
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
        {topGroup.map((p, i) => {
          const pct = Math.round((p.weight / totalWeight) * 100);
          return (
            <span key={p.name} className="inline-flex items-center gap-1.5 type-caption">
              <span
                className={`h-2 w-2 shrink-0 rounded-full ${
                  WEIGHT_SEGMENT_CLASSES[i % WEIGHT_SEGMENT_CLASSES.length]
                }`}
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
    <div className="surface-card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <Activity className="w-4 h-4 text-[var(--fg-1)]" />
          <div>
          <div className="type-body-sm font-medium text-[var(--fg-0)]">
              自动探活
            </div>
            <div className="mt-0.5 type-caption text-[var(--fg-2)]">
              {isOff
                ? "已关闭，仅手动探活"
                : `每 ${interval >= 60 ? `${interval / 60} 分钟` : `${interval} 秒`}自动检测`}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {saving && <Loader2 className="w-3 h-3 animate-spin text-[var(--fg-2)]" />}
          <Select
            value={interval}
            onChange={(e) => onChangeInterval(Number(e.target.value))}
            disabled={saving}
            className="w-auto min-w-28 sm:h-9"
          >
            {PROBE_INTERVAL_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </Select>
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
    <div className="surface-card p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="type-caption">
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
              <div className="flex items-center justify-between type-body-sm">
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
                          ? "text-[var(--accent)]"
                          : "text-danger"
                    }`}
                  >
                    成功 {Math.round(rate)}%
                  </span>
                </div>
              </div>
              <div className="flex rounded-[var(--radius-control)] overflow-hidden h-1.5 bg-[var(--bg-2)]">
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
          <span className="type-caption whitespace-nowrap font-medium">
            优先级 {group.priority}
            {group.label && (
              <span className="ml-1.5 text-[var(--fg-2)]">
                ({group.label})
              </span>
            )}
          </span>
          <div className="h-px flex-1 bg-[var(--border-subtle)]" />
          <span className="type-caption text-[var(--fg-2)] tabular-nums">
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
