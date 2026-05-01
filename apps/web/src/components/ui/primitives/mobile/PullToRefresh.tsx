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
import { pushMobileToast } from "./Toast";

const DAMPING = 0.55; // 拖拽阻力（经验值，iOS Safari 手感接近原生）

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
  const startY = useRef<number | null>(null);
  const pullRef = useRef(0);
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
    const onTouchStart = (e: TouchEvent) => {
      if (!ref.current) return;
      if (ref.current.scrollTop > 0) {
        startY.current = null;
        setDragging(false);
        return;
      }
      startY.current = e.touches[0].clientY;
      setDragging(true);
    };
    const onTouchMove = (e: TouchEvent) => {
      if (startY.current == null) return;
      const dy = e.touches[0].clientY - startY.current;
      if (dy <= 0) {
        if (pullRef.current !== 0) {
          pullRef.current = 0;
          setPull(0);
        }
        return;
      }
      const { threshold: currentThreshold } = latestRef.current;
      // 阻力函数
      const resisted = Math.min(dy * DAMPING, currentThreshold * 2);
      pullRef.current = resisted;
      setPull(resisted);
      if (resisted > 10 && e.cancelable) e.preventDefault();
    };
    const onTouchEnd = () => {
      const {
        loading: currentLoading,
        onRefresh: currentOnRefresh,
        threshold: currentThreshold,
      } = latestRef.current;
      startY.current = null;
      setDragging(false);
      const finalPull = pullRef.current;
      if (finalPull >= currentThreshold && !currentLoading) {
        setLoading(true);
        setSweeping(true);
        pullRef.current = currentThreshold;
        setPull(currentThreshold);
        Promise.resolve(currentOnRefresh())
          .catch((err) => {
            // 把 catch 里吞掉的错误暴露给用户 —— 至少给个 toast,避免静默失败
            const msg =
              err instanceof Error && err.message ? err.message : "刷新失败";
            pushMobileToast(msg, "danger");
          })
          .finally(() => {
            setLoading(false);
            pullRef.current = 0;
            setPull(0);
            // 扫光动画 540ms 后收尾
            window.setTimeout(() => setSweeping(false), 540);
          });
      } else {
        pullRef.current = 0;
        setPull(0);
      }
    };
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: false });
    el.addEventListener("touchend", onTouchEnd);
    el.addEventListener("touchcancel", onTouchEnd);
    return () => {
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      el.removeEventListener("touchend", onTouchEnd);
      el.removeEventListener("touchcancel", onTouchEnd);
    };
  }, []);

  const progress = Math.min(pull / threshold, 1);

  return (
    <div
      ref={setContainerRefs}
      className={[
        "relative overflow-y-auto overscroll-y-contain h-full",
        className,
      ].join(" ")}
      style={{ overscrollBehaviorY: "contain" }}
    >
      {/* 顶部 1px 琥珀进度线（触发/扫过时显） */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-px z-[6] overflow-hidden"
        style={{ opacity: sweeping ? 1 : 0, transition: "opacity 140ms linear" }}
      >
        <div
          className="h-full w-[40%] bg-[var(--amber-400)]"
          style={{
            boxShadow: "0 0 10px 1px var(--amber-glow-strong)",
            animation: sweeping ? "lumen-pt-sweep 540ms linear" : undefined,
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
          style={{
            height: pull,
            transition: !dragging ? "height 220ms var(--ease-develop)" : undefined,
          }}
        >
          <div
            role="status"
            aria-label={loading ? "正在刷新" : progress >= 1 ? "释放刷新" : "下拉刷新"}
            className="h-1.5 w-1.5 rounded-full bg-[var(--amber-400)]"
            style={{
              opacity: progress,
              transform: `scale(${0.6 + progress * 0.9})`,
              boxShadow: progress > 0.95 ? "var(--shadow-amber)" : "none",
              animation: loading ? "lumen-pulse-soft 1s ease-in-out infinite" : undefined,
            }}
          />
        </div>
      </div>

      <div
        style={{
          transform: `translateY(${pull}px)`,
          transition: !dragging ? "transform 220ms var(--ease-develop)" : undefined,
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
