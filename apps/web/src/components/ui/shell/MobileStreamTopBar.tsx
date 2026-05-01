"use client";

import { Filter, Search } from "lucide-react";
import { type ReactNode } from "react";
import { MobileTopBar } from "./MobileTopBar";

export function MobileStreamTopBar({
  compact,
  countLabel,
  onToggleSearch,
  onToggleFilter,
  searchActive,
  filterActive,
}: {
  compact: boolean;
  countLabel?: ReactNode;
  onToggleSearch: () => void;
  onToggleFilter: () => void;
  searchActive: boolean;
  filterActive: boolean;
}) {
  return (
    <MobileTopBar
      left={
        compact ? (
          <div className="min-w-0 flex items-baseline gap-2">
            <span className="font-display italic text-[17px] leading-[1.2] text-[var(--fg-0)] shrink-0">
              灵感流
            </span>
            {countLabel && (
              <span className="text-[10px] tracking-wider text-[var(--fg-2)] font-mono truncate">
                {countLabel}
              </span>
            )}
          </div>
        ) : (
          <div className="min-w-0">
            <div className="font-display italic text-[28px] leading-[1.1] text-[var(--fg-0)]">
              灵感流
            </div>
            {countLabel && (
              <div className="text-[10px] tracking-wider text-[var(--fg-2)] font-mono mt-0.5">
                {countLabel}
              </div>
            )}
          </div>
        )
      }
      right={
        <>
          <IconToggle onClick={onToggleSearch} active={searchActive} label="搜索">
            <Search className="w-4 h-4" />
          </IconToggle>
          <IconToggle onClick={onToggleFilter} active={filterActive} label="筛选">
            <Filter className="w-4 h-4" />
          </IconToggle>
        </>
      }
    />
  );
}

function IconToggle({
  onClick,
  active,
  label,
  children,
}: {
  onClick: () => void;
  active?: boolean;
  label: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-pressed={active}
      className={[
        "inline-flex items-center justify-center w-9 h-9 rounded-full",
        active
          ? "text-[var(--amber-400)] bg-[var(--accent-soft)]"
          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
      ].join(" ")}
    >
      {children}
    </button>
  );
}
