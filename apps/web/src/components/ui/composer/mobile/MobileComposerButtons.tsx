"use client";

import {
  ArrowUp,
  Loader2,
  MessageSquare,
  Palette,
} from "lucide-react";

import { Pressable } from "@/components/ui/primitives/mobile/Pressable";
import { SegmentedControl } from "@/components/ui/primitives/mobile";
import { IconButton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export type MobileComposerMode = "chat" | "image";

export function MobileComposerIconButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <IconButton
      size="lg"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      tooltip={label}
      className={cn(
        "relative",
        "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-[var(--bg-2)]",
        "active:opacity-[var(--op-press)] motion-reduce:transition-none",
      )}
    >
      {children}
    </IconButton>
  );
}

export function MobileComposerSendButton({
  canSubmit,
  isSending,
  burst,
  onClick,
}: {
  canSubmit: boolean;
  isSending: boolean;
  burst?: boolean;
  onClick: () => void;
}) {
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
        "shrink-0 inline-flex min-h-11 min-w-11 items-center justify-center rounded-full",
        "transition-[background-color,box-shadow,opacity] duration-200 motion-reduce:transition-none",
        "focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
        isActive
          ? [
              "bg-accent text-[var(--accent-on)]",
              burst
                ? "shadow-[var(--shadow-amber)]"
                : "shadow-[var(--shadow-1)]",
            ].join(" ")
          : "bg-[var(--bg-3)] text-[var(--fg-3)] cursor-not-allowed",
      )}
    >
      {isSending ? (
        <Loader2 className="w-[18px] h-[18px] animate-spin" aria-hidden />
      ) : (
        <ArrowUp className="w-[18px] h-[18px]" aria-hidden />
      )}
    </Pressable>
  );
}

export function MobileComposerModeSegment({
  value,
  onChange,
  className,
}: {
  value: MobileComposerMode;
  onChange: (value: MobileComposerMode) => void;
  className?: string;
}) {
  return (
    <SegmentedControl<MobileComposerMode>
      value={value}
      onChange={onChange}
      ariaLabel="模式"
      className={className}
      items={[
        {
          value: "chat",
          label: (
            <>
              <MessageSquare className="w-3.5 h-3.5 shrink-0" aria-hidden />
              <span>对话</span>
            </>
          ),
        },
        {
          value: "image",
          label: (
            <>
              <Palette className="w-3.5 h-3.5 shrink-0" aria-hidden />
              <span>生图</span>
            </>
          ),
        },
      ]}
    />
  );
}
