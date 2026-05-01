"use client";

// 受控/非受控 text input。统一高度 h-9、玻璃背景、focus amber ring。
// invalid=true 时边框走 danger；error 字符串会渲染到底部一行。leftIcon / rightSlot 可选。

import { useId } from "react";
import { cn } from "@/lib/utils";

interface InputProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "size"> {
  invalid?: boolean;
  error?: string;
  label?: string;
  hint?: string;
  leftIcon?: React.ReactNode;
  rightSlot?: React.ReactNode;
  /** 容器额外 className（不是 input 本身） */
  wrapperClassName?: string;
}

// iOS Safari 在 focus 到 font-size<16px 的 input 时会强制 zoom。
// 故移动端文字升到 16px；同时 min-h-11 兜底 44px 可点区域。
const FIELD =
  "h-9 w-full rounded-md px-3 text-sm " +
  "max-sm:text-base max-sm:min-h-11 " +
  "bg-[var(--bg-1)]/60 text-[var(--fg-0)] placeholder:text-[var(--fg-1)]/70 " +
  "border border-[var(--border)] " +
  "transition-[border-color,box-shadow,background-color] duration-150 " +
  "hover:bg-[var(--bg-1)]/75 " +
  "focus:outline-none focus:bg-[var(--bg-1)]/75 " +
  "focus:border-[var(--accent)]/60 focus:shadow-[0_0_0_3px_rgba(242,169,58,0.18)] " +
  "disabled:opacity-50 disabled:cursor-not-allowed";

export function Input({
  invalid,
  error,
  label,
  hint,
  leftIcon,
  rightSlot,
  wrapperClassName,
  className,
  id,
  ref,
  ...props
}: InputProps & { ref?: React.Ref<HTMLInputElement> }) {
  const reactId = useId();
  const inputId = id ?? reactId;
  const describedBy = error ? `${inputId}-err` : hint ? `${inputId}-hint` : undefined;
  const isInvalid = invalid || !!error;

  return (
    <div className={cn("flex flex-col gap-1", wrapperClassName)}>
      {label ? (
        <label
          htmlFor={inputId}
          className="text-xs font-medium text-[var(--fg-1)]"
        >
          {label}
        </label>
      ) : null}
      <div className={cn("relative flex items-center")}>
        {leftIcon ? (
          <span className="absolute left-2.5 text-[var(--fg-1)] pointer-events-none flex items-center">
            {leftIcon}
          </span>
        ) : null}
        <input
          ref={ref}
          id={inputId}
          aria-invalid={isInvalid || undefined}
          aria-describedby={describedBy}
          className={cn(
            FIELD,
            leftIcon && "pl-9",
            rightSlot && "pr-9",
            isInvalid &&
              "border-[var(--danger)]/60 focus:border-[var(--danger)] focus:shadow-[0_0_0_3px_rgba(229,72,77,0.18)]",
            className,
          )}
          {...props}
        />
        {rightSlot ? (
          <span className="absolute right-2 flex items-center text-[var(--fg-1)]">
            {rightSlot}
          </span>
        ) : null}
      </div>
      {error ? (
        <p
          id={`${inputId}-err`}
          className="text-[11px] text-[var(--danger)]"
        >
          {error}
        </p>
      ) : hint ? (
        <p
          id={`${inputId}-hint`}
          className="text-[11px] text-[var(--fg-1)]/80"
        >
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export default Input;
