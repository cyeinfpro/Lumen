"use client";

import { useEffect, useRef, useState } from "react";
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
  const bannerRef = useRef<HTMLDivElement>(null);

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

  const visible = online === false || Boolean(pwaIssue);
  useEffect(() => {
    if (!visible) {
      document.documentElement.style.setProperty(
        "--offline-banner-height",
        "0px",
      );
      return;
    }
    const element = bannerRef.current;
    if (!element) return;
    const update = () => {
      document.documentElement.style.setProperty(
        "--offline-banner-height",
        `${element.offsetHeight}px`,
      );
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => {
      observer.disconnect();
      document.documentElement.style.setProperty(
        "--offline-banner-height",
        "0px",
      );
    };
  }, [visible]);

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
        ref={bannerRef}
        role="alert"
        aria-live="assertive"
        className="fixed inset-x-0 z-[var(--z-toast,100)] flex min-h-11 items-start justify-center gap-2 border-b border-danger-border bg-danger-soft px-4 py-2 type-body-sm text-[var(--danger-fg)] shadow-[var(--shadow-2)] backdrop-blur-md sm:items-center"
        style={{
          top: "var(--system-banner-height, 0px)",
          paddingTop:
            "max(0.5rem, calc(env(safe-area-inset-top, 0px) - var(--system-banner-height, 0px)))",
        }}
      >
        <WifiOff className="mt-0.5 h-4 w-4 shrink-0 sm:mt-0" aria-hidden />
        <span className="max-w-2xl break-words">
          网络已断开，恢复后会自动重连。
        </span>
      </div>
    );
  }

  if (!pwaIssue) return null;

  return (
    <div
      ref={bannerRef}
      role="status"
      aria-live="polite"
      className="fixed inset-x-0 z-[var(--z-toast,100)] flex min-h-11 items-start justify-center gap-2 border-b border-warning-border bg-warning-soft px-4 py-2 type-body-sm text-[var(--warning-fg)] shadow-[var(--shadow-2)] backdrop-blur-md sm:items-center"
      style={{
        top: "var(--system-banner-height, 0px)",
        paddingTop:
          "max(0.5rem, calc(env(safe-area-inset-top, 0px) - var(--system-banner-height, 0px)))",
      }}
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 sm:mt-0" aria-hidden />
      <span className="max-w-2xl break-words">{pwaIssue}</span>
    </div>
  );
}
