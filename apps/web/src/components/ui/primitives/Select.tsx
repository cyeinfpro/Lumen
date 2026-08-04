"use client";

import { cn } from "@/lib/utils";

export interface SelectProps
  extends React.SelectHTMLAttributes<HTMLSelectElement> {
  wrapperClassName?: string;
}

const FIELD =
  "control-shell type-body-sm h-10 w-full appearance-none px-3 pr-9 outline-none " +
  "text-[var(--fg-0)] max-sm:min-h-11 " +
  "transition-[border-color,box-shadow,background-color] duration-[var(--dur-quick)] " +
  "focus:border-accent-border focus:shadow-[var(--ring)] " +
  "disabled:cursor-not-allowed disabled:opacity-50 " +
  "[&>option]:bg-[var(--bg-1)] [&>option]:text-[var(--fg-0)]";

export function Select({
  className,
  wrapperClassName,
  children,
  ref,
  ...props
}: SelectProps & { ref?: React.Ref<HTMLSelectElement> }) {
  return (
    <div className={cn("relative min-w-0", wrapperClassName)}>
      <select
        {...props}
        ref={ref}
        className={cn(FIELD, className)}
      >
        {children}
      </select>
      <svg
        aria-hidden="true"
        className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-1)]"
        viewBox="0 0 16 16"
        fill="none"
      >
        <path
          d="m4 6 4 4 4-4"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.5"
        />
      </svg>
    </div>
  );
}
