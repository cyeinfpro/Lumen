"use client";

// 极简 tooltip：300ms 悬停延迟后显示；pointer-events-none 不阻塞点击。
// 不依赖 Radix；定位用 absolute + side 四方向。内容跟随 children 的 focus-visible 一并显示，
// 便于键盘用户预览。

import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import { cloneElement, useEffect, useRef, useState, useId } from "react";
import { cn } from "@/lib/utils";

type Side = "top" | "bottom" | "left" | "right";

interface TooltipProps {
  content: React.ReactNode;
  children: React.ReactElement;
  side?: Side;
  delayMs?: number;
  className?: string;
  /** 为 false 时完全旁路，等同于直接渲染 children。便于条件开关。 */
  enabled?: boolean;
}

const SIDE_POS: Record<Side, string> = {
  top: "bottom-full left-1/2 -translate-x-1/2 mb-1.5",
  bottom: "top-full left-1/2 -translate-x-1/2 mt-1.5",
  left: "right-full top-1/2 -translate-y-1/2 mr-1.5",
  right: "left-full top-1/2 -translate-y-1/2 ml-1.5",
};

const SIDE_ORIGIN: Record<Side, string> = {
  top: "50% 100%",
  bottom: "50% 0%",
  left: "100% 50%",
  right: "0% 50%",
};

const TOOLTIP_WARM_WINDOW_MS = 800;
let tooltipWarmUntil = 0;

function mergeDescribedBy(existing: string | undefined, tooltipId: string) {
  return [...new Set([...(existing?.split(/\s+/) ?? []), tooltipId])]
    .filter(Boolean)
    .join(" ");
}

export function Tooltip({
  content,
  children,
  side = "top",
  delayMs = 180,
  className,
  enabled = true,
}: TooltipProps) {
  const [open, setOpen] = useState(false);
  const [instant, setInstant] = useState(false);
  const [isTouch, setIsTouch] = useState(false);
  const reduceMotion = useReducedMotion();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const id = useId();

  // 触控设备通过 matchMedia 检测；SSR 安全：初始 false，客户端 effect 里同步
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(hover: none)");
    const sync = () => setIsTouch(mq.matches);
    sync();
    mq.addEventListener?.("change", sync);
    return () => mq.removeEventListener?.("change", sync);
  }, []);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  if (!enabled || content == null || content === "") {
    return children;
  }
  // 移动端不挂 tooltip 弹层；依赖子元素自身的 title / aria-label 语义
  if (isTouch) {
    return children;
  }

  const trigger = children as React.ReactElement<{
    "aria-describedby"?: string;
  }>;
  const showPointer = () => {
    if (timer.current) clearTimeout(timer.current);
    const nextDelay = Date.now() < tooltipWarmUntil ? 0 : delayMs;
    setInstant(nextDelay === 0);
    timer.current = setTimeout(() => setOpen(true), nextDelay);
  };
  const showFocus = () => {
    if (timer.current) clearTimeout(timer.current);
    setInstant(true);
    setOpen(true);
  };
  const hide = () => {
    if (timer.current) clearTimeout(timer.current);
    if (open) tooltipWarmUntil = Date.now() + TOOLTIP_WARM_WINDOW_MS;
    setOpen(false);
  };

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={showPointer}
      onMouseLeave={hide}
      onFocus={showFocus}
      onBlur={hide}
    >
      {cloneElement(trigger, {
        "aria-describedby": mergeDescribedBy(
          trigger.props["aria-describedby"],
          id,
        ),
      })}
      <AnimatePresence>
        {open && (
          <motion.span
            key={id}
            id={id}
            role="tooltip"
            initial={
              reduceMotion || instant
                ? false
                : { opacity: 0, scale: 0.97 }
            }
            animate={{ opacity: 1, scale: 1 }}
            exit={
              reduceMotion || instant
                ? { opacity: 0 }
                : { opacity: 0, scale: 0.97 }
            }
            transition={{
              duration: reduceMotion || instant ? 0 : 0.14,
              ease: [0.22, 1, 0.36, 1],
            }}
            className={cn(
              "pointer-events-none absolute z-[60]",
              "max-w-[min(20rem,calc(100vw-1rem))] whitespace-normal break-words px-2 py-1 text-center type-caption font-medium rounded-[var(--radius-control)]",
              "bg-[var(--bg-1)]/95 text-[var(--fg-0)] border border-[var(--border)]",
              "adaptive-material shadow-lumen-pop backdrop-blur-md",
              SIDE_POS[side],
              className,
            )}
            style={{ transformOrigin: SIDE_ORIGIN[side] }}
          >
            {content}
          </motion.span>
        )}
      </AnimatePresence>
    </span>
  );
}
