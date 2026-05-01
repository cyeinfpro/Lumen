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
        "inline-flex items-center justify-center min-w-[18px] h-[18px] px-1",
        "text-[11px] font-mono tracking-tight",
        "bg-white/[0.06] text-[var(--fg-0)]",
        "border border-white/[0.1] border-b-white/[0.08]",
        "rounded-[4px] shadow-[0_1px_0_0_rgba(255,255,255,0.04)_inset,0_1px_0_0_rgba(0,0,0,0.25)]",
        className,
      )}
      {...props}
    >
      {children}
    </kbd>
  );
}

export default Kbd;
