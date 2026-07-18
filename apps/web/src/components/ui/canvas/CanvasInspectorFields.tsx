import { Check } from "lucide-react";

import { Button } from "@/components/ui/primitives";

const SELECT_CLASS =
  "h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 type-body-sm text-[var(--fg-0)] focus:border-[var(--accent)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-soft)] max-sm:min-h-11 max-sm:text-base";

export type SelectOption = {
  value: string;
  label: string;
};

const NODE_COLOR_OPTIONS = [
  { value: null, label: "无颜色", color: "var(--bg-3)" },
  { value: "accent", label: "琥珀色", color: "var(--accent)" },
  { value: "success", label: "绿色", color: "var(--success)" },
  { value: "info", label: "蓝色", color: "var(--info)" },
  { value: "danger", label: "红色", color: "var(--danger)" },
] as const;

export function InlineConfigConfirmation({
  removedConnections,
  onCancel,
  onConfirm,
}: {
  removedConnections: number;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <section
      aria-label="模式切换确认"
      className="m-4 grid gap-3 rounded-[var(--radius-card)] border border-[var(--danger)]/35 bg-[var(--danger-soft)] p-3"
    >
      <div role="alert" aria-live="assertive">
        <h3 className="type-body-sm font-medium text-[var(--danger-fg)]">
          确认切换模式
        </h3>
        <p className="mt-1 type-caption text-[var(--fg-1)]">
          继续后会移除 {removedConnections} 条不兼容连接。
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Button size="sm" variant="outline" onClick={onCancel}>
          取消
        </Button>
        <Button size="sm" variant="danger" onClick={onConfirm}>
          继续
        </Button>
      </div>
    </section>
  );
}

export function ColorSwatchField({
  value,
  disabled,
  onChange,
}: {
  value: string | null;
  disabled?: boolean;
  onChange: (value: string | null) => void;
}) {
  return (
    <fieldset disabled={disabled} className="grid gap-2">
      <legend className="type-caption font-medium text-[var(--fg-1)]">
        颜色标记
      </legend>
      <div className="flex flex-wrap gap-2">
        {NODE_COLOR_OPTIONS.map((option) => {
          const selected = value === option.value;
          return (
            <button
              key={option.label}
              type="button"
              title={option.label}
              aria-label={option.label}
              aria-pressed={selected}
              onClick={() => onChange(option.value)}
              style={{ backgroundColor: option.color }}
              className={`relative grid h-11 w-11 place-items-center rounded-full border transition-[border-color,box-shadow,opacity] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-50 ${
                selected
                  ? "border-[var(--fg-0)] ring-2 ring-[var(--accent)]/35"
                  : "border-[var(--border-strong)]"
              }`}
            >
              {option.value === null ? (
                <span
                  aria-hidden
                  className="h-px w-6 rotate-45 bg-[var(--fg-2)]"
                />
              ) : null}
              {selected ? (
                <Check
                  className="absolute bottom-0.5 right-0.5 h-3.5 w-3.5 rounded-full bg-[var(--bg-0)] p-0.5 text-[var(--fg-0)]"
                  aria-hidden
                />
              ) : null}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}

export function InspectorShell({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--bg-1)] text-[var(--fg-0)]">
      <header className="shrink-0 border-b border-[var(--border)] px-4 py-3">
        <p className="type-page-kicker">{eyebrow}</p>
        <h2 className="type-card-title mt-1 truncate">{title}</h2>
      </header>
      {children}
    </div>
  );
}

export function InspectorSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="grid gap-3 border-b border-[var(--border)] p-4 last:border-0">
      <h3 className="type-overline text-[var(--fg-2)]">{title}</h3>
      {children}
    </section>
  );
}

export function SelectField({
  label,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  value: string;
  options: readonly SelectOption[];
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1">
      <span className="type-caption font-medium text-[var(--fg-1)]">{label}</span>
      <select
        className={SELECT_CLASS}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.value)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function ToggleField({
  label,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex min-h-11 items-center justify-between gap-3">
      <span className="type-body-sm text-[var(--fg-1)]">{label}</span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.checked)}
        className="h-5 w-5 accent-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-50"
      />
    </label>
  );
}

export function ReadOnlyRow({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center justify-between gap-3 type-body-sm">
      <span className="text-[var(--fg-2)]">{label}</span>
      <span className="truncate text-[var(--fg-0)]">{value}</span>
    </div>
  );
}
