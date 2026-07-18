"use client";

import {
  animate,
  motion,
  useMotionValue,
  useReducedMotion,
} from "framer-motion";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { GESTURE, SPRING, projectMomentum } from "@/lib/motion";
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
      return "bg-danger text-[var(--danger-on)]";
    case "warning":
      return "bg-warning text-[var(--warning-on)]";
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
  const reduceMotion = useReducedMotion();
  const visibleActions = actions.slice(0, 3);
  const width = visibleActions.length * buttonWidth;
  const [confirmKey, setConfirmKey] = useState<string | null>(null);
  const [actionsOpen, setActionsOpen] = useState(false);
  const actionsId = useId();
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
    (
      target: number,
      options: { velocity?: number; preserveConfirm?: boolean } = {},
    ) => {
      stopAnimation();
      setActionsOpen(target < 0);
      if (reduceMotion) {
        x.set(target);
        if (!options.preserveConfirm) setConfirmKey(null);
        return null;
      }
      const ctrl = animate(x, target, {
        ...SPRING.gesture,
        velocity: options.velocity ?? 0,
      });
      animationRef.current = ctrl;
      void ctrl.then(() => {
        if (animationRef.current === ctrl) animationRef.current = null;
      });
      if (!options.preserveConfirm) setConfirmKey(null);
      return ctrl;
    },
    [reduceMotion, stopAnimation, x],
  );

  const handleEnd = useCallback(
    (
      event: { type?: string },
      info: { offset: { x: number }; velocity: { x: number } },
    ) => {
      if (
        event.type === "pointercancel" ||
        event.type === "touchcancel"
      ) {
        resetTo(0);
        return;
      }
      const dx = info.offset.x;
      const v = info.velocity.x;
      const projectedX = dx + projectMomentum(v);
      const direction =
        Math.abs(v) >= GESTURE.snapVelocity
          ? Math.sign(v)
          : Math.sign(projectedX);
      if (
        direction < 0 &&
        (projectedX <= -fullSwipeThreshold ||
          v < -GESTURE.dismissVelocity)
      ) {
        const last = visibleActions[visibleActions.length - 1];
        if (!last) {
          resetTo(0, { velocity: v });
          return;
        }
        if (last.confirm) {
          setConfirmKey(last.key);
          resetTo(-width, { velocity: v, preserveConfirm: true });
          return;
        }
        last.onAction();
        resetTo(0, { velocity: v });
        return;
      }
      if (
        direction < 0 &&
        (projectedX <= -width / 2 || v < -GESTURE.snapVelocity)
      ) {
        resetTo(-width, { velocity: v });
      } else {
        resetTo(0, { velocity: v });
      }
    },
    [visibleActions, width, resetTo, fullSwipeThreshold],
  );

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (visibleActions.length === 0) return;
      if (event.key === "Escape") {
        if (!actionsOpen) return;
        event.preventDefault();
        resetTo(0);
        containerRef.current?.focus();
        return;
      }
      if (event.target !== event.currentTarget) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        resetTo(-width);
        return;
      }
      if (event.key === "ArrowRight") {
        if (!actionsOpen) return;
        event.preventDefault();
        resetTo(0);
        return;
      }
      if (
        event.key === "Enter" ||
        event.key === " " ||
        event.key === "Spacebar"
      ) {
        event.preventDefault();
        resetTo(actionsOpen ? 0 : -width);
      }
    },
    [actionsOpen, resetTo, visibleActions.length, width],
  );

  return (
    <div
      ref={containerRef}
      role="group"
      aria-label={
        actionsOpen
          ? "行操作已展开，按 Escape 收起"
          : "行操作，按左箭头或回车展开"
      }
      aria-keyshortcuts={
        visibleActions.length > 0
          ? "ArrowLeft ArrowRight Enter Space Escape"
          : undefined
      }
      aria-controls={visibleActions.length > 0 ? actionsId : undefined}
      tabIndex={visibleActions.length > 0 ? 0 : undefined}
      onKeyDown={handleKeyDown}
      className={["relative overflow-hidden touch-pan-y", className].join(" ")}
    >
      {/* action 层 */}
      <div
        id={actionsId}
        aria-hidden={!actionsOpen}
        inert={!actionsOpen ? true : undefined}
        className="absolute inset-y-0 right-0 flex"
        style={{ width }}
      >
        {visibleActions.map((a) => {
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
                confirming ? "bg-danger text-[var(--danger-on)]" : "",
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
        dragConstraints={{ left: -width, right: 0 }}
        dragElastic={
          reduceMotion ? false : { left: 0.16, right: 0.08 }
        }
        dragDirectionLock
        dragMomentum={false}
        onDragStart={() => {
          stopAnimation();
          setActionsOpen(true);
        }}
        onDragEnd={handleEnd}
        style={{ x }}
        className="relative bg-[var(--bg-0)]"
      >
        {children}
      </motion.div>
    </div>
  );
}
