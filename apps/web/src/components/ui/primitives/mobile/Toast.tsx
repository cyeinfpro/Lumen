"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
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

interface MobileToastCtx {
  push: (message: ReactNode, kind?: MobileToastKind) => void;
}

const Ctx = createContext<MobileToastCtx | null>(null);

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

  const ctx = useMemo(() => ({ push }), [push]);

  return (
    <Ctx.Provider value={ctx}>
      <div
        aria-live="polite"
        aria-atomic="true"
        className="pointer-events-none fixed inset-x-0 flex flex-col items-center gap-2 px-4"
        style={{
          // 56px = 底部导航栏高度；56px = 迷你播放器高度（MiniPlayer）；16px = 间距
          bottom: "calc(56px + 56px + 16px + env(safe-area-inset-bottom, 0px))",
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
                "pointer-events-auto inline-flex items-center gap-2",
                "max-w-full px-4 py-2.5 rounded-full",
                "text-[13px] font-medium",
                "backdrop-blur-xl border shadow-[var(--shadow-2)]",
                kindClass(t.kind),
              ].join(" ")}
              role={t.kind === "danger" || t.kind === "warning" ? "alert" : "status"}
            >
              <span>{t.message}</span>
              {t.count > 1 && (
                <span className="text-[10px] tracking-wider text-[var(--fg-2)] ml-0.5">
                  ×{t.count}
                </span>
              )}
              <MobileIconButton
                icon={<X className="w-4 h-4" />}
                label="关闭通知"
                minHit={false}
                onPress={() => setItems((list) => list.filter((x) => x.id !== t.id))}
                className="w-8 h-8 opacity-60 hover:opacity-100"
              />
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </Ctx.Provider>
  );
}

function kindClass(k: MobileToastKind) {
  switch (k) {
    case "success":
      return "bg-[var(--bg-1)]/90 text-[var(--success)] border-[rgba(48,164,108,0.4)]";
    case "warning":
      return "bg-[var(--bg-1)]/90 text-[var(--amber-300)] border-[var(--border-amber)]";
    case "danger":
      return "bg-[var(--bg-1)]/90 text-[var(--danger)] border-[rgba(229,72,77,0.4)]";
    default:
      return "bg-[var(--bg-1)]/90 text-[var(--fg-0)] border-[var(--border-subtle)]";
  }
}

export function useMobileToast() {
  const ctx = useContext(Ctx);
  return {
    push: (m: ReactNode, k?: MobileToastKind) => {
      if (ctx) ctx.push(m, k);
      else pushMobileToast(m, k);
    },
  };
}
