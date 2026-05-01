"use client";

// 错误态：红色强调 + 图标 + 标题 + 描述 + 可选 retry。
// 作为 ErrorBoundary / error.tsx / 查询失败的统一展示层。

import { AlertTriangle, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "./Button";

interface ErrorStateProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  icon?: React.ReactNode;
  title: React.ReactNode;
  description?: React.ReactNode;
  /** 错误原文 / digest。独占一行等宽字体显示。 */
  detail?: React.ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
  /** 右侧次要操作（例如"返回首页"） */
  secondaryAction?: React.ReactNode;
  tone?: "danger" | "warning";
}

export function ErrorState({
  icon,
  title,
  description,
  detail,
  onRetry,
  retryLabel = "重试",
  secondaryAction,
  tone = "danger",
  className,
  ref,
  ...props
}: ErrorStateProps & { ref?: React.Ref<HTMLDivElement> }) {
  const isWarn = tone === "warning";
  return (
    <div
      ref={ref}
      role="alert"
      className={cn(
        "flex flex-col items-center justify-center text-center px-6 py-10",
        "rounded-xl border backdrop-blur-sm",
        isWarn
          ? "border-[var(--warning)]/25 bg-[var(--warning-soft)]"
          : "border-[var(--danger)]/25 bg-[var(--danger-soft)]",
        className,
      )}
      {...props}
    >
      <div
        className={cn(
          "flex items-center justify-center w-12 h-12 rounded-full mb-3 border",
          isWarn
            ? "bg-[var(--warning-soft)] border-[var(--warning)]/30 text-[var(--warning)]"
            : "bg-[var(--danger-soft)] border-[var(--danger)]/30 text-[var(--danger)]",
        )}
      >
        {icon ?? <AlertTriangle className="w-5 h-5" aria-hidden="true" />}
      </div>
      <h3 className="text-[15px] font-medium tracking-tight mb-1 text-[var(--fg-0)] text-balance">
        {title}
      </h3>
      {description ? (
        <p className="text-xs text-[var(--fg-1)] leading-relaxed max-w-sm text-pretty">
          {description}
        </p>
      ) : null}
      {detail ? (
        <p className="mt-3 text-[11px] font-mono text-[var(--fg-1)] break-words max-w-md px-3 py-1.5 rounded-md bg-black/20 border border-white/5">
          {detail}
        </p>
      ) : null}
      {(onRetry || secondaryAction) && (
        // 移动端纵向堆叠避免按钮被挤压；桌面横排 wrap
        <div className="mt-4 flex flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-center sm:flex-wrap">
          {onRetry ? (
            <Button
              variant="primary"
              size="sm"
              onClick={onRetry}
              leftIcon={<RefreshCw className="w-3.5 h-3.5" />}
            >
              {retryLabel}
            </Button>
          ) : null}
          {secondaryAction}
        </div>
      )}
    </div>
  );
}

export default ErrorState;
