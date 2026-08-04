"use client";

import { cn } from "@/lib/utils";

export type MediaControlButtonSize = "sm" | "md" | "lg";

export interface MediaControlButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  size?: MediaControlButtonSize;
  children?: React.ReactNode;
  "aria-label": string;
}

const SIZES: Record<MediaControlButtonSize, string> = {
  sm: "h-8 w-8 max-sm:min-h-11 max-sm:min-w-11",
  md: "h-10 w-10 max-sm:min-h-11 max-sm:min-w-11",
  lg: "h-11 w-11",
};

export function MediaControlButton({
  size = "md",
  className,
  disabled,
  children,
  type,
  ref,
  ...props
}: MediaControlButtonProps & { ref?: React.Ref<HTMLButtonElement> }) {
  return (
    // @hit-area-ok: desktop media overlays stay compact; mobile sizes expand to 44px.
    <button
      {...props}
      ref={ref}
      type={type ?? "button"}
      disabled={disabled}
      data-lumen-interactive={disabled ? undefined : "tight"}
      className={cn(
        "inline-flex shrink-0 items-center justify-center rounded-full " +
          "bg-[var(--media-control-bg)] text-[var(--media-control-fg)] " +
          "touch-manipulation transition-[filter,opacity,transform] duration-[var(--dur-quick)] " +
          "hover:brightness-110 focus-visible:outline-none " +
          "focus-visible:shadow-[var(--ring)] " +
          "disabled:cursor-not-allowed disabled:opacity-50",
        SIZES[size],
        className,
      )}
    >
      {children}
    </button>
  );
}
