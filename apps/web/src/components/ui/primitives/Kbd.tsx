"use client";

// 键盘提示 chip。语义 <kbd>；用于 shortcut 展示（⌘ / Enter / J 等单键）。

import { cn } from "@/lib/utils";

type KbdProps = React.HTMLAttributes<HTMLElement>;

export function Kbd({
  className,
  children,
  ref,
  ...props
}: KbdProps & { ref?: React.Ref<HTMLElement> }) {
  return (
    <kbd
      ref={ref}
      className={cn(
        "inline-flex h-[18px] min-w-[18px] items-center justify-center px-1",
        "font-mono text-[11px]",
        "bg-[var(--bg-2)] text-[var(--fg-0)]",
        "border border-[var(--border)]",
        "rounded-[var(--radius-sm)] shadow-[var(--shadow-1)]",
        className,
      )}
      {...props}
    >
      {children}
    </kbd>
  );
}
