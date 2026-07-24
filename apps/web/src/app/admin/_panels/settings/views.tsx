"use client";

import { useMemo, useState, useSyncExternalStore } from "react";
import { motion } from "framer-motion";
import {
  Activity,
  AlertCircle,
  Bot,
  BrainCircuit,
  Check,
  ChevronDown,
  ChevronRight,
  Database,
  Globe,
  ImageIcon,
  Info,
  Loader2,
  RotateCcw,
  ShieldCheck,
  SlidersHorizontal,
  type LucideIcon,
} from "lucide-react";
import type { SystemSettingItem } from "@/lib/types";
import { getAdminContextHealth } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { SettingDetails } from "../../_components/SettingDetails";
import {
  GROUPS,
  GROUP_NAV_SECTIONS,
  IMAGE_CHANNEL_KEY,
  IMAGE_ENGINE_KEY,
  MODEL_LIBRARY_SYNC_PROXY_NAME_KEY,
  type DependencyState,
  type FilterId,
  type ModelsQueryState,
  type Op,
  type ProviderStatus,
  type SettingGroupId,
  type SettingMeta,
  type UpdateProxyOption,
  currentDisplayValue,
  formatCircuitState,
  formatPlainNumber,
  formatValue,
  getBrowserOrigin,
  getBrowserOriginSSR,
  getSettingMeta,
  normalizeImageChannel,
  normalizeImageEngine,
  settingInputClassName,
  settingMonoInputClassName,
  subscribeStatic,
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
            <p className="mb-1.5 px-2 text-[10px] font-medium uppercase tracking-[0.08em] text-[var(--fg-3)]">
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
                      <span className="mt-0.5 hidden truncate text-[11px] leading-4 text-[var(--fg-2)] xl:block">
                        {group.description}
                      </span>
                    </span>
                    <span
                      className={cn(
                        "shrink-0 rounded-full border px-1.5 py-0.5 font-mono text-[10px]",
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
        <span className="shrink-0 rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 font-mono text-[11px] text-[var(--fg-2)]">
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
              <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 text-[11px] text-[var(--fg-1)]">
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
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-[var(--fg-2)]">
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
        <p className="mt-3 text-xs text-[var(--color-lumen-amber)]/90">
          保存后改为：{formatValue(op.value, meta)}
        </p>
      )}
      {op?.kind === "clear" && (
        <p className="mt-3 text-xs text-[var(--color-lumen-amber)]/90">
          保存后清除该项
        </p>
      )}
    </>
  );
}

type SettingControlProps = {
  item: SystemSettingItem;
  meta: SettingMeta;
  op: Op | undefined;
  modelsQuery: ModelsQueryState;
  providerStatus: ProviderStatus;
  updateProxyOptions: UpdateProxyOption[];
  onChange: (op: Op | undefined) => void;
};

export function SettingControl({
  item,
  meta,
  op,
  modelsQuery,
  providerStatus,
  updateProxyOptions,
  onChange,
}: SettingControlProps) {
  const { controlValue, inputValue } = settingControlValues(item, meta, op);
  const showDefaultAction =
    item.value != null &&
    item.value !== "" &&
    meta.defaultValue != null &&
    item.value !== meta.defaultValue;

  if (meta.kind === "enum") {
    return (
      <EnumSettingControl
        item={item}
        meta={meta}
        op={op}
        providerStatus={providerStatus}
        controlValue={controlValue}
        showDefaultAction={showDefaultAction}
        onChange={onChange}
      />
    );
  }
  if (meta.kind === "model") {
    return (
      <ModelSelectControl
        item={item}
        meta={meta}
        op={op}
        modelsQuery={modelsQuery}
        showDefaultAction={showDefaultAction}
        onChange={onChange}
      />
    );
  }
  if (item.key === MODEL_LIBRARY_SYNC_PROXY_NAME_KEY) {
    return (
      <UpdateProxySelectControl
        item={item}
        op={op}
        proxies={updateProxyOptions}
        onChange={onChange}
      />
    );
  }
  if (meta.kind === "toggle") {
    return (
      <ToggleSettingControl
        meta={meta}
        op={op}
        controlValue={controlValue}
        showDefaultAction={showDefaultAction}
        onChange={onChange}
      />
    );
  }
  if (meta.kind === "integer" || meta.kind === "decimal") {
    return (
      <NumericSettingControl
        item={item}
        meta={meta}
        op={op}
        inputValue={inputValue}
        showDefaultAction={showDefaultAction}
        onChange={onChange}
      />
    );
  }
  return (
    <TextSettingControl
      item={item}
      meta={meta}
      op={op}
      inputValue={inputValue}
      showDefaultAction={showDefaultAction}
      onChange={onChange}
    />
  );
}

