"use client";

import { ArrowDown } from "lucide-react";
import { IconButton } from "@/components/ui/primitives";

export function AgentScrollToLatest({
  visible,
  onClick,
  className,
  style,
}: {
  visible: boolean;
  onClick: () => void;
  className?: string;
  style?: React.CSSProperties;
}) {
  if (!visible) return null;
  return (
    <div className={className} style={style}>
      <IconButton
        size="md"
        variant="secondary"
        onClick={onClick}
        aria-label="跳到最新回复"
        tooltip="跳到最新回复"
        className="shadow-[var(--shadow-2)]"
      >
        <ArrowDown className="h-4 w-4" aria-hidden />
      </IconButton>
    </div>
  );
}
