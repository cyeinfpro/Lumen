"use client";

import type { ReactNode } from "react";
import {
  ArrowUp,
  Loader2,
  MessageSquare,
  Palette,
} from "lucide-react";

import {
  SegmentedControl,
} from "@/components/ui/primitives/mobile";
import { Pressable } from "@/components/ui/primitives/mobile/Pressable";
import type { ComposerMode } from "@/store/chat/types";
import { cn } from "@/lib/utils";

export function IconBtn({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={cn(
        "relative shrink-0 inline-flex min-h-11 min-w-11 items-center justify-center rounded-[var(--radius-control)]",
        "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-2)]",
        "active:opacity-[var(--op-press)] transition-[background-color,color,opacity] duration-[var(--dur-quick)]",
        "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
        "disabled:opacity-40 disabled:cursor-not-allowed",
      )}
    >
      {children}
    </button>
  );
}

export function SendButton({
  canSubmit,
  isSending,
  burst,
  onClick,
  size = "lg",
}: {
  canSubmit: boolean;
  isSending: boolean;
  burst?: boolean;
  onClick: () => void;
  size?: "md" | "lg";
}) {
  const dim = size === "lg" ? "w-10 h-10" : "w-9 h-9";
  const isActive = canSubmit || isSending;
  return (
    <Pressable
      size="inline"
      minHit={false}
      pressScale="tight"
      haptic={false}
      onPress={onClick}
      disabled={!canSubmit}
      aria-label="发送"
      aria-busy={isSending || undefined}
      className={cn(
        "shrink-0 inline-flex items-center justify-center rounded-full",
        dim,
        "transition-[background-color,box-shadow,opacity] duration-[var(--dur-normal)]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/70",
        isActive
          ? [
              "bg-[var(--amber-400)] text-[var(--bg-0)]",
              burst
                ? "shadow-[var(--shadow-amber)]"
                : "shadow-[var(--shadow-1)]",
            ].join(" ")
          : "bg-[var(--bg-3)] text-[var(--fg-3)] cursor-not-allowed",
      )}
    >
      {isSending ? (
        <Loader2 className="w-4 h-4 animate-spin" aria-hidden />
      ) : (
        <ArrowUp className="w-4 h-4" aria-hidden />
      )}
    </Pressable>
  );
}

export function ModeSegment({
  value,
  onChange,
}: {
  value: ComposerMode;
  onChange: (value: ComposerMode) => void;
}) {
  return (
    <div className="shrink-0">
      <SegmentedControl<ComposerMode>
        value={value}
        onChange={onChange}
        ariaLabel="模式"
        density="compact"
        items={[
          {
            value: "chat",
            label: (
              <span className="inline-flex items-center gap-1.5">
                <MessageSquare className="w-3.5 h-3.5" aria-hidden />
                <span>对话</span>
              </span>
            ),
          },
          {
            value: "image",
            label: (
              <span className="inline-flex items-center gap-1.5">
                <Palette className="w-3.5 h-3.5" aria-hidden />
                <span>生图</span>
              </span>
            ),
          },
        ]}
      />
    </div>
  );
}
