"use client";

import { useId } from "react";
import { AlertCircle, Check } from "lucide-react";

import { copy } from "@/lib/copy";

import { clampInt } from "./ByokPanel.model";

export function ByokNotices({
  error,
  saved,
  onClearError,
  onClearSaved,
}: {
  error: string | null;
  saved: string | null;
  onClearError: () => void;
  onClearSaved: () => void;
}) {
  return (
    <>
      {error && (
        <div className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-danger">
          <AlertCircle className="mt-0.5 w-4 h-4 shrink-0" />
          <span className="flex-1">{error}</span>
          <button
            type="button"
            onClick={onClearError}
            className="type-caption text-danger/80 hover:text-danger"
          >
            {copy.action.close}
          </button>
        </div>
      )}
      {saved && (
        <div className="flex items-center gap-2 rounded-[var(--radius-card)] border border-success-border bg-success-soft px-3 py-2 type-body-sm text-success">
          <Check className="w-4 h-4" />
          <span className="flex-1">{saved}</span>
          <button
            type="button"
            onClick={onClearSaved}
            className="type-caption text-success/80 hover:text-success"
          >
            {copy.action.close}
          </button>
        </div>
      )}
    </>
  );
}

export function FieldText({
  label,
  hint,
  value,
  onChange,
  onBlur,
  placeholder,
  error,
  isPassword,
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (value: string) => void;
  onBlur?: (value: string) => void;
  placeholder?: string;
  error?: string | null;
  isPassword?: boolean;
}) {
  const id = useId();
  return (
    <label htmlFor={id} className="flex flex-col gap-1">
      <span className="text-[11px] uppercase tracking-wider text-[var(--fg-1)]">
        {label}
      </span>
      <input
        id={id}
        type={isPassword ? "password" : "text"}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur ? (event) => onBlur(event.target.value) : undefined}
        placeholder={placeholder}
        className={
          "h-10 rounded-[var(--radius-control)] bg-[var(--bg-0)] px-3 text-sm border focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/25 placeholder:text-[var(--fg-3)] transition-colors " +
          (error ? "border-danger-border" : "border-[var(--border)]")
        }
      />
      {error ? (
        <span
          role="alert"
          aria-live="assertive"
          className="text-[11px] text-danger"
        >
          {error}
        </span>
      ) : hint ? (
        <span className="text-[11px] text-[var(--fg-2)]">{hint}</span>
      ) : null}
    </label>
  );
}

export function FieldNumber({
  label,
  hint,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
}) {
  const id = useId();
  return (
    <label htmlFor={id} className="flex flex-col gap-1">
      <span className="text-[11px] uppercase tracking-wider text-[var(--fg-1)]">
        {label}
      </span>
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(event) => onChange(clampInt(event.target.value, min, max))}
        className="h-10 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--accent)]/25"
      />
      {hint && (
        <span className="text-[11px] text-[var(--fg-2)]">{hint}</span>
      )}
    </label>
  );
}

export function ToggleRow({
  checked,
  label,
  hint,
  disabled,
  onChange,
}: {
  checked: boolean;
  label: string;
  hint?: string;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label
      className={
        "flex items-start justify-between gap-3 rounded-[var(--radius-panel)] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-3 py-2 " +
        (disabled ? "opacity-50" : "")
      }
    >
      <span className="flex flex-col">
        <span className="text-sm text-[var(--fg-0)]">{label}</span>
        {hint && (
          <span className="text-[11px] text-[var(--fg-2)] mt-0.5">
            {hint}
          </span>
        )}
      </span>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-1"
      />
    </label>
  );
}
