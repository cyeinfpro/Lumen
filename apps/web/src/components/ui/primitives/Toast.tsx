"use client";

// 全局 toast：右下角堆叠。通过 `toast.success(...) / toast.error(...) / toast.info(...)`
// 在任何组件/回调中调用；需要把 <ToastViewport /> 挂在 layout.tsx 里。
// 自动 3s 消失，可带 action（一个按钮）。

import { useEffect } from "react";
import { create } from "zustand";
import { motion, AnimatePresence } from "framer-motion";
import {
  CheckCircle2,
  AlertCircle,
  AlertTriangle,
  Info,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";

type ToastTone = "success" | "error" | "info" | "warning";

interface ToastAction {
  label: string;
  onClick: () => void;
}

interface ToastItem {
  id: string;
  tone: ToastTone;
  title: string;
  description?: string;
  durationMs: number;
  action?: ToastAction;
}

interface ToastState {
  items: ToastItem[];
  push: (t: Omit<ToastItem, "id" | "durationMs"> & { durationMs?: number }) => string;
  dismiss: (id: string) => void;
  clear: () => void;
}

const DEFAULT_DURATION_MS = 3000;

export const useToastStore = create<ToastState>((set) => ({
  items: [],
  push: (t) => {
    const id =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `t-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const item: ToastItem = {
      id,
      tone: t.tone,
      title: t.title,
      description: t.description,
      action: t.action,
      durationMs: t.durationMs ?? DEFAULT_DURATION_MS,
    };
    set((s) => ({ items: [...s.items, item] }));
    return id;
  },
  dismiss: (id) => set((s) => ({ items: s.items.filter((i) => i.id !== id) })),
  clear: () => set({ items: [] }),
}));

// 外部 API：`toast.success("已保存")` 等
function make(tone: ToastTone) {
  return (title: string, options?: { description?: string; durationMs?: number; action?: ToastAction }) =>
    useToastStore.getState().push({ tone, title, ...options });
}

export const toast = {
  success: make("success"),
  error: make("error"),
  info: make("info"),
  warning: make("warning"),
  dismiss: (id: string) => useToastStore.getState().dismiss(id),
  clear: () => useToastStore.getState().clear(),
};

const TONE_CLASSES: Record<ToastTone, { border: string; icon: string; iconBg: string }> = {
  success: {
    border: "border-[var(--success)]/30",
    icon: "text-[var(--success)]",
    iconBg: "bg-[var(--success-soft)]",
  },
  error: {
    border: "border-[var(--danger)]/30",
    icon: "text-[var(--danger)]",
    iconBg: "bg-[var(--danger-soft)]",
  },
  info: {
    border: "border-[var(--info)]/30",
    icon: "text-[var(--info)]",
    iconBg: "bg-[var(--info-soft)]",
  },
  warning: {
    border: "border-[var(--warning)]/30",
    icon: "text-[var(--warning)]",
    iconBg: "bg-[var(--warning-soft)]",
  },
};

function ToneIcon({ tone }: { tone: ToastTone }) {
  const cls = "w-4 h-4";
  if (tone === "success") return <CheckCircle2 className={cls} aria-hidden="true" />;
  if (tone === "error") return <AlertCircle className={cls} aria-hidden="true" />;
  if (tone === "warning") return <AlertTriangle className={cls} aria-hidden="true" />;
  return <Info className={cls} aria-hidden="true" />;
}

function ToastRow({ item }: { item: ToastItem }) {
  const dismiss = useToastStore((s) => s.dismiss);
  const tone = TONE_CLASSES[item.tone];

  // 自毁定时器（使用 effect）
  useAutoDismiss(item.id, item.durationMs, dismiss);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 16, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, x: 24, scale: 0.96, transition: { duration: 0.18 } }}
      transition={{ type: "spring", damping: 22, stiffness: 260 }}
      role="status"
      aria-live="polite"
      className={cn(
        "pointer-events-auto w-[320px] max-w-[calc(100vw-2rem)]",
        // 移动端撑满可用宽度（已扣掉 viewport 两侧 padding）
        "max-sm:w-full",
        "flex items-start gap-3 px-3 py-2.5 rounded-xl",
        "bg-neutral-900/95 backdrop-blur-xl border text-[var(--fg-0)]",
        "shadow-lumen-pop",
        tone.border,
      )}
    >
      <div
        className={cn(
          "mt-0.5 w-6 h-6 shrink-0 rounded-full flex items-center justify-center",
          tone.iconBg,
          tone.icon,
        )}
      >
        <ToneIcon tone={item.tone} />
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-[13px] font-medium leading-tight truncate">{item.title}</p>
        {item.description ? (
          <p className="mt-0.5 text-[11px] text-[var(--fg-1)] leading-relaxed line-clamp-3">
            {item.description}
          </p>
        ) : null}
        {item.action ? (
          <button
            type="button"
            onClick={() => {
              item.action?.onClick();
              dismiss(item.id);
            }}
            className="mt-1.5 text-[11px] font-medium text-[var(--accent)] hover:underline underline-offset-2"
          >
            {item.action.label}
          </button>
        ) : null}
      </div>
      <button
        type="button"
        aria-label="关闭通知"
        onClick={() => dismiss(item.id)}
        className={cn(
          "shrink-0 w-6 h-6 rounded-md inline-flex items-center justify-center",
          "text-[var(--fg-1)] hover:text-[var(--fg-0)] hover:bg-white/8 transition-colors",
        )}
      >
        <X className="w-3.5 h-3.5" aria-hidden="true" />
      </button>
    </motion.div>
  );
}

// 独立的 hook：避免在 ToastRow 重复声明 useEffect 逻辑
function useAutoDismiss(id: string, durationMs: number, dismiss: (id: string) => void) {
  useEffect(() => {
    if (durationMs <= 0) return;
    const t = window.setTimeout(() => dismiss(id), durationMs);
    return () => window.clearTimeout(t);
  }, [id, durationMs, dismiss]);
}

export function ToastViewport() {
  const items = useToastStore((s) => s.items);
  return (
    <div
      className={cn(
        "fixed z-[120] flex flex-col gap-2",
        // 桌面：右下角
        "sm:bottom-4 sm:right-4 sm:items-end",
        // 移动端：底部居中，留左右 padding；safe-area 避免被 home indicator / PromptComposer 挡住
        "max-sm:left-0 max-sm:right-0 max-sm:items-center max-sm:px-4",
        "max-sm:bottom-[max(1rem,env(safe-area-inset-bottom))]",
        "pointer-events-none",
      )}
      aria-live="polite"
      aria-atomic="false"
    >
      <AnimatePresence initial={false}>
        {items.map((item) => (
          <ToastRow key={item.id} item={item} />
        ))}
      </AnimatePresence>
    </div>
  );
}

export default ToastViewport;
