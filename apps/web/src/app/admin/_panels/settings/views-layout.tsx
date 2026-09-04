"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import {
  Activity,
  AlertCircle,
  Bot,
  BrainCircuit,
  ChevronDown,
  ChevronRight,
  Database,
  ImageIcon,
  Info,
  SlidersHorizontal,
  type LucideIcon,
} from "lucide-react";
import type { SystemSettingItem } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/primitives";
import { SettingDetails } from "../../_components/SettingDetails";
import { SettingControl } from "./views-controls";
import {
  DependencyNotice,
  OverviewMetric,
  SourceBadge,
} from "./views-health";
import {
  GROUPS,
  GROUP_NAV_SECTIONS,
  type DependencyState,
  type FilterId,
  type ModelsQueryState,
  type Op,
  type ProviderStatus,
  type SettingGroupId,
  type SettingMeta,
  type UpdateProxyOption,
  currentDisplayValue,
  formatPlainNumber,
  formatValue,
  getSettingMeta,
} from "./model";

export function SettingsOverviewCard({
  overview,
  dirtyCount,
  visibleCount,
}: {
  overview: {
    defaultModelLabel: string;
    engineLabel: string;
    channelLabel: string;
    formatLabel: string;
    compressionLabel: string;
  };
  dirtyCount: number;
  visibleCount: number;
}) {
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/70 p-4 shadow-[var(--shadow-2)] backdrop-blur-sm md:p-5">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-accent-border bg-accent-soft">
            <SlidersHorizontal className="h-4 w-4 text-accent" />
          </div>
          <div className="min-w-0">
            <h2 className="type-card-title">系统配置概览</h2>
            <p className="mt-1 max-w-3xl type-body-sm text-[var(--fg-2)]">
              常用开关在这里先给出结果，下面再按任务分区编辑。数据库设置优先生效，保存后通常几秒内同步到 API 和 Worker。
            </p>
          </div>
        </div>
        <div className="inline-flex w-fit items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-3 py-1.5 type-caption text-[var(--fg-1)]">
          <Database className="h-3.5 w-3.5 text-[var(--fg-2)]" />
          {dirtyCount > 0 ? `${dirtyCount} 项待保存` : `${visibleCount} 项可配置`}
        </div>
      </div>

      <div className="mt-5 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
        <OverviewMetric
          icon={Bot}
          label="默认模型"
          value={overview.defaultModelLabel}
        />
        <OverviewMetric
          icon={ImageIcon}
          label="生图引擎"
          value={overview.engineLabel}
        />
        <OverviewMetric
          icon={Activity}
          label="异步通道"
          value={overview.channelLabel}
        />
        <OverviewMetric
          icon={ImageIcon}
          label="输出格式"
          value={overview.formatLabel}
        />
        <OverviewMetric
          icon={BrainCircuit}
          label="自动压缩"
          value={overview.compressionLabel}
        />
      </div>
    </div>
  );
}

export function SettingsSectionHeader({
  icon: Icon,
  title,
  description,
  badge,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  badge?: string;
}) {
  return (
    <div className="flex flex-col gap-3 border-b border-[var(--border-subtle)] pb-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex min-w-0 items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]">
          <Icon className="h-4 w-4 text-[var(--fg-1)]" />
        </div>
        <div className="min-w-0">
          <h3 className="type-card-title">{title}</h3>
          <p className="mt-1 type-body-sm text-[var(--fg-2)]">{description}</p>
        </div>
      </div>
      {badge && (
        <span className="w-fit rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2.5 py-1 type-caption text-[var(--fg-1)]">
          {badge}
        </span>
      )}
    </div>
  );
}

