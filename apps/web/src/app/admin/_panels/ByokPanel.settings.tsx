"use client";

import type { ReactNode } from "react";
import {
  AlertCircle,
  ChevronDown,
  Globe,
  KeyRound,
  Lock,
  Save,
  Server,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import type { ByokSettingsOut, ByokSettingsPatchIn } from "@/lib/types";

import {
  ADVANCED_TOGGLES,
  clampInt,
  MODE_DEFS,
  TIMEOUT_MAX_MS,
  TIMEOUT_MIN_MS,
  TTL_MAX_S,
  TTL_MIN_S,
} from "./ByokPanel.model";
import type { ByokMode, ModeDef } from "./ByokPanel.model";
import { FieldNumber, FieldText, ToggleRow } from "./ByokPanel.shared";

const MODE_ICONS: Record<ByokMode, LucideIcon> = {
  off: Lock,
  bind_only: KeyRound,
  key_first: Sparkles,
  fully_open: Globe,
};

export function Overview({
  mode,
  supplierCount,
  activeCredentials,
  loading,
}: {
  mode: ByokMode | null;
  supplierCount: number;
  activeCredentials: number;
  loading: boolean;
}) {
  const def = mode ? MODE_DEFS.find((item) => item.value === mode) : undefined;
  const ModeIcon = mode ? MODE_ICONS[mode] : AlertCircle;
  return (
    <section className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-5">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <OverviewItem
          icon={<ModeIcon className="w-4 h-4" />}
          label="当前模式"
          value={loading ? "加载中…" : (def?.label ?? "自定义")}
        />
        <OverviewItem
          icon={<Server className="w-4 h-4" />}
          label="供应商模板"
          value={loading ? "—" : `${supplierCount} 个`}
        />
        <OverviewItem
          icon={<KeyRound className="w-4 h-4" />}
          label="活跃 Key 总数"
          value={loading ? "—" : `${activeCredentials} 把`}
        />
      </div>
    </section>
  );
}

function OverviewItem({
  icon,
  label,
  value,
}: {
  icon: ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 flex h-7 w-7 items-center justify-center rounded-[var(--radius-card)] bg-[var(--bg-2)] border border-[var(--border-subtle)] text-[var(--accent)]">
        {icon}
      </span>
      <div className="flex flex-col">
        <span className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
          {label}
        </span>
        <span className="text-sm text-[var(--fg-0)] mt-0.5">{value}</span>
      </div>
    </div>
  );
}

function ModeCard({
  def,
  active,
  onSelect,
}: {
  def: ModeDef;
  active: boolean;
  onSelect: () => void;
}) {
  const Icon = MODE_ICONS[def.value];
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      className={
        "text-left rounded-[var(--radius-panel)] border p-3 transition-colors " +
        (active
          ? "border-[var(--accent)]/60 bg-[var(--accent)]/10"
          : "border-[var(--border)] bg-[var(--bg-2)] hover:bg-[var(--bg-3)]")
      }
    >
      <div className="flex items-center gap-2">
        <span
          className={
            "flex h-7 w-7 items-center justify-center rounded-[var(--radius-card)] " +
            (active
              ? "bg-[var(--accent)] text-black"
              : "bg-[var(--bg-2)] text-[var(--fg-1)]")
          }
        >
          <Icon className="w-3.5 h-3.5" />
        </span>
        <span className="text-sm font-medium text-[var(--fg-0)]">
          {def.label}
        </span>
      </div>
      <p className="mt-2 text-xs text-[var(--fg-2)] leading-relaxed">
        {def.hint}
      </p>
      <p className="mt-2 text-[11px] text-[var(--fg-2)]">
        适合：{def.scenario}
      </p>
    </button>
  );
}

