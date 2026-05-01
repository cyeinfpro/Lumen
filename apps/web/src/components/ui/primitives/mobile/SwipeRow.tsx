"use client";

import { motion, useMotionValue, animate } from "framer-motion";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { SPRING } from "@/lib/motion";
import { Pressable } from "./Pressable";

export interface SwipeAction {
  key: string;
  label: ReactNode;
  icon?: ReactNode;
  color?: "neutral" | "warning" | "danger";
  /** 确认二次点击，默认 false */
  confirm?: boolean;
  onAction: () => void;
}

export interface SwipeRowProps {
  children: ReactNode;
  /** 左滑露出的 actions，最多 3 个 */
  actions: SwipeAction[];
  /** 左滑 ≥ 此距离触发末位按钮；默认 200 */
  fullSwipeThreshold?: number;
  /** actions 宽度（单按钮），默认 80 */
  buttonWidth?: number;
  className?: string;
}

function colorClass(c: SwipeAction["color"]) {
  switch (c) {
    case "danger":
      return "bg-[var(--danger)] text-white";
    case "warning":
      // 令牌没有 --warning；使用琥珀 500 + 暗底配色，保持"提醒但非破坏"语义
      return "bg-[var(--amber-500)] text-[#2a1a00]";
    default:
      return "bg-[var(--bg-3)] text-[var(--fg-0)]";
  }
}

export function SwipeRow({
  children,
  actions,
  fullSwipeThreshold = 200,
  buttonWidth = 80,
  className = "",
}: SwipeRowProps) {
  const x = useMotionValue(0);
  const width = Math.min(actions.length, 3) * buttonWidth;
  const [confirmKey, setConfirmKey] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const animationRef = useRef<{ stop: () => void } | null>(null);

  const stopAnimation = useCallback(() => {
    animationRef.current?.stop();
    animationRef.current = null;
  }, []);

  useEffect(() => {
    return () => {
      stopAnimation();
    };
  }, [stopAnimation]);

  const resetTo = useCallback(
    (target: number) => {
      stopAnimation();
      const ctrl = animate(x, target, SPRING.snap);
      animationRef.current = ctrl;
      setConfirmKey(null);
      return ctrl;
    },
    [stopAnimation, x],
  );

  const handleEnd = useCallback(
    (_e: unknown, info: { offset: { x: number }; velocity: { x: number } }) => {
      const dx = info.offset.x;
      const v = info.velocity.x;
      if (dx <= -fullSwipeThreshold || v < -800) {
        const last = actions[actions.length - 1];
        if (!last) {
          resetTo(0);
          return;
        }
        if (last.confirm) {
          setConfirmKey(last.key);
          resetTo(-width);
          return;
        }
        last.onAction();
        resetTo(0);
        return;
      }
      if (dx <= -width / 2 || v < -400) {
        resetTo(-width);
      } else {
        resetTo(0);
      }
    },
    [actions, width, resetTo, fullSwipeThreshold],
  );

  return (
    <div
      ref={containerRef}
      className={["relative overflow-hidden touch-pan-y", className].join(" ")}
    >
      {/* action 层 */}
      <div
        aria-hidden
        className="absolute inset-y-0 right-0 flex"
        style={{ width }}
      >
        {actions.map((a) => {
          const confirming = confirmKey === a.key;
          const labelText = typeof a.label === "string" ? a.label : a.key;
          return (
            <Pressable
              key={a.key}
              size="default"
              minHit={false}
              pressScale="tight"
              haptic={a.color === "danger" ? "warning" : "light"}
              aria-label={confirming ? `确认${labelText}` : labelText}
              onPress={() => {
                if (a.confirm && !confirming) {
                  setConfirmKey(a.key);
                  return;
                }
                a.onAction();
                resetTo(0);
              }}
              className={[
                "flex-col h-full text-xs gap-1",
                colorClass(a.color),
                confirming ? "bg-[var(--danger)] text-white" : "",
              ].join(" ")}
              style={{ width: buttonWidth }}
            >
              {a.icon && <span className="mb-1">{a.icon}</span>}
              <span className="text-caption">{confirming ? "确认?" : a.label}</span>
            </Pressable>
          );
        })}
      </div>
      {/* drag 层 */}
      <motion.div
        drag="x"
        dragConstraints={{ left: -width - 40, right: 0 }}
        dragElastic={{ left: 0.05, right: 0 }}
        dragDirectionLock
        onDragStart={stopAnimation}
        onDragEnd={handleEnd}
        style={{ x }}
        className="relative bg-[var(--bg-0)]"
      >
        {children}
      </motion.div>
    </div>
  );
}
