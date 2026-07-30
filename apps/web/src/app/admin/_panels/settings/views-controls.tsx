"use client";

import { useMemo, useState, useSyncExternalStore } from "react";
import {
  Check,
  ChevronRight,
  Globe,
  Loader2,
  RotateCcw,
} from "lucide-react";
import type { SystemSettingItem } from "@/lib/types";
import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import {
  IMAGE_CHANNEL_KEY,
  IMAGE_ENGINE_KEY,
  MODEL_LIBRARY_SYNC_PROXY_NAME_KEY,
  type ModelsQueryState,
  type Op,
  type ProviderStatus,
  type SettingMeta,
  type UpdateProxyOption,
  formatValue,
  getBrowserOrigin,
  getBrowserOriginSSR,
  normalizeImageChannel,
  normalizeImageEngine,
  settingInputClassName,
  settingMonoInputClassName,
  subscribeStatic,
} from "./model";

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
