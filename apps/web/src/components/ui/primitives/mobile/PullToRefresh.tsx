"use client";

// PullToRefresh · spec §9.6 / §6.8
// - 0-30px：琥珀小点渐现
// - 30-60px：呼吸扩大
// - ≥ 60px 释放：触发 onRefresh，顶部 1px 琥珀进度线扫过
// - 完成后 240ms 回弹
// - overscroll-behavior-y: contain
// - 仅当根容器 scrollTop === 0 才接管触摸；否则让原生滚动
// - reduced-motion：扫光替换为琥珀点一次亮起 / 回弹

import {
  type RefObject,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  DURATION,
  GESTURE,
  rubberBandDistance,
} from "@/lib/motion";
import { pushMobileToast } from "./Toast";

const SWEEP_MS = Math.round(DURATION.sheet * 1_000);

function resistedPullDistance(distance: number, threshold: number): number {
  const direct = distance * 0.68;
  if (direct <= threshold) return direct;
  return Math.min(
    threshold * 1.85,
    threshold +
      rubberBandDistance(direct - threshold, threshold * 1.6, 0.55),
  );
}

type PullGestureMode = "pending" | "vertical" | "horizontal";

function trackedTouch(
  touches: TouchList,
  identifier: number | null,
): Touch | null {
  if (identifier == null || touches.length !== 1) return null;
  const touch = touches[0];
  return touch.identifier === identifier ? touch : null;
}

function pullGestureIntent(dx: number, dy: number): PullGestureMode {
  if (
    Math.max(Math.abs(dx), Math.abs(dy)) <
    GESTURE.intentSlop
  ) {
    return "pending";
  }
  return dy > 0 &&
    Math.abs(dy) > Math.abs(dx) * GESTURE.directionBias
    ? "vertical"
    : "horizontal";
}

export interface PullToRefreshProps {
  /** 触发刷新的异步回调 */
  onRefresh: () => Promise<void> | void;
  children: ReactNode;
  /** 触发距离，默认 60 */
  threshold?: number;
  className?: string;
  /** 暴露实际滚动容器，给父级监听 scroll / IntersectionObserver 使用。 */
  containerRef?: RefObject<HTMLDivElement | null>;
  /** BUG-038: 下拉刷新激活时通知父组件，用于临时禁用 history loading IntersectionObserver。 */
  onActiveChange?: (active: boolean) => void;
}

