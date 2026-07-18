import {
  useRef,
  useState,
  type ComponentProps,
} from "react";

import {
  Input,
  Textarea,
} from "@/components/ui/primitives";

const SELECT_CLASS =
  "h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 type-body-sm text-[var(--fg-0)] focus:border-[var(--accent)] focus:outline-none focus:ring-2 focus:ring-[var(--accent-soft)] max-sm:min-h-11 max-sm:text-base";

export type SelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

export function ConfigSection({
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
      <span className="type-caption font-medium text-[var(--fg-1)]">
        {label}
      </span>
      <select
        className={SELECT_CLASS}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.currentTarget.value)}
      >
        {options.map((option) => (
          <option
            key={`${option.value}:${option.label}`}
            value={option.value}
            disabled={option.disabled}
          >
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function RangeField({
  label,
  value,
  min,
  max,
  step = 1,
  suffix,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  suffix?: string;
  onChange: (value: number) => void;
}) {
  const boundedValue = Math.min(Math.max(value, min), Math.max(min, max));
  return (
    <RangeFieldControl
      key={`${boundedValue}:${min}:${max}:${step}`}
      label={label}
      value={boundedValue}
      min={min}
      max={max}
      step={step}
      suffix={suffix}
      onChange={onChange}
    />
  );
}

function RangeFieldControl({
  label,
  value,
  min,
  max,
  step,
  suffix,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  suffix?: string;
  onChange: (value: number) => void;
}) {
  const [draft, setDraft] = useState(value);
  const committedRef = useRef(value);
  const commit = () => {
    if (draft === committedRef.current) return;
    committedRef.current = draft;
    onChange(draft);
  };
  return (
    <label className="grid gap-2">
      <span className="flex items-center justify-between gap-3 type-caption font-medium text-[var(--fg-1)]">
        {label}
        <span className="font-mono text-[var(--fg-0)]">
          {draft}
          {suffix}
        </span>
      </span>
      <input
        type="range"
        min={min}
        max={Math.max(min, max)}
        step={step}
        value={draft}
        onChange={(event) => setDraft(Number(event.currentTarget.value))}
        onPointerUp={commit}
        onKeyUp={commit}
        onBlur={commit}
        className="h-11 w-full cursor-pointer accent-[var(--accent)]"
      />
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
    <label className="flex min-h-11 cursor-pointer items-center justify-between gap-3">
      <span className="type-body-sm text-[var(--fg-1)]">{label}</span>
      <span className="relative inline-flex h-6 w-10 shrink-0">
        <input
          type="checkbox"
          checked={checked}
          disabled={disabled}
          onChange={(event) => onChange(event.currentTarget.checked)}
          className="peer sr-only"
        />
        <span className="absolute inset-0 rounded-full border border-[var(--border-strong)] bg-[var(--bg-2)] transition-colors peer-checked:border-[var(--accent-border)] peer-checked:bg-[var(--accent)] peer-disabled:cursor-not-allowed peer-disabled:opacity-50" />
        <span className="pointer-events-none absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-[var(--fg-0)] shadow-[var(--shadow-1)] transition-transform peer-checked:translate-x-4" />
      </span>
    </label>
  );
}

export function CommitInput({
  value,
  onCommit,
  ...props
}: Omit<
  ComponentProps<typeof Input>,
  "value" | "defaultValue" | "onChange"
> & {
  value: string;
  onCommit: (value: string) => void;
}) {
  return (
    <Input
      key={value}
      {...props}
      defaultValue={value}
      onBlur={(event) => {
        const nextValue = event.currentTarget.value.trim();
        if (nextValue !== value) onCommit(nextValue);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          event.currentTarget.blur();
        }
        if (event.key === "Escape") {
          event.preventDefault();
          event.currentTarget.value = value;
          event.currentTarget.blur();
        }
      }}
    />
  );
}

export function CommitTextarea({
  value,
  onCommit,
  ...props
}: Omit<
  ComponentProps<typeof Textarea>,
  "value" | "defaultValue" | "onChange"
> & {
  value: string;
  onCommit: (value: string) => void;
}) {
  return (
    <Textarea
      key={value}
      {...props}
      defaultValue={value}
      onBlur={(event) => {
        const nextValue = event.currentTarget.value;
        if (nextValue !== value) onCommit(nextValue);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.currentTarget.value = value;
          event.currentTarget.blur();
        }
      }}
    />
  );
}
