"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, WifiOff } from "lucide-react";

const PWA_STATUS_EVENT = "lumen:pwa-status";

type PwaStatusDetail = {
  status?: "registration_failed";
  message?: string;
};

export function OfflineBanner() {
  // null 代表 SSR / hydration 初期未确定状态；仅在客户端 effect 运行后切换为 boolean，
  // 避免 SSR 默认 true 与客户端实际 offline 之间的 hydration 闪烁。
  const [online, setOnline] = useState<boolean | null>(null);
  const [pwaIssue, setPwaIssue] = useState<string | null>(null);

  useEffect(() => {
    const update = () => setOnline(navigator.onLine);
    update();
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);

  useEffect(() => {
    const onPwaStatus = (event: Event) => {
      const detail = (event as CustomEvent<PwaStatusDetail>).detail;
      if (detail?.status === "registration_failed") {
        setPwaIssue(detail.message || "离线安装不可用，刷新后会重试。");
      }
    };
    window.addEventListener(PWA_STATUS_EVENT, onPwaStatus);
    return () => window.removeEventListener(PWA_STATUS_EVENT, onPwaStatus);
  }, []);

  if (online === false) {
    return (
      <div
        role="status"
        aria-live="assertive"
        className="fixed inset-x-0 top-0 z-[var(--z-toast,100)] flex items-center justify-center gap-2 border-b border-danger-border bg-danger-soft px-4 py-2 type-body-sm text-[var(--danger-fg)] shadow-[var(--shadow-2)] backdrop-blur-md"
      >
        <WifiOff className="h-4 w-4" aria-hidden />
        <span>网络已断开，恢复后会自动重连。</span>
      </div>
    );
  }

  if (!pwaIssue) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed inset-x-0 top-0 z-[var(--z-toast,100)] flex items-center justify-center gap-2 border-b border-warning-border bg-warning-soft px-4 py-2 type-body-sm text-[var(--warning-fg)] shadow-[var(--shadow-2)] backdrop-blur-md"
    >
      <AlertTriangle className="h-4 w-4" aria-hidden />
      <span>{pwaIssue}</span>
    </div>
  );
}
