import type { ReactNode } from "react";
import { AlertCircle } from "lucide-react";

import type { Issue } from "./domain";

export type StatusTone = "success" | "warning" | "danger" | "neutral";

export function SectionTitle({
  icon,
  title,
}: {
  icon: ReactNode;
  title: string;
}) {
  return (
    <div className="flex items-center gap-2 text-xs font-medium text-[var(--fg-0)]">
      <span className="text-[var(--fg-2)]">{icon}</span>
      {title}
    </div>
  );
}

export function IssueList({
  issues,
  className = "",
}: {
  issues: Issue[];
  className?: string;
}) {
  return (
    <div className={`space-y-1.5 ${className}`}>
      {issues.map((issue, index) => (
        <div
          key={`${issue.message}-${index}`}
          className={`flex items-start gap-2 rounded-[var(--radius-card)] border px-3 py-2 type-caption ${
            issue.severity === "error"
              ? "border-danger-border bg-danger-soft text-danger"
              : "border-warning-border bg-warning-soft text-warning"
          }`}
        >
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{issue.message}</span>
        </div>
      ))}
    </div>
  );
}

export function StatusPill({
  tone,
  label,
}: {
  tone: StatusTone;
  label: string;
}) {
  const className =
    tone === "success"
      ? "border-success-border bg-success-soft text-success"
      : tone === "warning"
        ? "border-warning-border bg-warning-soft text-warning"
        : tone === "danger"
          ? "border-danger-border bg-danger-soft text-danger"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]";

  return (
    <span
      className={`inline-flex items-center rounded-[var(--radius-control)] border px-2 py-1 text-[11px] font-medium ${className}`}
    >
      {label}
    </span>
  );
}

export function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
  name,
  autoComplete,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
  name?: string;
  autoComplete?: string;
}) {
  return (
    <label className="space-y-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <input
        type={type}
        value={value}
        name={name}
        autoComplete={autoComplete}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-3)] focus:border-[var(--accent)]/50"
      />
    </label>
  );
}

export function MetaSep() {
  return <span className="text-[var(--fg-3)]">·</span>;
}