function ByokModeSettings({
  currentMode,
  effectiveSettings,
  onSetMode,
  onPatch,
}: {
  currentMode: ByokMode | null;
  effectiveSettings: ByokSettingsOut | undefined;
  onSetMode: (mode: ByokMode) => void;
  onPatch: (patch: ByokSettingsPatchIn) => void;
}) {
  return (
    <>
      <header className="flex items-center gap-2 text-xs uppercase tracking-wider text-[var(--fg-2)]">
        <ShieldCheck className="w-3.5 h-3.5" />
        BYOK 模式
      </header>
      <p className="text-xs text-[var(--fg-2)]">
        按业务场景一键配置；下方「高级覆盖」可手动微调 3 个原始开关。
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
        {MODE_DEFS.map((def) => (
          <ModeCard
            key={def.value}
            def={def}
            active={currentMode === def.value}
            onSelect={() => onSetMode(def.value)}
          />
        ))}
      </div>
      {currentMode === null && (
        <p className="flex items-start gap-2 text-xs text-[var(--accent)]/90">
          <AlertCircle className="mt-0.5 w-3.5 h-3.5 shrink-0" />
          当前是自定义组合（未匹配预设模式），点上方任意卡片可重置。
        </p>
      )}
      <details className="group rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-2)] overflow-hidden">
        <summary className="cursor-pointer list-none px-3 py-2 text-xs text-[var(--fg-2)] flex items-center justify-between">
          <span>高级覆盖（手动改 3 个原始开关）</span>
          <ChevronDown className="w-3.5 h-3.5 transition-transform group-open:rotate-180" />
        </summary>
        <div className="p-3 grid grid-cols-1 md:grid-cols-2 gap-3 border-t border-[var(--border-subtle)]">
          {ADVANCED_TOGGLES.map(({ key, label, hint, requiresMode }) => {
            const modeOn = Boolean(effectiveSettings?.mode_enabled);
            const disabled = requiresMode && !modeOn;
            const checked = Boolean(
              (
                effectiveSettings as
                  | Record<string, boolean | undefined>
                  | undefined
              )?.[key],
            );
            return (
              <ToggleRow
                key={key}
                label={label}
                hint={disabled ? "需先开启 BYOK 总开关" : hint}
                checked={checked}
                disabled={disabled}
                onChange={(value) => onPatch({ [key]: value })}
              />
            );
          })}
        </div>
      </details>
    </>
  );
}

function ByokValidationSettings({
  draft,
  settings,
  onPatch,
}: {
  draft: ByokSettingsPatchIn;
  settings: ByokSettingsOut | undefined;
  onPatch: (patch: ByokSettingsPatchIn) => void;
}) {
  return (
    <div className="space-y-2">
      <div className="text-xs uppercase tracking-wider text-[var(--fg-2)]">
        验证设置
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <FieldText
          label="验证模型"
          hint="发随机算术题给上游验证 Key（建议 gpt-5.4）"
          value={draft.validation_model ?? settings?.validation_model ?? ""}
          onChange={(value) => onPatch({ validation_model: value })}
          placeholder="gpt-5.4"
        />
        <FieldNumber
          label="验证超时 (ms)"
          hint={`单次验证 HTTP 请求超时，${TIMEOUT_MIN_MS}-${TIMEOUT_MAX_MS}（默认 15000）`}
          min={TIMEOUT_MIN_MS}
          max={TIMEOUT_MAX_MS}
          value={
            draft.validation_timeout_ms ??
            settings?.validation_timeout_ms ??
            15000
          }
          onChange={(value) =>
            onPatch({
              validation_timeout_ms: clampInt(
                value,
                TIMEOUT_MIN_MS,
                TIMEOUT_MAX_MS,
              ),
            })
          }
        />
        <FieldNumber
          label="Token TTL (秒)"
          hint={`验证完到注册间的最大间隔，${TTL_MIN_S}-${TTL_MAX_S}（默认 900 = 15min）`}
          min={TTL_MIN_S}
          max={TTL_MAX_S}
          value={
            draft.pending_token_ttl_seconds ??
            settings?.pending_token_ttl_seconds ??
            900
          }
          onChange={(value) =>
            onPatch({
              pending_token_ttl_seconds: clampInt(
                value,
                TTL_MIN_S,
                TTL_MAX_S,
              ),
            })
          }
        />
      </div>
    </div>
  );
}