function EnumSettingControl({
  item,
  meta,
  op,
  providerStatus,
  controlValue,
  showDefaultAction,
  onChange,
}: Pick<
  SettingControlProps,
  "item" | "meta" | "op" | "providerStatus" | "onChange"
> & {
  controlValue: string;
  showDefaultAction: boolean;
}) {
  const [showAdvancedEngine, setShowAdvancedEngine] = useState(
    normalizeImageEngine(controlValue) === "dual_race",
  );
    const isEngine = item.key === IMAGE_ENGINE_KEY;
    const normalizedValue = isEngine
      ? normalizeImageEngine(controlValue)
      : item.key === IMAGE_CHANNEL_KEY
        ? normalizeImageChannel(controlValue)
        : controlValue;
    const choices =
      isEngine && !showAdvancedEngine && normalizedValue !== "dual_race"
        ? (meta.choices ?? []).filter((option) => option.value !== "dual_race")
        : meta.choices ?? [];
    return (
      <div className="space-y-2">
        <div
          className="grid gap-2 md:grid-cols-3"
          role="radiogroup"
          aria-label={meta.title}
        >
          {choices.map((option) => {
            const selected = normalizedValue === option.value;
            return (
              <button
                key={option.value}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => onChange({ kind: "set", value: option.value })}
                className={cn(
                  "min-h-[72px] cursor-pointer rounded-[var(--radius-control)] border px-3 py-2 text-left transition-colors",
                  option.value === "dual_race"
                    ? "border-danger-border bg-danger-soft"
                    : selected
                      ? "border-accent-border bg-accent-soft text-[var(--fg-0)]"
                      : "border-[var(--border)] bg-[var(--bg-0)]/60 text-[var(--fg-1)] hover:bg-[var(--bg-2)]",
                )}
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="type-body-sm font-medium text-current">{option.label}</span>
                  {option.badge && (
                    <span
                      className={cn(
                        "rounded-full border px-2 py-0.5 text-[10px]",
                        option.value === "dual_race"
                          ? "border-danger-border bg-danger-soft text-danger"
                          : "border-warning-border bg-warning-soft text-warning",
                      )}
                    >
                      {option.badge}
                    </span>
                  )}
                </span>
                <span className="mt-1 block type-caption text-[var(--fg-2)]">
                  {option.description}
                </span>
              </button>
            );
          })}
        </div>
        {isEngine && !showAdvancedEngine && (
          <button
            type="button"
            onClick={() => setShowAdvancedEngine(true)}
            className="inline-flex min-h-[32px] cursor-pointer items-center gap-1 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-2 type-caption text-danger transition-colors hover:bg-danger/15"
          >
            <ChevronRight className="h-3.5 w-3.5" />
            显示进阶路径
          </button>
        )}
        {item.key === IMAGE_CHANNEL_KEY && (
          <p className="type-caption text-[var(--fg-2)]">{providerStatus.label}</p>
        )}
        <ResetEditButton
          dirty={!!op}
          defaultValue={meta.defaultValue}
          showDefaultAction={showDefaultAction}
          onReset={() => onChange(undefined)}
          onUseDefault={(value) => onChange({ kind: "set", value })}
        />
      </div>
    );
}

function ToggleSettingControl({
  meta,
  op,
  controlValue,
  showDefaultAction,
  onChange,
}: Pick<SettingControlProps, "meta" | "op" | "onChange"> & {
  controlValue: string;
  showDefaultAction: boolean;
}) {
    const checked = controlValue === "1";
    return (
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          role="switch"
          aria-checked={checked}
          aria-label={`${meta.title} ${checked ? "关闭" : "开启"}`}
          onClick={() => onChange({ kind: "set", value: checked ? "0" : "1" })}
          className={cn(
            "relative inline-flex h-7 w-12 shrink-0 cursor-pointer items-center rounded-full border transition-colors focus:outline-none focus:ring-2 focus:ring-accent/30 max-sm:min-h-11 max-sm:min-w-11",
            checked
              ? "border-accent-border bg-accent"
              : "border-[var(--border)] bg-[var(--bg-2)]",
          )}
        >
          <span
            aria-hidden
            className={cn(
              "inline-block h-5 w-5 rounded-full bg-[var(--bg-0)] shadow-[var(--shadow-1)] transition-transform",
              checked ? "translate-x-[22px]" : "translate-x-0.5",
            )}
          />
        </button>
        <span
          className={cn(
            "inline-flex rounded-[var(--radius-control)] border px-2 py-1 type-caption",
            checked
              ? "border-success-border bg-success-soft text-success"
              : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]",
          )}
        >
          {checked ? "开启" : "关闭"}
        </span>
        <ResetEditButton
          dirty={!!op}
          defaultValue={meta.defaultValue}
          showDefaultAction={showDefaultAction}
          onReset={() => onChange(undefined)}
          onUseDefault={(value) => onChange({ kind: "set", value })}
        />
      </div>
    );
}

