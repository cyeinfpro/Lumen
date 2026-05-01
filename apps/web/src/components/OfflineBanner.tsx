"use client";

import { useEffect, useState } from "react";
import { WifiOff } from "lucide-react";

export function OfflineBanner() {
  // null 代表 SSR / hydration 初期未确定状态；仅在客户端 effect 运行后切换为 boolean，
  // 避免 SSR 默认 true 与客户端实际 offline 之间的 hydration 闪烁。
  const [online, setOnline] = useState<boolean | null>(null);

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

  // 初始化中（null）或在线（true）都不显示
  if (online !== false) return null;

  return (
    <div
      role="status"
      aria-live="assertive"
      className="fixed inset-x-0 top-0 z-[var(--z-toast,100)] flex items-center justify-center gap-2 border-b border-red-500/30 bg-red-950/95 px-4 py-2 text-sm text-red-100 shadow-lg backdrop-blur-md"
    >
      <WifiOff className="h-4 w-4" aria-hidden />
      <span>网络已断开，恢复后会自动重连。</span>
    </div>
  );
}