export function PullToRefresh({
  onRefresh,
  children,
  threshold = 60,
  className = "",
  containerRef,
  onActiveChange,
}: PullToRefreshProps) {
  const [pull, setPull] = useState(0);
  const [loading, setLoading] = useState(false);
  const [sweeping, setSweeping] = useState(false);
  const [dragging, setDragging] = useState(false);
  const startX = useRef<number | null>(null);
  const startY = useRef<number | null>(null);
  const activeTouchId = useRef<number | null>(null);
  const gestureMode = useRef<PullGestureMode | null>(null);
  const pullFrameRef = useRef<number | null>(null);
  const pullRef = useRef(0);
  const refreshingRef = useRef(false);
  const refreshGenerationRef = useRef(0);
  const sweepTimerRef = useRef<number | null>(null);
  const ref = useRef<HTMLDivElement | null>(null);
  const latestRef = useRef({
    loading,
    onRefresh,
    threshold,
  });

  useEffect(() => {
    latestRef.current = {
      loading,
      onRefresh,
      threshold,
    };
  }, [loading, onRefresh, threshold]);

  // BUG-038: 通知父组件 pull-to-refresh 是否激活，以便禁用 history-loading IntersectionObserver。
  useEffect(() => {
    onActiveChange?.(dragging || loading);
  }, [dragging, loading, onActiveChange]);
  useEffect(() => {
    return () => onActiveChange?.(false);
  }, [onActiveChange]);

  const setContainerRefs = useCallback(
    (node: HTMLDivElement | null) => {
      ref.current = node;
      if (containerRef) containerRef.current = node;
    },
    [containerRef],
  );

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let disposed = false;
    const schedulePull = (value: number) => {
      pullRef.current = value;
      if (pullFrameRef.current !== null) return;
      pullFrameRef.current = window.requestAnimationFrame(() => {
        pullFrameRef.current = null;
        setPull(pullRef.current);
      });
    };
    const setPullImmediate = (value: number) => {
      if (pullFrameRef.current !== null) {
        window.cancelAnimationFrame(pullFrameRef.current);
        pullFrameRef.current = null;
      }
      pullRef.current = value;
      setPull(value);
    };
    const resetGesture = () => {
      startX.current = null;
      startY.current = null;
      activeTouchId.current = null;
      gestureMode.current = null;
      setDragging(false);
      setPullImmediate(0);
    };
    const onTouchStart = (e: TouchEvent) => {
      if (!ref.current) return;
      if (
        e.touches.length !== 1 ||
        latestRef.current.loading ||
        refreshingRef.current ||
        ref.current.scrollTop > 0
      ) {
        resetGesture();
        return;
      }
      const touch = e.touches[0];
      activeTouchId.current = touch.identifier;
      startX.current = touch.clientX;
      startY.current = touch.clientY;
      gestureMode.current = "pending";
    };
    const onTouchMove = (e: TouchEvent) => {
      if (startX.current == null || startY.current == null) return;
      const touch = trackedTouch(e.touches, activeTouchId.current);
      if (!touch) {
        resetGesture();
        return;
      }
      const dx = touch.clientX - startX.current;
      const dy = touch.clientY - startY.current;
      if (gestureMode.current === "pending") {
        gestureMode.current = pullGestureIntent(dx, dy);
        if (gestureMode.current === "pending") return;
        if (gestureMode.current === "horizontal") {
          startX.current = null;
          startY.current = null;
          setDragging(false);
          return;
        }
        setDragging(true);
      }
      if (gestureMode.current !== "vertical") return;
      if (dy <= 0) {
        if (pullRef.current !== 0) {
          schedulePull(0);
        }
        return;
      }
      const { threshold: currentThreshold } = latestRef.current;
      const resisted = resistedPullDistance(dy, currentThreshold);
      schedulePull(resisted);
      if (resisted > 10 && e.cancelable) e.preventDefault();
    };
    const finishGesture = (commit: boolean) => {
      const {
        loading: currentLoading,
        onRefresh: currentOnRefresh,
        threshold: currentThreshold,
      } = latestRef.current;
      const verticalGesture = gestureMode.current === "vertical";
      startX.current = null;
      startY.current = null;
      activeTouchId.current = null;
      gestureMode.current = null;
      setDragging(false);
      const finalPull =
        commit && verticalGesture ? pullRef.current : 0;
      if (
        finalPull >= currentThreshold &&
        !currentLoading &&
        !refreshingRef.current
      ) {
        const generation = refreshGenerationRef.current + 1;
        refreshGenerationRef.current = generation;
        refreshingRef.current = true;
        if (sweepTimerRef.current !== null) {
          window.clearTimeout(sweepTimerRef.current);
          sweepTimerRef.current = null;
        }
        setLoading(true);
        setSweeping(true);
        setPullImmediate(currentThreshold);
        Promise.resolve()
          .then(() => currentOnRefresh())
          .catch((err) => {
            if (
              disposed ||
              refreshGenerationRef.current !== generation
            ) {
              return;
            }
            // 把 catch 里吞掉的错误暴露给用户 —— 至少给个 toast,避免静默失败
            const msg =
              err instanceof Error && err.message ? err.message : "刷新失败";
            pushMobileToast(msg, "danger");
          })
          .finally(() => {
            if (
              disposed ||
              refreshGenerationRef.current !== generation
            ) {
              return;
            }
            refreshingRef.current = false;
            setLoading(false);
            setPullImmediate(0);
            sweepTimerRef.current = window.setTimeout(() => {
              sweepTimerRef.current = null;
              if (
                disposed ||
                refreshGenerationRef.current !== generation
              ) {
                return;
              }
              setSweeping(false);
            }, SWEEP_MS);
          });
      } else {
        setPullImmediate(0);
      }
    };
    const activeTouchEnded = (e: TouchEvent) => {
      const touchId = activeTouchId.current;
      return (
        touchId != null &&
        Array.from(e.changedTouches).some(
          (touch) => touch.identifier === touchId,
        )
      );
    };
    const onTouchEnd = (e: TouchEvent) => {
      if (!activeTouchEnded(e)) return;
      finishGesture(e.touches.length === 0);
    };
    const onTouchCancel = (e: TouchEvent) => {
      if (!activeTouchEnded(e)) return;
      finishGesture(false);
    };
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: false });
    el.addEventListener("touchend", onTouchEnd);
    el.addEventListener("touchcancel", onTouchCancel);
    return () => {
      disposed = true;
      refreshGenerationRef.current += 1;
      refreshingRef.current = false;
      if (pullFrameRef.current !== null) {
        window.cancelAnimationFrame(pullFrameRef.current);
        pullFrameRef.current = null;
      }
      if (sweepTimerRef.current !== null) {
        window.clearTimeout(sweepTimerRef.current);
        sweepTimerRef.current = null;
      }
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
      el.removeEventListener("touchcancel", onTouchCancel);
    };
  }, []);

  const progress = Math.min(pull / threshold, 1);
  const statusText = loading
    ? "刷新中"
    : progress >= 1
      ? "释放即可刷新"
      : dragging
        ? "下拉刷新"
        : "";

  return (
    <div
      ref={setContainerRefs}
      className={[
        "relative overflow-y-auto overscroll-y-contain h-full",
        className,
      ].join(" ")}
      style={{ overscrollBehaviorY: "contain" }}
    >
      <span className="sr-only" role="status" aria-live="polite">
        {statusText}
      </span>

      {/* 顶部 1px 琥珀进度线（触发/扫过时显） */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-px z-[6] overflow-hidden"
        style={{
          opacity: sweeping ? 1 : 0,
          transition: "opacity var(--dur-quick) linear",
        }}
      >
        <div
          className="h-full w-[40%] bg-[var(--amber-400)]"
          style={{
            boxShadow: "0 0 10px 1px var(--amber-glow-strong)",
            animation: sweeping
              ? `lumen-pt-sweep ${SWEEP_MS}ms linear`
              : undefined,
          }}
        />
      </div>

      {/* 琥珀小点（拉动提示） */}
      <div
        aria-hidden
        className="pointer-events-none sticky top-0 left-0 right-0 h-0 z-[5]"
      >
        <div
          className="absolute inset-x-0 top-0 flex items-center justify-center"
        >
          <div
            className="h-1.5 w-1.5 rounded-full bg-[var(--amber-400)]"
            style={{
              opacity: progress,
              transform: `translate3d(0, ${Math.max(
                8,
                pull * 0.72,
              )}px, 0) scale(${0.6 + progress * 0.9})`,
              transition: !dragging
                ? "transform var(--dur-panel) var(--ease-develop), opacity var(--dur-quick) linear"
                : undefined,
              boxShadow: progress > 0.95 ? "var(--shadow-amber)" : "none",
              animation: loading ? "lumen-pulse-soft 1s ease-in-out infinite" : undefined,
            }}
          />
        </div>
      </div>

      <div
        style={{
          transform: `translate3d(0, ${pull}px, 0)`,
          transition: !dragging
            ? "transform var(--dur-panel) var(--ease-develop)"
            : undefined,
        }}
      >
        {children}
      </div>

      {/* 扫光 keyframes 就地注入（避免动 globals.css） */}
      <style>{`
        @keyframes lumen-pt-sweep {
          0%   { transform: translateX(-100%); }
          100% { transform: translateX(350%); }
        }
        @media (prefers-reduced-motion: reduce) {
          @keyframes lumen-pt-sweep {
            0%, 100% { transform: translateX(0); opacity: 0.6; }
            50%      { opacity: 1; }
          }
        }
      `}</style>
    </div>
  );
}