function NumericSettingControl({
  item,
  meta,
  op,
  inputValue,
  showDefaultAction,
  onChange,
}: Pick<SettingControlProps, "item" | "meta" | "op" | "onChange"> & {
  inputValue: string;
  showDefaultAction: boolean;
}) {
    return (
      <div className="flex flex-col gap-2 md:flex-row md:items-center">
        <label htmlFor={`setting-${item.key}`} className="sr-only">
          {meta.title}
        </label>
        <div className="relative flex-1">
          <input
            id={`setting-${item.key}`}
            type="number"
            value={inputValue}
            min={meta.min}
            max={meta.max}
            step={meta.step ?? (meta.kind === "integer" ? 1 : "any")}
            onChange={(e) => {
              const value = e.target.value;
              onChange(value === "" ? undefined : { kind: "set", value });
            }}
            placeholder={
              meta.defaultValue
                ? `默认 ${formatValue(meta.defaultValue, meta)}`
                : "填写数值"
            }
            inputMode={meta.kind === "integer" ? "numeric" : "decimal"}
            className={`${settingMonoInputClassName} pr-16`}
          />
          {meta.unit && (
            <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 type-caption text-[var(--fg-2)]">
              {meta.unit}
            </span>
          )}
        </div>
        <ResetEditButton
          dirty={!!op}
          defaultValue={meta.defaultValue}
          showDefaultAction={showDefaultAction}
          onReset={() => onChange(undefined)}
          onUseDefault={(value) => onChange({ kind: "set", value })}
        />
      </div>
    );
}

function TextSettingControl({
  item,
  meta,
  op,
  inputValue,
  showDefaultAction,
  onChange,
}: Pick<SettingControlProps, "item" | "meta" | "op" | "onChange"> & {
  inputValue: string;
  showDefaultAction: boolean;
}) {
  const browserOrigin = useSyncExternalStore(
    subscribeStatic,
    getBrowserOrigin,
    getBrowserOriginSSR,
  );
  return (
    <div className="flex flex-col gap-2 md:flex-row md:items-center">
      <label htmlFor={`setting-${item.key}`} className="sr-only">
        {meta.title}
      </label>
      <input
        id={`setting-${item.key}`}
        type={meta.kind === "url" ? "url" : "text"}
        value={inputValue}
        onChange={(e) => {
          const value = e.target.value;
          onChange(value === "" ? undefined : { kind: "set", value });
        }}
        placeholder={
          meta.kind === "url"
            ? "https://example.com"
            : meta.defaultValue
              ? `默认 ${meta.defaultValue}`
              : "填写内容"
        }
        autoComplete="off"
        className={`flex-1 ${settingMonoInputClassName}`}
      />
      {meta.kind === "url" && browserOrigin && (
        <Button
          variant="secondary"
          size="sm"
          onClick={() => onChange({ kind: "set", value: browserOrigin })}
          leftIcon={<Globe className="h-3.5 w-3.5" />}
        >
          填入当前域名
        </Button>
      )}
      <ResetEditButton
        dirty={!!op}
        defaultValue={meta.defaultValue}
        showDefaultAction={showDefaultAction}
        onReset={() => onChange(undefined)}
        onUseDefault={(value) => onChange({ kind: "set", value })}
      />
    </div>
  );
}

export function settingControlValues(
  item: SystemSettingItem,
  meta: SettingMeta,
  op: Op | undefined,
) {
  if (op?.kind === "clear") {
    return { controlValue: "", inputValue: "" };
  }
  if (op?.kind === "set") {
    return { controlValue: op.value, inputValue: op.value };
  }
  return {
    controlValue: item.value ?? meta.defaultValue ?? "",
    inputValue: item.value ?? "",
  };
}

