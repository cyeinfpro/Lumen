"use client";

import { AlertTriangle, LogIn, RefreshCw } from "lucide-react";
import { useState } from "react";

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
  if (status.session === "revalidating") {
    return "正在验证会话";
  }
  return "";
}

function useSessionRecovery(status: RuntimeResilienceSnapshot["session"]) {
  const [recovery, setRecovery] = useState({ requested: false, status });
  if (recovery.status !== status) {
    setRecovery({ requested: recovery.requested && status === "revalidating", status });
  }
  const requested = recovery.requested;
  return {
    // Initial/background verification stays quiet; explicit recovery retains focus.
    revalidating: requested && status === "revalidating",
    recover: () => {
      if (status === "revalidating") return;
      if (status === "unauthorized") { replaceWithLogin(); return; }
      setRecovery({ requested: true, status });
      requestRuntimeRecovery();
    },
  };
}

export function RuntimeResilienceStatus() {
  const status = useRuntimeResilience();
  const unauthorized = status.session === "unauthorized";
  const sessionDegraded = status.session === "degraded";
  const { revalidating, recover } = useSessionRecovery(status.session);
  if (!unauthorized && !sessionDegraded && !revalidating) return null;

  const message = runtimeStatusMessage(status);
  const Icon = unauthorized ? LogIn : AlertTriangle;
  const ActionIcon = unauthorized ? LogIn : RefreshCw;
  const actionLabel = revalidating ? "验证中，请稍候" : unauthorized ? "登录" : "重新验证会话";

  return (
    <div
      role={revalidating ? "status" : "alert"}
      aria-live={revalidating ? "polite" : "assertive"}
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
          onClick={recover}
          aria-busy={revalidating || undefined}
          aria-disabled={revalidating || undefined}
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
  const { revalidating, recover } = useSessionRecovery(status.session);
  if (!unauthorized && !sessionDegraded && !revalidating) return null;

  const message = runtimeStatusMessage(status);
  const Icon = revalidating ? RefreshCw : unauthorized ? LogIn : AlertTriangle;
  const actionLabel = revalidating ? "验证中，请稍候" : unauthorized ? "登录" : "重新验证会话";
  const accessibleLabel = `${message}，${actionLabel}`;

  return (
    <div
      role={revalidating ? "status" : "alert"}
      aria-live={revalidating ? "polite" : "assertive"}
      aria-atomic="true"
      data-runtime-resilience-status="mobile"
      className="flex shrink-0"
    >
      <span className="sr-only">{message}</span>
      <IconButton
        size="md"
        variant="ghost"
        onClick={recover}
        aria-busy={revalidating || undefined}
        aria-disabled={revalidating || undefined}
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
