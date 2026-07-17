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
  "control-shell type-body-sm h-10 w-full px-3 outline-none " +
  "max-sm:min-h-11 max-sm:text-base max-sm:leading-6 " +
  "text-[var(--fg-0)] placeholder:text-[var(--fg-2)] " +
  "transition-[border-color,box-shadow,background-color] duration-150 " +
  "hover:bg-[var(--bg-1)]/82 " +
  "focus:bg-[var(--bg-1)]/88 " +
  "focus:border-[var(--accent)]/60 focus:ring-2 focus:ring-[var(--accent)]/20 " +
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
  "aria-describedby": ariaDescribedBy,
  ref,
  ...props
}: InputProps & { ref?: React.Ref<HTMLInputElement> }) {
  const reactId = useId();
  const inputId = id ?? reactId;
  const hintId = hint ? `${inputId}-hint` : undefined;
  const errorId = error ? `${inputId}-err` : undefined;
  const describedBy =
    [ariaDescribedBy, errorId, hintId].filter(Boolean).join(" ") || undefined;
  const isInvalid = invalid || !!error;

  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", wrapperClassName)}>
      {label ? (
        <label
          htmlFor={inputId}
          className="type-label"
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
              "border-[var(--danger)]/60 focus:border-[var(--danger)] focus:ring-danger/20",
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
          id={errorId}
          role="alert"
          className="type-caption text-[var(--danger-fg)]"
        >
          {error}
        </p>
      ) : null}
      {hint ? (
        <p
          id={hintId}
          className="type-caption text-[var(--text-muted)]"
        >
          {hint}
        </p>
      ) : null}
    </div>
  );
}