export function ModelSelectControl({
  item,
  meta,
  op,
  modelsQuery,
  showDefaultAction,
  onChange,
}: {
  item: SystemSettingItem;
  meta: SettingMeta;
  op: Op | undefined;
  modelsQuery: ModelsQueryState;
  showDefaultAction: boolean;
  onChange: (op: Op | undefined) => void;
}) {
  const modelIds = useMemo(
    () => collectModelIds(meta.defaultValue, modelsQuery.models),
    [meta.defaultValue, modelsQuery.models],
  );
  const inputValue = modelInputValue(item, op);
  const effective = inputValue || meta.defaultValue || "";
  const [customMode, setCustomMode] = useState(
    Boolean(effective && !modelIds.includes(effective)),
  );

  if (modelsQuery.isError || modelIds.length === 0) {
    return (
      <ModelFallbackControl
        item={item}
        meta={meta}
        op={op}
        value={inputValue}
        errorMessage={modelsQuery.errorMessage}
        showDefaultAction={showDefaultAction}
        onChange={onChange}
      />
    );
  }

  return (
    <ModelChoiceControl
      item={item}
      meta={meta}
      op={op}
      modelIds={modelIds}
      effective={effective}
      value={inputValue}
      customMode={customMode}
      setCustomMode={setCustomMode}
      loading={modelsQuery.isLoading}
      showDefaultAction={showDefaultAction}
      onChange={onChange}
    />
  );
}

function collectModelIds(defaultValue: string | undefined, models: string[]) {
  const ids = new Set<string>();
  if (defaultValue) ids.add(defaultValue);
  for (const model of models) ids.add(model);
  return Array.from(ids).sort();
}

function modelInputValue(item: SystemSettingItem, op: Op | undefined) {
  if (op?.kind === "clear") return "";
  if (op?.kind === "set") return op.value;
  return item.value ?? "";
}

function ModelFallbackControl({
  item,
  meta,
  op,
  value,
  errorMessage,
  showDefaultAction,
  onChange,
}: {
  item: SystemSettingItem;
  meta: SettingMeta;
  op: Op | undefined;
  value: string;
  errorMessage?: string;
  showDefaultAction: boolean;
  onChange: (op: Op | undefined) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-col gap-2 md:flex-row md:items-center">
        <TextSettingInput
          item={item}
          meta={meta}
          value={value}
          onChange={onChange}
        />
        <ResetEditButton
          dirty={!!op}
          defaultValue={meta.defaultValue}
          showDefaultAction={showDefaultAction}
          onReset={() => onChange(undefined)}
          onUseDefault={(defaultValue) =>
            onChange({ kind: "set", value: defaultValue })
          }
        />
      </div>
      <p className="type-caption text-warning">
        模型列表读取失败，已切换为手动输入
        {errorMessage ? `：${errorMessage}` : ""}
      </p>
    </div>
  );
}

