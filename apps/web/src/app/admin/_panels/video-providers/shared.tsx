import type { ReactNode } from "react";
import { AlertCircle } from "lucide-react";

import { Input, StatusBadge } from "@/components/ui/primitives";
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
  return (
    <StatusBadge
      status={
        tone === "success"
          ? "success"
          : tone === "warning"
            ? "warning"
            : tone === "danger"
              ? "error"
              : "unknown"
      }
      tone={tone === "neutral" ? "info" : tone}
      label={label}
      className={
        tone === "neutral"
          ? "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]"
          : undefined
      }
    />
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
    <Input
      label={label}
      type={type}
      value={value}
      name={name}
      autoComplete={autoComplete}
      placeholder={placeholder}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function MetaSep() {
  return <span className="text-[var(--fg-3)]">·</span>;
}
