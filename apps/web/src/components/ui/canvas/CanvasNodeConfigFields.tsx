import {
  useRef,
  useState,
  type ComponentProps,
} from "react";

import {
  Input,
  Select,
  Slider,
  Switch,
  Textarea,
} from "@/components/ui/primitives";

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
      <Select
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
      </Select>
    </label>
  );
}

export function SliderField({
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
    <SliderFieldControl
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

function SliderFieldControl({
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
      <Slider
        min={min}
        max={Math.max(min, max)}
        step={step}
        value={draft}
        onChange={(event) => setDraft(Number(event.currentTarget.value))}
        onPointerUp={commit}
        aria-label={label}
        // I-5：移动端手指滑出滑轨 / 被浏览器手势接管时只有 pointercancel，
        // 没有 pointerup —— 不补这一路，拖到一半的值会显示成已改但从未提交。
        onPointerCancel={commit}
        onKeyUp={commit}
        onBlur={commit}
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
    <div className="flex min-h-11 items-center justify-between gap-3">
      <span className="type-body-sm text-[var(--fg-1)]">{label}</span>
      <Switch
        aria-label={label}
        checked={checked}
        disabled={disabled}
        onCheckedChange={onChange}
      />
    </div>
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
