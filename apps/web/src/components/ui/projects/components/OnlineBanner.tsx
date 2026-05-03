"use client";

// 离线/恢复 banner。
// 离线：红色横条；从离线恢复时显示 2.5s 的"已恢复连接"绿色提示后自动隐藏。
// 不依赖 Service Worker，仅基于 navigator.onLine 与 online/offline 事件。

import { motion, AnimatePresence } from "framer-motion";
import { CloudOff, Wifi } from "lucide-react";
import { useEffect, useRef, useState } from "react";

type Mode = "hidden" | "offline" | "recovered";

export function OnlineBanner() {
  const [mode, setMode] = useState<Mode>("hidden");
  const recoveredTimerRef = useRef<number | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const setOffline = () => {
      if (recoveredTimerRef.current) {
        window.clearTimeout(recoveredTimerRef.current);
        recoveredTimerRef.current = null;
      }
      setMode("offline");
    };
    const setOnline = () => {
      setMode((prev) => (prev === "offline" ? "recovered" : prev));
      if (recoveredTimerRef.current) window.clearTimeout(recoveredTimerRef.current);
      recoveredTimerRef.current = window.setTimeout(() => {
        setMode("hidden");
        recoveredTimerRef.current = null;
      }, 2500);
    };
    if (!navigator.onLine) setOffline();
    window.addEventListener("offline", setOffline);
    window.addEventListener("online", setOnline);
    return () => {
      window.removeEventListener("offline", setOffline);
      window.removeEventListener("online", setOnline);
      if (recoveredTimerRef.current) window.clearTimeout(recoveredTimerRef.current);
    };
  }, []);

  return (
    <AnimatePresence>
      {mode === "offline" ? (
        <motion.div
          key="offline"
          initial={{ y: -28, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: -28, opacity: 0 }}
          transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
          className="sticky top-0 z-[var(--z-toast)] flex h-9 items-center justify-center gap-2 border-b border-[var(--danger)]/30 bg-[var(--danger-soft)] text-xs text-[var(--fg-0)]"
          role="status"
          aria-live="polite"
        >
          <CloudOff className="h-3.5 w-3.5" />
          网络已断开，操作将在恢复后重试
        </motion.div>
      ) : mode === "recovered" ? (
        <motion.div
          key="recovered"
          initial={{ y: -28, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: -28, opacity: 0 }}
          transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
          className="sticky top-0 z-[var(--z-toast)] flex h-9 items-center justify-center gap-2 border-b border-[var(--success)]/30 bg-[var(--success-soft)] text-xs text-[var(--fg-0)]"
          role="status"
          aria-live="polite"
        >
          <Wifi className="h-3.5 w-3.5" />
          网络已恢复
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
