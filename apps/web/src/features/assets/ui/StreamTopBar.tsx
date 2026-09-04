"use client";

import { MobileTopBar } from "@/components/ui/shell/MobileTopBar";

export interface StreamTopBarProps {
  compact: boolean;
}

export function StreamTopBar({ compact }: StreamTopBarProps) {
  return (
    <MobileTopBar
      left={
        <span className={compact ? "type-card-title" : "type-page-title"}>
          素材
        </span>
      }
    />
  );
}
