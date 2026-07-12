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
  const displayBadge =
    typeof badge === "number" && Number.isFinite(badge)
      ? badge > 99
        ? "99+"
        : badge
      : badge;
  const content = (
    <>
      <span
        aria-hidden
        className={cn(
          "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-card)]",
          "bg-[var(--bg-2)]",
          destructive ? "text-[var(--danger)]" : "text-[var(--fg-1)]",
        )}
      >
        {icon}
      </span>

      <span className="flex min-w-0 flex-1 items-center gap-2">
        <span
          className={cn(
            "min-w-0 break-words text-[15px] leading-snug",
            destructive ? "text-[var(--danger)]" : "text-[var(--fg-0)]",
          )}
        >
          {label}
        </span>
        {displayBadge != null && (
          <span className="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-[var(--amber-400)]/15 text-[10px] font-medium text-[var(--amber-400)] tabular-nums">
            {displayBadge}
          </span>
        )}
      </span>

      {description && (
        <span className="mr-1 max-w-[42%] shrink-0 break-all text-right text-[12px] text-[var(--fg-2)]">
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
    "flex min-h-[52px] w-full items-center gap-3 py-2",
    grouped ? "px-4" : "px-4",
    !last && "border-b border-[var(--border-subtle)]",
    "text-left active:bg-[var(--bg-2)] transition-colors",
    className,
  );

  if (toggle) {
    /* 全宽设置行：跨 icon/label/badge/toggle 多列布局，不匹配标准 Button */
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

  /* 全宽设置行：同上 */
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