function ModelChoiceControl({
  item,
  meta,
  op,
  modelIds,
  effective,
  value,
  customMode,
  setCustomMode,
  loading,
  showDefaultAction,
  onChange,
}: {
  item: SystemSettingItem;
  meta: SettingMeta;
  op: Op | undefined;
  modelIds: string[];
  effective: string;
  value: string;
  customMode: boolean;
  setCustomMode: (value: boolean) => void;
  loading: boolean;
  showDefaultAction: boolean;
  onChange: (op: Op | undefined) => void;
}) {
  return (
    <div className="flex flex-col gap-2 md:flex-row md:items-center">
      {customMode ? (
        <TextSettingInput
          item={item}
          meta={meta}
          value={value}
          onChange={onChange}
        />
      ) : (
        <select
          value={modelIds.includes(effective) ? effective : "__custom__"}
          onChange={(event) => {
            const next = event.target.value;
            if (next === "__custom__") {
              setCustomMode(true);
              return;
            }
            onChange({ kind: "set", value: next });
          }}
          className={`flex-1 ${settingMonoInputClassName}`}
        >
          {modelIds.map((model) => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
          <option value="__custom__">自定义...</option>
        </select>
      )}
      {customMode && (
        <Button
          variant="secondary"
          size="sm"
          onClick={() => setCustomMode(false)}
        >
          返回列表
        </Button>
      )}
      <ResetEditButton
        dirty={!!op}
        defaultValue={meta.defaultValue}
        showDefaultAction={showDefaultAction}
        onReset={() => onChange(undefined)}
        onUseDefault={(defaultValue) =>
          onChange({ kind: "set", value: defaultValue })
        }
      />
      {loading && (
        <span className="inline-flex items-center gap-1 type-caption text-[var(--fg-2)]">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          模型列表读取中
        </span>
      )}
    </div>
  );
}

export function UpdateProxySelectControl({
  item,
  op,
  proxies,
  onChange,
}: {
  item: SystemSettingItem;
  op: Op | undefined;
  proxies: UpdateProxyOption[];
  onChange: (op: Op | undefined) => void;
}) {
  const value =
    op?.kind === "clear" ? "" : op?.kind === "set" ? op.value : item.value ?? "";
  const enabledProxies = proxies.filter((proxy) => proxy.enabled);
  const selectedExists = !value || enabledProxies.some((proxy) => proxy.name === value);
  const proxyFeatureLabel =
    item.key === MODEL_LIBRARY_SYNC_PROXY_NAME_KEY
      ? "模特库同步使用代理池"
      : "更新时使用代理池";

  return (
    <div className="space-y-2">
      <div className="flex flex-col gap-2 md:flex-row md:items-center">
        <select
          value={selectedExists ? value : "__custom__"}
          onChange={(event) => {
            const next = event.target.value;
            if (next === "") {
              onChange(item.value ? { kind: "clear" } : undefined);
            } else {
              onChange({ kind: "set", value: next });
            }
          }}
          className={`flex-1 ${settingInputClassName}`}
        >
          <option value="">自动选择第一个启用代理</option>
          {enabledProxies.map((proxy) => (
            <option key={proxy.name} value={proxy.name}>
              {proxy.name}
              {proxy.last_latency_ms != null
                ? ` · ${Math.round(proxy.last_latency_ms)}ms`
                : ""}
              {proxy.in_cooldown ? " · 冷却中" : ""}
            </option>
          ))}
          {!selectedExists && <option value="__custom__">{value}</option>}
        </select>
        <ResetEditButton
          dirty={!!op}
          defaultValue={undefined}
          showDefaultAction={false}
          onReset={() => onChange(undefined)}
          onUseDefault={() => {}}
        />
      </div>
      {enabledProxies.length === 0 ? (
        <p className="type-caption text-warning">
          代理池没有启用代理；开启“{proxyFeatureLabel}”后，请求会被后端拒绝。
        </p>
      ) : (
        <p className="type-caption text-[var(--fg-2)]">
          可用代理 {enabledProxies.length} 个，选择后记得保存设置。
        </p>
      )}
    </div>
  );
}

export function TextSettingInput({
  item,
  meta,
  value,
  onChange,
}: {
  item: SystemSettingItem;
  meta: SettingMeta;
  value: string;
  onChange: (op: Op | undefined) => void;
}) {
  return (
    <>
      <label htmlFor={`setting-${item.key}`} className="sr-only">
        {meta.title}
      </label>
      <input
        id={`setting-${item.key}`}
        type={meta.kind === "url" ? "url" : "text"}
        value={value}
        onChange={(e) => {
          const next = e.target.value;
          onChange(next === "" ? undefined : { kind: "set", value: next });
        }}
        placeholder={
          meta.kind === "url"
            ? "https://example.com"
            : meta.defaultValue
              ? `默认 ${meta.defaultValue}`
              : "填写内容"
        }
        autoComplete="off"
        className={`flex-1 ${settingMonoInputClassName}`}
      />
    </>
  );
}

export function ResetEditButton({
  dirty,
  defaultValue,
  showDefaultAction,
  onReset,
  onUseDefault,
}: {
  dirty: boolean;
  defaultValue: string | undefined;
  showDefaultAction: boolean;
  onReset: () => void;
  onUseDefault: (value: string) => void;
}) {
  if (dirty) {
    return (
      <Button
        variant="secondary"
        size="sm"
        onClick={onReset}
        leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
      >
        撤销修改
      </Button>
    );
  }
  if (!defaultValue || !showDefaultAction) return null;
  return (
    <Button
      variant="secondary"
      size="sm"
      onClick={() => onUseDefault(defaultValue)}
      leftIcon={<Check className="h-3.5 w-3.5" />}
    >
      填入默认值
    </Button>
  );
}

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
