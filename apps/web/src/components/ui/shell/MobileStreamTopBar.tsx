"use client";

import { Filter, Search } from "lucide-react";
import { type ReactNode } from "react";
import { MobileTopBar } from "./MobileTopBar";
import { Pressable } from "@/components/ui/primitives/mobile/Pressable";

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
          <div className="min-w-0 flex items-baseline gap-2 overflow-hidden">
            <span className="type-card-title shrink-0">
              素材
            </span>
            {countLabel && (
              <span className="type-caption truncate text-[var(--fg-2)]">
                {countLabel}
              </span>
            )}
          </div>
        ) : (
          <div className="min-w-0 overflow-hidden">
            <div className="type-page-title">
              素材
            </div>
            {countLabel && (
              <div className="mt-0.5 truncate type-caption text-[var(--fg-2)]">
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
    <Pressable
      size="default"
      minHit={true}
      pressScale="tight"
      haptic="light"
      onPress={onClick}
      aria-label={label}
      aria-pressed={active}
      className={[
        "h-11 w-11 rounded-full",
        active
          ? "bg-accent-soft text-accent"
          : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
      ].join(" ")}
    >
      {children}
    </Pressable>
  );
}