function ByokRetentionSettings({
  effectiveSettings,
  hideDays,
  deleteDays,
  invalid,
  onPatch,
}: {
  effectiveSettings: ByokSettingsOut | undefined;
  hideDays: number;
  deleteDays: number;
  invalid: boolean;
  onPatch: (patch: ByokSettingsPatchIn) => void;
}) {
  return (
    <div className="space-y-3">
      <div className="text-xs uppercase tracking-wider text-[var(--fg-2)]">
        数据保留
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <ToggleRow
          label="超过窗口后用户侧隐藏"
          hint="仅影响 BYOK 用户；管理员仍可在删除前查看。"
          checked={Boolean(effectiveSettings?.retention_hide_enabled ?? true)}
          onChange={(value) => onPatch({ retention_hide_enabled: value })}
        />
        <ToggleRow
          label="自动软删除过期数据"
          hint="危险操作，默认关闭；开启后 worker 会按删除窗口软删除 BYOK 过期数据。"
          checked={Boolean(effectiveSettings?.retention_delete_enabled ?? false)}
          onChange={(value) => onPatch({ retention_delete_enabled: value })}
        />
        <FieldNumber
          label="隐藏窗口（天）"
          hint="默认 3 天；关闭隐藏开关时不生效。"
          min={1}
          max={3650}
          value={hideDays}
          onChange={(value) =>
            onPatch({ retention_hide_days: clampInt(value, 1, 3650) })
          }
        />
        <FieldNumber
          label="删除窗口（天）"
          hint="默认 7 天；关闭自动删除时不生效。"
          min={1}
          max={3650}
          value={deleteDays}
          onChange={(value) =>
            onPatch({ retention_delete_days: clampInt(value, 1, 3650) })
          }
        />
      </div>
      {invalid && (
        <p className="flex items-start gap-2 text-xs text-[var(--danger)]">
          <AlertCircle className="mt-0.5 w-3.5 h-3.5 shrink-0" />
          删除窗口不能小于隐藏窗口。
        </p>
      )}
    </div>
  );
}

function ByokSettingsActions({
  busy,
  dirty,
  retentionInvalid,
  onSave,
  onDiscard,
}: {
  busy: boolean;
  dirty: boolean;
  retentionInvalid: boolean;
  onSave: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <Button
        variant="primary"
        size="md"
        onClick={onSave}
        disabled={busy || !dirty || retentionInvalid}
        loading={busy}
        leftIcon={!busy ? <Save className="w-4 h-4" /> : undefined}
      >
        保存系统设置
      </Button>
      {dirty && (
        <Button
          variant="link"
          size="sm"
          onClick={onDiscard}
          className="text-[var(--fg-2)] no-underline hover:underline"
        >
          丢弃改动
        </Button>
      )}
    </div>
  );
}

export function ByokSystemSettingsSection({
  currentMode,
  effectiveSettings,
  draft,
  settings,
  hideDays,
  deleteDays,
  retentionInvalid,
  busy,
  dirty,
  onSetMode,
  onPatch,
  onSave,
  onDiscard,
}: {
  currentMode: ByokMode | null;
  effectiveSettings: ByokSettingsOut | undefined;
  draft: ByokSettingsPatchIn;
  settings: ByokSettingsOut | undefined;
  hideDays: number;
  deleteDays: number;
  retentionInvalid: boolean;
  busy: boolean;
  dirty: boolean;
  onSetMode: (mode: ByokMode) => void;
  onPatch: (patch: ByokSettingsPatchIn) => void;
  onSave: () => void;
  onDiscard: () => void;
}) {
  return (
    <section className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-5 space-y-4">
      <ByokModeSettings
        currentMode={currentMode}
        effectiveSettings={effectiveSettings}
        onSetMode={onSetMode}
        onPatch={onPatch}
      />
      <ByokValidationSettings
        draft={draft}
        settings={settings}
        onPatch={onPatch}
      />
      <ByokRetentionSettings
        effectiveSettings={effectiveSettings}
        hideDays={hideDays}
        deleteDays={deleteDays}
        invalid={retentionInvalid}
        onPatch={onPatch}
      />
      <ByokSettingsActions
        busy={busy}
        dirty={dirty}
        retentionInvalid={retentionInvalid}
        onSave={onSave}
        onDiscard={onDiscard}
      />
    </section>
  );
}
