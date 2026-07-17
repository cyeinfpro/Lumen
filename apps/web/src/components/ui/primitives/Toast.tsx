"use client";

// 全局 toast：右下角堆叠。通过 `toast.success(...) / toast.error(...) / toast.info(...)`
// 在任何组件/回调中调用；需要把 <ToastViewport /> 挂在 layout.tsx 里。
// 自动 3s 消失，可带 action（一个按钮）。

import { useEffect, useRef, useState } from "react";
import { create } from "zustand";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import {
  CheckCircle2,
  AlertCircle,
  AlertTriangle,
  Info,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  GESTURE,
  SPRING,
  projectMomentum,
} from "@/lib/motion";
import { IconButton } from "./IconButton";

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

const useToastStore = create<ToastState>((set) => ({
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
  const reduceMotion = useReducedMotion();
  const tone = TONE_CLASSES[item.tone];
  const [paused, setPaused] = useState(false);

  useAutoDismiss(item.id, item.durationMs, dismiss, paused);

  return (
    <motion.div
      layout
      initial={reduceMotion ? false : { opacity: 0, y: 16, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={reduceMotion ? { opacity: 0 } : { opacity: 0, x: 24, scale: 0.96 }}
      transition={reduceMotion ? { duration: 0 } : SPRING.toast}
      drag={reduceMotion ? false : "x"}
      dragConstraints={{ left: 0, right: 0 }}
      dragElastic={{ left: 0.04, right: 0.28 }}
      dragMomentum={false}
      onDragEnd={(_event, info) => {
        const projectedX = info.offset.x + projectMomentum(info.velocity.x);
        if (
          projectedX > GESTURE.snapDistance * 1.5 ||
          info.velocity.x > GESTURE.dismissVelocity
        ) {
          dismiss(item.id);
        }
      }}
      style={{ touchAction: "pan-y" }}
      role={item.tone === "error" || item.tone === "warning" ? "alert" : "status"}
      aria-live={item.tone === "error" || item.tone === "warning" ? "assertive" : "polite"}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocusCapture={() => setPaused(true)}
      onBlurCapture={(event) => {
        if (
          event.relatedTarget instanceof Node &&
          event.currentTarget.contains(event.relatedTarget)
        ) {
          return;
        }
        setPaused(false);
      }}
      className={cn(
        "pointer-events-auto w-[320px] max-w-[calc(100vw-2rem)]",
        // 移动端撑满可用宽度（已扣掉 viewport 两侧 padding）
        "max-sm:w-full",
        "surface-panel flex items-start gap-3 px-3 py-2.5 text-[var(--fg-0)]",
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
        <p className="type-label break-words text-[var(--fg-0)]">{item.title}</p>
        {item.description ? (
          <p className="type-caption mt-0.5 line-clamp-3 text-[var(--fg-1)]">
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
      <IconButton
        variant="ghost"
        size="sm"
        aria-label="关闭通知"
        onClick={() => dismiss(item.id)}
        className="shrink-0 text-[var(--fg-1)] hover:text-[var(--fg-0)]"
      >
        <X className="w-3.5 h-3.5" aria-hidden="true" />
      </IconButton>
    </motion.div>
  );
}

// 页面隐藏、hover 或 action 获得焦点时暂停，避免用户回来时通知已经消失。
function useAutoDismiss(
  id: string,
  durationMs: number,
  dismiss: (id: string) => void,
  paused: boolean,
) {
  const remainingMs = useRef(durationMs);
  const startedAt = useRef(0);

  useEffect(() => {
    remainingMs.current = durationMs;
  }, [durationMs, id]);

  useEffect(() => {
    if (durationMs <= 0) return;
    let timeoutId = 0;

    const stop = () => {
      if (!timeoutId) return;
      window.clearTimeout(timeoutId);
      timeoutId = 0;
      remainingMs.current = Math.max(
        0,
        remainingMs.current - (Date.now() - startedAt.current),
      );
    };
    const start = () => {
      if (paused || document.hidden || timeoutId) return;
      if (remainingMs.current <= 0) {
        dismiss(id);
        return;
      }
      startedAt.current = Date.now();
      timeoutId = window.setTimeout(() => dismiss(id), remainingMs.current);
    };
    const onVisibilityChange = () => {
      if (document.hidden) stop();
      else start();
    };

    start();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [dismiss, durationMs, id, paused]);
}

export function ToastViewport() {
  const items = useToastStore((s) => s.items);
  return (
    <div
      className={cn(
        "fixed z-[var(--z-toast)] flex flex-col gap-2",
        // 桌面：右下角
        "sm:bottom-4 sm:right-4 sm:items-end",
        // 移动端：底部居中，留左右 padding；safe-area 避免被 home indicator / composer 挡住
        "max-sm:left-0 max-sm:right-0 max-sm:items-center max-sm:px-[var(--mobile-page-gutter)]",
        "max-sm:bottom-[calc(var(--mobile-tabbar-height)+0.75rem)]",
        "pointer-events-none",
      )}
    >
      <AnimatePresence initial={false}>
        {items.map((item) => (
          <ToastRow key={item.id} item={item} />
        ))}
      </AnimatePresence>
    </div>
  );
}
