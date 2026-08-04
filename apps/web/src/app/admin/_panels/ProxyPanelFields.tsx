import {
  AlertCircle,
  CheckCircle2,
  Clock,
  Edit3,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Zap,
  XCircle,
} from "lucide-react";

import { Button, Select } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import type { ProxyTestOut } from "@/lib/types";

export function ProxyToolbar({
  isEditing,
  hasProxies,
  testingAll,
  saving,
  editError,
  onStartEdit,
  onTestAll,
  onRefresh,
  onSave,
  onCancel,
  onAdd,
}: {
  isEditing: boolean;
  hasProxies: boolean;
  testingAll: boolean;
  saving: boolean;
  editError: string | null;
  onStartEdit: () => void;
  onTestAll: () => void;
  onRefresh: () => void;
  onSave: () => void;
  onCancel: () => void;
  onAdd: () => void;
}) {
  if (!isEditing) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          size="md"
          onClick={onStartEdit}
          leftIcon={<Edit3 className="h-3.5 w-3.5" />}
        >
          编辑代理列表
        </Button>
        <Button
          variant="secondary"
          size="md"
          onClick={onTestAll}
          disabled={testingAll || !hasProxies}
          loading={testingAll}
          leftIcon={!testingAll ? <Zap className="h-3.5 w-3.5" /> : undefined}
        >
          {testingAll ? "全部测试中" : "全部测一遍"}
        </Button>
        <Button
          variant="secondary"
          size="md"
          onClick={onRefresh}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          刷新
        </Button>
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        variant="primary"
        size="md"
        onClick={onSave}
        disabled={saving}
        loading={saving}
        leftIcon={!saving ? <Save className="h-3.5 w-3.5" /> : undefined}
      >
        {saving ? copy.state.saving : "保存代理列表"}
      </Button>
      <Button
        variant="secondary"
        size="md"
        onClick={onCancel}
        disabled={saving}
        leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
      >
        {copy.action.cancel}
      </Button>
      <Button
        variant="secondary"
        size="md"
        onClick={onAdd}
        leftIcon={<Plus className="h-3.5 w-3.5" />}
      >
        加一个代理
      </Button>
      {editError ? (
        <span
          role="alert"
          className="inline-flex items-center gap-1 type-caption text-danger"
        >
          <AlertCircle className="h-3 w-3" /> {editError}
        </span>
      ) : null}
    </div>
  );
}

export function LatencyBadge({
  tested,
  testing,
}: {
  tested: ProxyTestOut | null | undefined;
  testing: boolean;
}) {
  if (testing) {
    return (
      <span className="inline-flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
        <RefreshCw className="h-3 w-3 animate-spin" /> 测试中
      </span>
    );
  }
  if (!tested) {
    return (
      <span className="inline-flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
        <Clock className="h-3 w-3" /> 还未测过
      </span>
    );
  }
  if (!tested.ok) {
    return (
      <span
        className="inline-flex items-center gap-1.5 type-caption text-danger"
        title={tested.error ?? ""}
      >
        <XCircle className="h-3.5 w-3.5" /> 不通
      </span>
    );
  }
  const ms = Math.max(0, tested.latency_ms);
  const color =
    ms < 200 ? "text-success" : ms < 600 ? "text-warning" : "text-danger";
  return (
    <span className={`inline-flex items-center gap-1.5 type-caption ${color}`}>
      <CheckCircle2 className="h-3.5 w-3.5" />
      <span className="font-mono tabular-nums">{ms.toFixed(0)} ms</span>
    </span>
  );
}

export function Field({
  label,
  hint,
  value,
  onChange,
  inputMode,
}: {
  label: string;
  hint: string;
  value: string;
  onChange: (value: string) => void;
  inputMode?: "text" | "numeric";
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="type-caption text-[var(--fg-1)]">{label}</span>
      <input
        type="text"
        value={value}
        inputMode={inputMode}
        onChange={(event) => onChange(event.target.value)}
        className="h-9 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 type-body-sm outline-none transition-colors focus:border-[var(--accent)]/50 focus:ring-2 focus:ring-[var(--accent)]/25"
      />
      <span className="type-caption leading-relaxed text-[var(--fg-2)]">
        {hint}
      </span>
    </label>
  );
}

export function FieldInline({
  label,
  value,
  onChange,
  placeholder,
  mono,
  inputMode,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  mono?: boolean;
  inputMode?: "text" | "numeric";
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <input
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        inputMode={inputMode}
        autoComplete="off"
        className={[
          "h-9 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 type-body-sm outline-none transition-colors focus:border-[var(--accent)]/50 focus:ring-2 focus:ring-[var(--accent)]/25",
          mono ? "font-mono" : "",
        ].join(" ")}
      />
    </label>
  );
}

export function FieldSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <Select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-9"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </Select>
    </label>
  );
}
