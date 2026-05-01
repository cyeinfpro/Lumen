"use client";

// 极简 tooltip：300ms 悬停延迟后显示；pointer-events-none 不阻塞点击。
// 不依赖 Radix；定位用 absolute + side 四方向。内容跟随 children 的 focus-visible 一并显示，
// 便于键盘用户预览。

import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useRef, useState, useId } from "react";
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

const SIDE_OFFSET: Record<Side, { x: number; y: number }> = {
  top: { x: 0, y: 4 },
  bottom: { x: 0, y: -4 },
  left: { x: 4, y: 0 },
  right: { x: -4, y: 0 },
};

export function Tooltip({
  content,
  children,
  side = "top",
  delayMs = 300,
  className,
  enabled = true,
}: TooltipProps) {
  const [open, setOpen] = useState(false);
  const [isTouch, setIsTouch] = useState(false);
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

  if (!enabled || content == null || content === "") {
    return children;
  }
  // 移动端不挂 tooltip 弹层；依赖子元素自身的 title / aria-label 语义
  if (isTouch) {
    return children;
  }

  const show = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setOpen(true), delayMs);
  };
  const hide = () => {
    if (timer.current) clearTimeout(timer.current);
    setOpen(false);
  };

  const offset = SIDE_OFFSET[side];

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      <AnimatePresence>
        {open && (
          <motion.span
            key={id}
            role="tooltip"
            initial={{ opacity: 0, x: offset.x, y: offset.y }}
            animate={{ opacity: 1, x: 0, y: 0 }}
            exit={{ opacity: 0, x: offset.x, y: offset.y }}
            transition={{ duration: 0.14, ease: [0.22, 1, 0.36, 1] }}
            className={cn(
              "pointer-events-none absolute z-[60]",
              "px-2 py-1 rounded-md text-[11px] font-medium whitespace-nowrap",
              "bg-neutral-900/95 text-neutral-100 border border-white/10",
              "shadow-lumen-pop backdrop-blur-md",
              SIDE_POS[side],
              className,
            )}
          >
            {content}
          </motion.span>
        )}
      </AnimatePresence>
    </span>
  );
}

export default Tooltip;
