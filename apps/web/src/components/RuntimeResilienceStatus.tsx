"use client";

import { AlertTriangle, LogIn, RefreshCw, WifiOff } from "lucide-react";

import {
  requestRuntimeRecovery,
  type RuntimeResilienceSnapshot,
  useRuntimeResilience,
} from "@/lib/runtimeResilience";

function sessionIsHealthy(status: RuntimeResilienceSnapshot): boolean {
  return status.session === "authenticated" || status.session === "public";
}

function realtimeIsHealthy(status: RuntimeResilienceSnapshot): boolean {
  return status.realtime === "open" || status.realtime === "idle";
}

function runtimeStatusMessage(status: RuntimeResilienceSnapshot): string {
  if (status.session === "unauthorized") {
    return "会话已失效，高风险操作已停止。";
  }
  if (status.session === "degraded") {
    return "会话验证暂不可用，高风险操作已切换为只读。";
  }
  if (status.realtime === "error" || status.realtime === "closed") {
    return "实时连接已中断，任务状态正通过快照恢复。";
  }
  return status.session === "revalidating"
    ? "正在确认会话状态。"
    : "正在建立实时连接。";
}

export function RuntimeResilienceStatus() {
  const status = useRuntimeResilience();
  if (sessionIsHealthy(status) && realtimeIsHealthy(status)) return null;

  const unauthorized = status.session === "unauthorized";
  const sessionDegraded = status.session === "degraded";
  const realtimeDegraded =
    status.realtime === "error" || status.realtime === "closed";
  const urgent = unauthorized || sessionDegraded || realtimeDegraded;
  const message = runtimeStatusMessage(status);
  const Icon = unauthorized
    ? LogIn
    : realtimeDegraded
      ? WifiOff
      : urgent
        ? AlertTriangle
        : RefreshCw;
  const ActionIcon = unauthorized ? LogIn : RefreshCw;

  const recover = () => {
    if (unauthorized) {
      window.location.assign("/login");
      return;
    }
    requestRuntimeRecovery();
  };

  return (
    <div
      role={urgent ? "alert" : "status"}
      aria-live={urgent ? "assertive" : "polite"}
      data-runtime-resilience-status
      className={
        "fixed inset-x-3 bottom-[calc(var(--mobile-tabbar-height,0px)+var(--space-2))] z-[var(--z-banner)] mx-auto flex max-w-xl items-center gap-2 rounded-[var(--radius-card)] border px-3 py-2 type-body-sm shadow-[var(--shadow-2)] backdrop-blur-xl md:bottom-4 " +
        (urgent
          ? "border-warning-border bg-warning-soft text-[var(--warning-fg)]"
          : "border-info-border bg-info-soft text-[var(--info-fg)]")
      }
    >
      <Icon
        className={"h-4 w-4 shrink-0 " + (!urgent ? "animate-spin" : "")}
        aria-hidden
      />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      <button
        type="button"
        onClick={recover}
        className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-[var(--radius-control)] px-2 text-[var(--fg-0)] transition-colors hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-[var(--focus-outline)] md:min-h-8"
      >
        <ActionIcon className="h-3.5 w-3.5" aria-hidden />
        {unauthorized ? "登录" : "立即恢复"}
      </button>
    </div>
  );
}
