"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useState,
} from "react";
import { DURATION, EASE } from "@/lib/motion";
import { MobileIconButton } from "./MobileIconButton";
import { X } from "lucide-react";

export type MobileToastKind = "info" | "success" | "warning" | "danger";

interface ToastItem {
  id: string;
  kind: MobileToastKind;
  message: ReactNode;
  count: number;
  expireAt: number;
}

const DEFAULT_DURATION = 1800;
const MAX_TOASTS = 2;

// 保留一个全局 push 函数，供 Class / 非 hook 场景使用
let globalPush: ((m: ReactNode, k?: MobileToastKind) => void) | null = null;

export function pushMobileToast(message: ReactNode, kind: MobileToastKind = "info") {
  globalPush?.(message, kind);
}

export function MobileToastViewport() {
  const [items, setItems] = useState<ToastItem[]>([]);

  const push = useCallback((message: ReactNode, kind: MobileToastKind = "info") => {
    setItems((list) => {
      // 相同文本合并
      const key = typeof message === "string" ? message : null;
      if (key) {
        const idx = list.findIndex(
          (t) => typeof t.message === "string" && t.message === key && t.kind === kind,
        );
        if (idx >= 0) {
          const next = list.slice();
          next[idx] = {
            ...next[idx],
            count: next[idx].count + 1,
            expireAt: Date.now() + DEFAULT_DURATION,
          };
          return next;
        }
      }
      const item: ToastItem = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        kind,
        message,
        count: 1,
        expireAt: Date.now() + DEFAULT_DURATION,
      };
      const merged = [...list, item];
      return merged.slice(-MAX_TOASTS);
    });
  }, []);

  useEffect(() => {
    globalPush = push;
    return () => {
      if (globalPush === push) globalPush = null;
    };
  }, [push]);

  // 垃圾回收：每 250ms 扫一次过期
  useEffect(() => {
    if (items.length === 0) return;
    const t = window.setInterval(() => {
      const now = Date.now();
      setItems((list) => list.filter((it) => it.expireAt > now));
    }, 250);
    return () => window.clearInterval(t);
  }, [items.length]);

  return (
    <div
      aria-live="polite"
      aria-atomic="true"
      className="pointer-events-none fixed inset-x-0 flex flex-col items-center gap-2 px-4"
      style={{
        // mobile-tabbar-height 已包含 safe-area；其余高度统一由 globals.css 管理。
        bottom: "var(--mobile-toast-bottom-offset)",
        zIndex: "var(--z-toast, 100)" as unknown as number,
      }}
    >
      <AnimatePresence initial={false}>
        {items.map((t) => (
          <motion.div
            key={t.id}
            initial={{ opacity: 0, y: 16, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 8, scale: 0.98 }}
            transition={{ duration: DURATION.normal, ease: EASE.develop }}
            className={[
              "pointer-events-auto flex items-start gap-2",
              "w-full max-w-[min(28rem,calc(100vw-2rem))] px-3.5 py-2.5 rounded-[var(--radius-card)]",
              "text-[13px] font-medium",
              "backdrop-blur-xl border shadow-[var(--shadow-2)]",
              kindClass(t.kind),
            ].join(" ")}
            role={t.kind === "danger" || t.kind === "warning" ? "alert" : "status"}
          >
            <span className="min-w-0 flex-1 break-words leading-snug [overflow-wrap:anywhere]">
              {t.message}
            </span>
            {t.count > 1 && (
              <span className="mt-0.5 shrink-0 text-[10px] tracking-wider text-[var(--fg-2)]">
                ×{t.count}
              </span>
            )}
            <MobileIconButton
              icon={<X className="w-4 h-4" />}
              label="关闭通知"
              minHit={false}
              onPress={() => setItems((list) => list.filter((x) => x.id !== t.id))}
              className="-mr-1 -mt-1 w-8 h-8 shrink-0 opacity-60 hover:opacity-100"
            />
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

function kindClass(k: MobileToastKind) {
  switch (k) {
    case "success":
      return "bg-[var(--bg-1)]/90 text-success border-success-border";
    case "warning":
      return "bg-[var(--bg-1)]/90 text-warning border-warning-border";
    case "danger":
      return "bg-[var(--bg-1)]/90 text-danger border-danger-border";
    default:
      return "bg-[var(--bg-1)]/90 text-[var(--fg-0)] border-[var(--border-subtle)]";
  }
}
