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
        "flex flex-col items-center justify-center px-6 py-10 text-center",
        "rounded-[var(--radius-card)] border backdrop-blur-sm",
        isWarn
          ? "border-[var(--warning)]/25 bg-[var(--warning-soft)]"
          : "border-[var(--danger)]/25 bg-[var(--danger-soft)]",
        className,
      )}
      {...props}
    >
      <div
        className={cn(
          "mb-3 flex h-12 w-12 items-center justify-center rounded-[var(--radius-card)] border",
          isWarn
            ? "bg-[var(--warning-soft)] border-[var(--warning)]/30 text-[var(--warning)]"
            : "bg-[var(--danger-soft)] border-[var(--danger)]/30 text-[var(--danger)]",
        )}
      >
        {icon ?? <AlertTriangle className="w-5 h-5" aria-hidden="true" />}
      </div>
      <h3 className="type-card-title mb-1 text-balance">
        {title}
      </h3>
      {description ? (
        <p className="type-body-sm max-w-sm text-pretty text-[var(--fg-1)]">
          {description}
        </p>
      ) : null}
      {detail ? (
        <p className="mt-3 max-w-md break-words rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]/70 px-3 py-1.5 font-mono text-[11px] text-[var(--fg-1)]">
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
