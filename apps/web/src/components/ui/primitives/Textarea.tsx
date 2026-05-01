"use client";

// 多行文本输入。与 Input 共享视觉语言，但高度 auto，min/max rows 由外部控制。

import { useId } from "react";
import { cn } from "@/lib/utils";

interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
  error?: string;
  label?: string;
  hint?: string;
  wrapperClassName?: string;
}

// iOS Safari focus 缩放规避：移动端字号升到 16px。
const FIELD =
  "w-full rounded-md px-3 py-2 text-sm leading-relaxed " +
  "max-sm:text-base " +
  "bg-[var(--bg-1)]/60 text-[var(--fg-0)] placeholder:text-[var(--fg-1)]/70 " +
  "border border-[var(--border)] resize-y " +
  "transition-[border-color,box-shadow,background-color] duration-150 " +
  "hover:bg-[var(--bg-1)]/75 " +
  "focus:outline-none focus:bg-[var(--bg-1)]/75 " +
  "focus:border-[var(--accent)]/60 focus:shadow-[0_0_0_3px_rgba(242,169,58,0.18)] " +
  "disabled:opacity-50 disabled:cursor-not-allowed";

export function Textarea({
  invalid,
  error,
  label,
  hint,
  wrapperClassName,
  className,
  id,
  rows = 3,
  ref,
  ...props
}: TextareaProps & { ref?: React.Ref<HTMLTextAreaElement> }) {
  const reactId = useId();
  const fieldId = id ?? reactId;
  const describedBy = error ? `${fieldId}-err` : hint ? `${fieldId}-hint` : undefined;
  const isInvalid = invalid || !!error;

  return (
    <div className={cn("flex flex-col gap-1", wrapperClassName)}>
      {label ? (
        <label
          htmlFor={fieldId}
          className="text-xs font-medium text-[var(--fg-1)]"
        >
          {label}
        </label>
      ) : null}
      <textarea
        ref={ref}
        id={fieldId}
        rows={rows}
        aria-invalid={isInvalid || undefined}
        aria-describedby={describedBy}
        className={cn(
          FIELD,
          isInvalid &&
            "border-[var(--danger)]/60 focus:border-[var(--danger)] focus:shadow-[0_0_0_3px_rgba(229,72,77,0.18)]",
          className,
        )}
        {...props}
      />
      {error ? (
        <p id={`${fieldId}-err`} className="text-[11px] text-[var(--danger)]">
          {error}
        </p>
      ) : hint ? (
        <p id={`${fieldId}-hint`} className="text-[11px] text-[var(--fg-1)]/80">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export default Textarea;
