"use client";

import { AlertTriangle, LogIn, RefreshCw } from "lucide-react";

import { replaceWithLogin } from "@/lib/auth/navigation";
import {
  requestRuntimeRecovery,
  type RuntimeResilienceSnapshot,
  useRuntimeResilience,
} from "@/lib/runtimeResilience";
import { IconButton } from "@/components/ui/primitives";

function runtimeStatusMessage(status: RuntimeResilienceSnapshot): string {
  if (status.session === "unauthorized") {
    return "会话已失效";
  }
  if (status.session === "degraded") {
    return "会话验证暂不可用";
  }
  return "";
}

function recoverRuntimeSession(unauthorized: boolean) {
  if (unauthorized) {
    replaceWithLogin();
    return;
  }
  requestRuntimeRecovery();
}

export function RuntimeResilienceStatus() {
  const status = useRuntimeResilience();
  const unauthorized = status.session === "unauthorized";
  const sessionDegraded = status.session === "degraded";
  if (!unauthorized && !sessionDegraded) return null;

  const message = runtimeStatusMessage(status);
  const Icon = unauthorized ? LogIn : AlertTriangle;
  const ActionIcon = unauthorized ? LogIn : RefreshCw;
  const actionLabel = unauthorized ? "登录" : "重新验证会话";

  return (
    <div
      role="alert"
      aria-live="assertive"
      aria-atomic="true"
      data-runtime-resilience-status="desktop"
      className="pointer-events-none fixed bottom-4 right-4 z-[var(--z-toast)] hidden max-w-[min(20rem,calc(100vw-2rem))] md:block"
    >
      <div
        className={[
          "pointer-events-auto flex items-center gap-2 rounded-[var(--radius-control)] border px-2.5 py-1.5 type-caption shadow-[var(--shadow-1)] backdrop-blur-xl",
          unauthorized
            ? "border-danger-border bg-danger-soft/95 text-[var(--danger-fg)]"
            : "border-warning-border bg-warning-soft/95 text-[var(--warning-fg)]",
        ].join(" ")}
      >
        <Icon className="h-4 w-4 shrink-0" aria-hidden />
        <span className="min-w-0 flex-1">{message}</span>
        <IconButton
          size="sm"
          variant="ghost"
          onClick={() => recoverRuntimeSession(unauthorized)}
          aria-label={actionLabel}
          title={actionLabel}
          tooltip={actionLabel}
          className="text-current hover:bg-[var(--bg-2)] hover:text-current"
        >
          <ActionIcon className="h-4 w-4" aria-hidden />
        </IconButton>
      </div>
    </div>
  );
}

export function MobileRuntimeResilienceStatus() {
  const status = useRuntimeResilience();
  const unauthorized = status.session === "unauthorized";
  const sessionDegraded = status.session === "degraded";
  if (!unauthorized && !sessionDegraded) return null;

  const message = runtimeStatusMessage(status);
  const Icon = unauthorized ? LogIn : AlertTriangle;
  const actionLabel = unauthorized ? "登录" : "重新验证会话";
  const accessibleLabel = `${message}，${actionLabel}`;

  return (
    <div
      role="alert"
      aria-live="assertive"
      aria-atomic="true"
      data-runtime-resilience-status="mobile"
      className="flex shrink-0"
    >
      <span className="sr-only">{message}</span>
      <IconButton
        size="md"
        variant="ghost"
        onClick={() => recoverRuntimeSession(unauthorized)}
        aria-label={accessibleLabel}
        title={accessibleLabel}
        tooltip={accessibleLabel}
        className={
          unauthorized
            ? "border border-danger-border bg-danger-soft text-[var(--danger-fg)] hover:bg-danger-soft hover:text-[var(--danger-fg)]"
            : "border border-warning-border bg-warning-soft text-[var(--warning-fg)] hover:bg-warning-soft hover:text-[var(--warning-fg)]"
        }
      >
        <Icon className="h-4 w-4" aria-hidden />
      </IconButton>
    </div>
  );
}
