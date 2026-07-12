import { format } from "date-fns";

export const adminInputShellClassName =
  "flex items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)]/60 px-3 transition-colors focus-within:border-accent-border focus-within:ring-2 focus-within:ring-accent/20";

export const tableShellClassName =
  "overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 shadow-[var(--shadow-1)] backdrop-blur-sm";

export function formatISODate(value: string): string {
  try {
    return format(new Date(value), "yyyy-MM-dd HH:mm");
  } catch {
    return value;
  }
}
