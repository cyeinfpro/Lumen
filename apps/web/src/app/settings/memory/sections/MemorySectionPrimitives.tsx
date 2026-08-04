import type { ReactNode } from "react";

import { type MemoryType } from "@/lib/apiClient";

import { typeLabel } from "../memoryPageUtils";

export function SectionHeader({
  title,
  suffix,
  actions,
}: {
  title: string;
  suffix?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-[var(--border-subtle)] p-4">
      <div className="flex items-baseline gap-2">
        <h2 className="type-card-title">{title}</h2>
        {suffix ? (
          <span className="type-caption text-[var(--fg-2)]">{suffix}</span>
        ) : null}
      </div>
      {actions ? (
        <div className="flex items-center gap-2">{actions}</div>
      ) : null}
    </div>
  );
}

export function TypeBadge({ type }: { type: MemoryType | string }) {
  return (
    <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-1.5 py-0.5 type-caption text-[var(--fg-1)]">
      {typeLabel(type)}
    </span>
  );
}
