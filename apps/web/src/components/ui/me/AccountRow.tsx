"use client";

import Link from "next/link";
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface AccountRowProps {
  icon: ReactNode;
  label: ReactNode;
  badge?: ReactNode;
  description?: ReactNode;
  href?: string;
  onClick?: () => void;
  toggle?: {
    checked: boolean;
    onChange: (next: boolean) => void;
    ariaLabel?: string;
  };
  destructive?: boolean;
  grouped?: boolean;
  last?: boolean;
  className?: string;
}

export function AccountRow({
  icon,
  label,
  badge,
  description,
  href,
  onClick,
  toggle,
  destructive,
  grouped,
  last,
  className,
}: AccountRowProps) {
  const content = (
    <>
      <span
        aria-hidden
        className={cn(
          "inline-flex items-center justify-center w-8 h-8 rounded-[10px] shrink-0",
          "bg-[var(--bg-2)]",
          destructive ? "text-[var(--danger)]" : "text-[var(--fg-1)]",
        )}
      >
        {icon}
      </span>

      <span className="flex-1 min-w-0 flex items-center gap-2">
        <span
          className={cn(
            "truncate text-[15px]",
            destructive ? "text-[var(--danger)]" : "text-[var(--fg-0)]",
          )}
        >
          {label}
        </span>
        {badge != null && (
          <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-[var(--amber-400)]/15 text-[10px] font-medium text-[var(--amber-400)] tabular-nums">
            {badge}
          </span>
        )}
      </span>

      {description && (
        <span className="text-[12px] text-[var(--fg-2)] mr-1 shrink-0">
          {description}
        </span>
      )}

      {toggle ? (
        <ToggleSwitch
          checked={toggle.checked}
          ariaLabel={toggle.ariaLabel ?? (typeof label === "string" ? label : undefined)}
        />
      ) : (
        <ChevronRight className="w-4 h-4 text-[var(--fg-2)]/60 shrink-0" />
      )}
    </>
  );

  const baseClass = cn(
    "w-full min-h-[48px] flex items-center gap-3",
    grouped ? "px-4" : "px-4",
    !last && "border-b border-[var(--border-subtle)]",
    "text-left active:bg-[var(--bg-2)] transition-colors",
    className,
  );

  if (toggle) {
    return (
      <button
        type="button"
        onClick={() => toggle.onChange(!toggle.checked)}
        className={baseClass}
        role="group"
      >
        {content}
      </button>
    );
  }

  if (href) {
    return (
      <Link href={href} className={baseClass}>
        {content}
      </Link>
    );
  }

  return (
    <button type="button" onClick={onClick} className={baseClass}>
      {content}
    </button>
  );
}

function ToggleSwitch({
  checked,
  ariaLabel,
}: {
  checked: boolean;
  ariaLabel?: string;
}) {
  return (
    <span
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      className={cn(
        "relative inline-flex items-center shrink-0",
        "w-[46px] h-7 rounded-full transition-colors duration-200",
        checked
          ? "bg-[var(--amber-400)]"
          : "bg-[var(--bg-3)] border border-[var(--border-subtle)]",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "absolute top-[3px] left-[3px] w-[22px] h-[22px] rounded-full bg-white shadow-sm",
          "transition-transform duration-200",
          checked ? "translate-x-[18px]" : "translate-x-0",
        )}
      />
    </span>
  );
}
