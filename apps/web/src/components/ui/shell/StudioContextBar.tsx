"use client";

import { Images, MessageSquareText, Zap } from "lucide-react";
import type { ComponentProps } from "react";

import { ConversationMemoryButton } from "@/components/ui/chat/ConversationMemoryButton";
import { ContextWindowMeter } from "@/components/ui/chat/ContextWindowMeter";
import { SystemPromptManager } from "@/components/ui/SystemPromptManager";
import { cn } from "@/lib/utils";

type ContextStats = ComponentProps<typeof ContextWindowMeter>["stats"];

export function StudioContextBar({
  title,
  view,
  onViewChange,
  fast,
  onFastChange,
  contextStats,
}: {
  title: string;
  view: "chat" | "images";
  onViewChange: (view: "chat" | "images") => void;
  fast: boolean;
  onFastChange: (next: boolean) => void;
  contextStats: ContextStats;
}) {
  return (
    <div className="flex h-11 shrink-0 items-center gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 md:px-4">
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] font-medium text-[var(--fg-0)]">
          {title}
        </p>
      </div>

      <div
        role="tablist"
        aria-label="会话视图"
        className="flex shrink-0 items-center rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-0.5"
      >
        <ViewButton
          active={view === "chat"}
          label="对话"
          onClick={() => onViewChange("chat")}
          icon={<MessageSquareText className="h-3.5 w-3.5" aria-hidden />}
        />
        <ViewButton
          active={view === "images"}
          label="图片"
          onClick={() => onViewChange("images")}
          icon={<Images className="h-3.5 w-3.5" aria-hidden />}
        />
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        <button
          type="button"
          onClick={() => onFastChange(!fast)}
          aria-pressed={fast}
          aria-label={fast ? "关闭快速模式" : "开启快速模式"}
          className={cn(
            "inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-control)] border px-2.5 text-[11px] font-medium",
            "transition-colors duration-[var(--dur-quick)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
            fast
              ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
              : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
          )}
        >
          <Zap className="h-3.5 w-3.5" fill={fast ? "currentColor" : "none"} aria-hidden />
          <span className="hidden xl:inline">Fast</span>
        </button>
        <ContextWindowMeter stats={contextStats} compact />
        <ConversationMemoryButton compact />
        <SystemPromptManager compact />
      </div>
    </div>
  );
}

function ViewButton({
  active,
  label,
  icon,
  onClick,
}: {
  active: boolean;
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-[calc(var(--radius-control)-2px)] px-2.5 text-[11px] font-medium",
        "transition-colors duration-[var(--dur-quick)] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
        active
          ? "bg-[var(--surface-selected)] text-[var(--fg-0)]"
          : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      {icon}
      <span className="hidden lg:inline">{label}</span>
    </button>
  );
}
