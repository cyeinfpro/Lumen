"use client";

import { AlertTriangle, LogIn, RefreshCw } from "lucide-react";

import {
  requestRuntimeRecovery,
  type RuntimeResilienceSnapshot,
  useRuntimeResilience,
} from "@/lib/runtimeResilience";
import { replaceWithLogin } from "@/lib/auth/navigation";

function runtimeStatusMessage(status: RuntimeResilienceSnapshot): string {
  if (status.session === "unauthorized") {
    return "会话已失效";
  }
  if (status.session === "degraded") {
    return "会话验证暂不可用";
  }
  return "";
}

export function RuntimeResilienceStatus() {
  const status = useRuntimeResilience();
  const unauthorized = status.session === "unauthorized";
  const sessionDegraded = status.session === "degraded";
  if (!unauthorized && !sessionDegraded) return null;

  const message = runtimeStatusMessage(status);
  const Icon = unauthorized ? LogIn : AlertTriangle;
  const ActionIcon = unauthorized ? LogIn : RefreshCw;

  const recover = () => {
    if (unauthorized) {
      replaceWithLogin();
      return;
    }
    requestRuntimeRecovery();
  };

  return (
    <div
      role="alert"
      aria-live="assertive"
      data-runtime-resilience-status
      className="pointer-events-none fixed right-3 top-[calc(var(--mobile-topbar-h)+var(--top-banner-stack-height,0px)+env(safe-area-inset-top,0px)+var(--space-2))] z-[var(--z-toast)] max-w-[min(20rem,calc(100vw-1.5rem))] md:bottom-4 md:left-auto md:right-4 md:top-auto"
    >
      <div
        className={
          "pointer-events-auto flex items-center gap-2 rounded-[var(--radius-control)] border border-warning-border bg-warning-soft/95 px-2.5 py-1.5 type-caption text-[var(--warning-fg)] shadow-[var(--shadow-1)] backdrop-blur-xl"
        }
      >
        <Icon
          className="h-4 w-4 shrink-0"
          aria-hidden
        />
        <span className="min-w-0 flex-1 truncate">{message}</span>
        <button
          type="button"
          onClick={recover}
          aria-label={unauthorized ? "登录" : "重新验证会话"}
          title={unauthorized ? "登录" : "重新验证会话"}
          className="inline-flex h-8 min-h-11 w-8 min-w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-0)] transition-colors hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-[var(--focus-outline)] md:min-h-8 md:min-w-8"
        >
          <ActionIcon className="h-4 w-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}