export function SettingsGroupNav({
  activeGroup,
  totalCount,
  groupCounts,
  onChange,
}: {
  activeGroup: FilterId;
  totalCount: number;
  groupCounts: Record<SettingGroupId, number>;
  onChange: (group: FilterId) => void;
}) {
  return (
    <div className="space-y-3" aria-label="系统设置分类">
      {GROUP_NAV_SECTIONS.map((section) => {
        const groupsInSection = section.ids
          .map((id) => GROUPS.find((group) => group.id === id))
          .filter((group): group is (typeof GROUPS)[number] => {
            if (!group) return false;
            const count =
              group.id === "all" ? totalCount : groupCounts[group.id] ?? 0;
            return group.id === "all" || count > 0;
          });
        if (groupsInSection.length === 0) return null;
        return (
          <div key={section.label}>
            <p className="mb-1.5 px-2 type-overline text-[var(--fg-3)]">
              {section.label}
            </p>
            <div className="space-y-1">
              {groupsInSection.map((group) => {
                const count =
                  group.id === "all" ? totalCount : groupCounts[group.id] ?? 0;
                const active = activeGroup === group.id;
                const Icon = group.icon;
                return (
                  <button
                    key={group.id}
                    type="button"
                    onClick={() => onChange(group.id)}
                    className={cn(
                      "flex min-h-[40px] w-full cursor-pointer items-center gap-2 rounded-[var(--radius-control)] border px-2.5 py-1.5 text-left transition-colors",
                      active
                        ? "border-accent-border bg-accent-soft text-[var(--fg-0)]"
                        : "border-transparent text-[var(--fg-1)] hover:border-[var(--border)] hover:bg-[var(--bg-2)]",
                    )}
                    title={group.description}
                  >
                    <Icon
                      className={cn(
                        "h-3.5 w-3.5 shrink-0",
                        active ? "text-accent" : "text-[var(--fg-2)]",
                      )}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate type-caption text-current">
                        {group.label}
                      </span>
                      <span className="mt-0.5 hidden truncate type-caption leading-4 text-[var(--fg-2)] xl:block">
                        {group.description}
                      </span>
                    </span>
                    <span
                      className={cn(
                        "shrink-0 rounded-full border px-1.5 py-0.5 font-mono type-caption",
                        active
                          ? "border-accent-border bg-[var(--bg-0)]/35 text-accent"
                          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]",
                      )}
                    >
                      {count}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function SettingsGroup({
  group,
  ops,
  fieldErrors,
  dependencyState,
  modelsQuery,
  providerStatus,
  updateProxyOptions,
  onChange,
}: {
  group: { id: SettingGroupId; label: string; description: string; items: SystemSettingItem[] };
  ops: Record<string, Op>;
  fieldErrors: Record<string, string>;
  dependencyState: DependencyState;
  modelsQuery: ModelsQueryState;
  providerStatus: ProviderStatus;
  updateProxyOptions: UpdateProxyOption[];
  onChange: (key: string, op: Op | undefined) => void;
}) {
  const groupMeta = GROUPS.find((g) => g.id === group.id);
  const Icon = groupMeta?.icon ?? Database;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]">
            <Icon className="h-4 w-4 text-[var(--fg-1)]" />
          </div>
          <div className="min-w-0">
            <h3 className="type-card-title">
              {group.label}
            </h3>
            <p className="mt-0.5 type-caption text-[var(--fg-2)]">
              {group.description}
            </p>
          </div>
        </div>
        <span className="shrink-0 rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 font-mono type-caption text-[var(--fg-2)]">
          {group.items.length}
        </span>
      </div>

      <div className="grid gap-3">
        {group.id === "context_auto" && !dependencyState.compressionEnabled && (
          <DependencyNotice
            icon={BrainCircuit}
            title="先打开自动压缩"
            body="打开后再调整触发阈值、目标 token、模型和熔断参数。"
          />
        )}
        {group.items.map((item) => (
          <SettingCard
            key={item.key}
            item={item}
            op={ops[item.key]}
            fieldError={fieldErrors[item.key]}
            modelsQuery={modelsQuery}
            providerStatus={providerStatus}
            updateProxyOptions={updateProxyOptions}
            onChange={(op) => onChange(item.key, op)}
          />
        ))}
      </div>
    </div>
  );
}

export function SettingCard({
  item,
  op,
  fieldError,
  modelsQuery,
  providerStatus,
  updateProxyOptions,
  onChange,
}: {
  item: SystemSettingItem;
  op: Op | undefined;
  fieldError: string | undefined;
  modelsQuery: ModelsQueryState;
  providerStatus: ProviderStatus;
  updateProxyOptions: UpdateProxyOption[];
  onChange: (op: Op | undefined) => void;
}) {
  const meta = getSettingMeta(item.key, item.description);
  const Icon = meta.icon;
  const isDirty = !!op;
  const displayValue = currentDisplayValue(item, op, meta);
  const hasDbOverride = item.value != null && item.value !== "";
  const [showDetails, setShowDetails] = useState(false);

  return (
    <motion.article
      layout
      transition={{ duration: 0.18 }}
      className={cn(
        "rounded-[var(--radius-card)] border p-3 backdrop-blur-sm transition-colors md:p-4",
        isDirty
          ? "border-accent-border bg-accent-soft shadow-[var(--shadow-1)]"
          : "border-[var(--border)] bg-[var(--bg-1)]/60",
      )}
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-accent-border bg-accent-soft">
            <Icon className="h-4 w-4 text-accent" />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h4 className="type-body-sm font-medium text-[var(--fg-0)]">
                {meta.title}
              </h4>
              <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 type-caption text-[var(--fg-1)]">
                当前：{displayValue}
              </span>
              <SourceBadge
                hasDbOverride={hasDbOverride}
                hasAnyValue={item.has_value}
              />
            </div>
            <p className="mt-1 type-body-sm text-[var(--fg-2)]">
              {meta.summary}
            </p>
          </div>
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setShowDetails((value) => !value)}
          leftIcon={
            showDetails ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )
          }
          className="w-fit"
        >
          详情
        </Button>
      </div>

      <div className="mt-3">
        <SettingControl
          item={item}
          meta={meta}
          op={op}
          modelsQuery={modelsQuery}
          providerStatus={providerStatus}
          updateProxyOptions={updateProxyOptions}
          onChange={onChange}
        />
      </div>

      <SettingCardAnnotations
        item={item}
        meta={meta}
        op={op}
        fieldError={fieldError}
        showDetails={showDetails}
      />
    </motion.article>
  );
}

function SettingCardAnnotations({
  item,
  meta,
  op,
  fieldError,
  showDetails,
}: {
  item: SystemSettingItem;
  meta: SettingMeta;
  op: Op | undefined;
  fieldError: string | undefined;
  showDetails: boolean;
}) {
  return (
    <>
      {meta.warning && (
        <div className="mt-3 flex items-start gap-2 rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-3 py-2 type-caption leading-5 text-warning">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          {meta.warning}
        </div>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2 type-caption text-[var(--fg-2)]">
        {meta.recommended && (
          <span className="rounded-[var(--radius-control)] border border-success-border bg-success-soft px-2 py-1 text-success">
            {meta.recommended}
          </span>
        )}
        {(meta.min != null || meta.max != null) && (
          <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1">
            范围 {meta.min != null ? formatPlainNumber(meta.min) : "不限"}
            {" 到 "}
            {meta.max != null ? formatPlainNumber(meta.max) : "不限"}
            {meta.unit ?? ""}
          </span>
        )}
      </div>
      <SettingDetails
        open={showDetails}
        detail={meta.detail}
        settingKey={item.key}
        description={item.description}
        summary={meta.summary}
      />
      {fieldError && (
        <p className="mt-3 flex items-center gap-1.5 type-caption text-danger">
          <AlertCircle className="h-3.5 w-3.5" /> {fieldError}
        </p>
      )}
      {op?.kind === "set" && (
        <p className="mt-3 type-caption text-[var(--accent)]/90">
          保存后改为：{formatValue(op.value, meta)}
        </p>
      )}
      {op?.kind === "clear" && (
        <p className="mt-3 type-caption text-[var(--accent)]/90">
          保存后清除该项
        </p>
      )}
    </>
  );
}
