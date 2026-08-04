"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";

export interface SwitchProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "onChange"> {
  checked?: boolean;
  defaultChecked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
}

const TRACK =
  "absolute left-1/2 top-1/2 h-6 w-11 -translate-x-1/2 -translate-y-1/2 rounded-full " +
  "transition-colors duration-[var(--dur-quick)]";

export function Switch({
  checked,
  defaultChecked = false,
  onCheckedChange,
  className,
  disabled,
  onClick,
  ref,
  ...props
}: SwitchProps & { ref?: React.Ref<HTMLButtonElement> }) {
  const [uncontrolledChecked, setUncontrolledChecked] = useState(defaultChecked);
  const isChecked = checked ?? uncontrolledChecked;

  const handleClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    const nextChecked = !isChecked;
    if (checked === undefined) {
      setUncontrolledChecked(nextChecked);
    }
    onCheckedChange?.(nextChecked);
    onClick?.(event);
  };

  return (
    <button
      {...props}
      ref={ref}
      type="button"
      role="switch"
      aria-checked={isChecked}
      disabled={disabled}
      data-state={isChecked ? "checked" : "unchecked"}
      data-lumen-interactive={disabled ? undefined : "soft"}
      onClick={handleClick}
      className={cn(
        "relative inline-flex h-6 w-11 min-w-11 items-center justify-center rounded-full " +
          "touch-manipulation transition-[box-shadow,opacity] duration-[var(--dur-quick)] " +
          "focus-visible:outline-none focus-visible:shadow-[var(--ring)] " +
          "disabled:cursor-not-allowed disabled:opacity-50 " +
          "max-sm:min-h-11 max-sm:min-w-11",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(TRACK, isChecked ? "bg-accent" : "bg-[var(--bg-3)]")}
      />
      <span
        aria-hidden="true"
        className={cn(
          "relative h-5 w-5 shrink-0 rounded-full bg-[var(--fg-0)] " +
            "transition-transform duration-[var(--dur-quick)]",
          isChecked ? "translate-x-2.5" : "-translate-x-2.5",
        )}
      />
    </button>
  );
}
