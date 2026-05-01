"use client";

// BottomSheet · spec §9.1
// - snapPoints: ("auto" | "NN%" | px)[]，在给定点之间拖拽切换
// - 顶部 28×4 handle 灰条，拖 handle 切换 snap；拖 sheet 其它区域且内部 scrollTop===0 时也可下拉关闭
// - 遮罩 bg-black/50 backdrop-blur-sm，背景点击关闭
// - role=dialog aria-modal，focus trap（Tab 循环），Esc 关闭，之前焦点恢复
// - 内部滚动与外部拖拽冲突：sheet 内容滚到顶才触发关闭
//
// React 19 规则：
//   - 不在 render 阶段读 ref
//   - effect 的 setState 走保护（比较后才 set），避免 loop

import { AnimatePresence, motion, type PanInfo } from "framer-motion";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { SPRING, DURATION, EASE } from "@/lib/motion";

export type SnapPoint = "auto" | `${number}%` | number;

export interface BottomSheetProps {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  /** 标题会被 screen reader 朗读；视觉可自行渲染 */
  ariaLabel?: string;
  /** 点遮罩是否关闭，默认 true */
  dismissOnOverlay?: boolean;
  /** 下拉关闭距离阈值（相对于当前 snap），默认 120px */
  dragCloseThreshold?: number;
  /** 自定义容器类名 */
  className?: string;
  /**
   * snap 点序列，自高到低。支持 "auto"（按内容）/ "NN%"（视口百分比）/ px 数字。
   * 默认 ["auto"]，即单一高度。
   */
  snapPoints?: SnapPoint[];
  /** 初始停靠点索引（默认 0，即最高位）。 */
  defaultSnapIndex?: number;
}

// SPRING.sheet 已在 @/lib/motion 统一定义，此处直接引用

/** 把 SnapPoint 解析成像素高度；"auto" 交由 null（让浏览器自适应） */
function resolveSnapHeight(p: SnapPoint, viewportH: number): number | null {
  if (p === "auto") return null;
  if (typeof p === "number") return Math.max(0, Math.min(p, viewportH));
  // NN%
  const m = /^(\d+(?:\.\d+)?)%$/.exec(p);
  if (m) {
    const pct = parseFloat(m[1]);
    return Math.max(0, Math.min((pct / 100) * viewportH, viewportH));
  }
  return null;
}

const FOCUSABLE_SEL = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled]):not([type=hidden])",
  "select:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function BottomSheet({
  open,
  onClose,
  children,
  ariaLabel,
  dismissOnOverlay = true,
  dragCloseThreshold = 120,
  className = "",
  snapPoints = ["auto"],
  defaultSnapIndex = 0,
}: BottomSheetProps) {
  const sheetRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const labelId = useId();

  const [viewportH, setViewportH] = useState<number>(
    typeof window === "undefined" ? 800 : window.innerHeight,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onResize = () => setViewportH(window.innerHeight);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const clampedInitial = useMemo(
    () => Math.max(0, Math.min(defaultSnapIndex, snapPoints.length - 1)),
    [defaultSnapIndex, snapPoints.length],
  );
  const [snapIndex, setSnapIndex] = useState<number>(clampedInitial);

  // open 变化时重置到初始 snap；放到下一帧，避免 React 19 的 effect 内同步 setState。
  useEffect(() => {
    if (!open) return;
    const raf = window.requestAnimationFrame(() => {
      setSnapIndex((current) => (
        current === clampedInitial ? current : clampedInitial
      ));
    });
    return () => window.cancelAnimationFrame(raf);
  }, [open, clampedInitial]);

  const currentHeightPx = useMemo(
    () => resolveSnapHeight(snapPoints[snapIndex] ?? "auto", viewportH),
    [snapPoints, snapIndex, viewportH],
  );

  // 焦点管理 + Esc + body 滚动锁
  useEffect(() => {
    if (!open) return;
    previouslyFocusedRef.current =
      (document.activeElement as HTMLElement) ?? null;

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);

    const t = window.setTimeout(() => {
      // 聚焦第一个可聚焦元素；无则聚焦 sheet 本体
      const el = sheetRef.current;
      if (!el) return;
      const first = el.querySelector<HTMLElement>(FOCUSABLE_SEL);
      (first ?? el).focus();
    }, 60);

    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      document.removeEventListener("keydown", onKey);
      window.clearTimeout(t);
      document.body.style.overflow = prevOverflow;
      previouslyFocusedRef.current?.focus?.();
    };
  }, [open, onClose]);

  // focus trap：Tab / Shift+Tab 在 sheet 内循环
  const onSheetKeyDown = useCallback((e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "Tab") return;
    const root = sheetRef.current;
    if (!root) return;
    const nodes = Array.from(
      root.querySelectorAll<HTMLElement>(FOCUSABLE_SEL),
    ).filter((n) => !n.hasAttribute("data-focus-skip"));
    if (nodes.length === 0) {
      e.preventDefault();
      root.focus();
      return;
    }
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement as HTMLElement | null;
    if (e.shiftKey) {
      if (active === first || !root.contains(active)) {
        e.preventDefault();
        last.focus();
      }
    } else {
      if (active === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }, []);

  // 拖拽结束：按方向切 snap 或关闭
  const handleDragEnd = useCallback(
    (_e: unknown, info: PanInfo) => {
      const dy = info.offset.y;
      const v = info.velocity.y;
      // 向下
      if (dy > 0) {
        // 先看是否已经在最低 snap 且超过关闭阈值
        const atLowest = snapIndex >= snapPoints.length - 1;
        if (atLowest && (dy > dragCloseThreshold || v > 500)) {
          onClose();
          return;
        }
        if (dy > 48 || v > 350) {
          setSnapIndex((i) => Math.min(snapPoints.length - 1, i + 1));
          return;
        }
      }
      // 向上
      if (dy < 0) {
        if (-dy > 48 || -v > 350) {
          setSnapIndex((i) => Math.max(0, i - 1));
          return;
        }
      }
      // 其它 → 回弹（靠 animate 自动回到 0）
    },
    [snapIndex, snapPoints.length, dragCloseThreshold, onClose],
  );

  // 决定 sheet 主体是否允许 drag（内部滚到顶才允许下拉关闭）
  // 基于 pointer 事件开始时的 scrollTop 判断
  const [bodyDragLocked, setBodyDragLocked] = useState(false);
  const onContentPointerDown = useCallback(() => {
    const sc = contentRef.current;
    // 内部有滚动且还没滚到顶 → 锁住 body drag（避免与内滚冲突）
    if (sc && sc.scrollTop > 0) setBodyDragLocked(true);
    else setBodyDragLocked(false);
  }, []);
  const onContentPointerUp = useCallback(() => {
    setBodyDragLocked(false);
  }, []);

  if (typeof document === "undefined") return null;

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          key="bs-root"
          className="fixed inset-0 flex items-end justify-center"
          style={{ zIndex: "var(--z-dialog, 90)" as unknown as number }}
          initial="hidden"
          animate="visible"
          exit="hidden"
          variants={{ hidden: {}, visible: {} }}
        >
          <motion.div
            key="bs-overlay"
            className="absolute inset-0 bg-black/50 backdrop-blur-sm mobile-perf-surface"
            variants={{
              hidden: { opacity: 0 },
              visible: { opacity: 1 },
            }}
            transition={{ duration: DURATION.normal, ease: EASE.develop }}
            onClick={() => dismissOnOverlay && onClose()}
            aria-hidden
          />
          <motion.div
            key="bs-sheet"
            ref={sheetRef}
            role="dialog"
            aria-modal="true"
            aria-label={ariaLabel ?? "底部面板"}
            aria-labelledby={labelId}
            tabIndex={-1}
            onKeyDown={onSheetKeyDown}
            variants={{
              hidden: { y: "100%", opacity: 0.6 },
              visible: { y: 0, opacity: 1 },
            }}
            transition={SPRING.sheet}
            style={
              currentHeightPx != null
                ? { height: currentHeightPx }
                : undefined
            }
            className={[
              "relative w-full max-w-[640px] mx-auto",
              "rounded-t-[24px] bg-[var(--bg-1)] border-t border-[var(--border-subtle)]",
              "shadow-[0_-24px_64px_-12px_rgba(0,0,0,0.8)]",
              "mobile-perf-surface",
              "flex flex-col",
              currentHeightPx == null ? "max-h-[88vh]" : "",
              "pb-[env(safe-area-inset-bottom)]",
              "safe-x",
              "focus:outline-none",
              className,
            ].join(" ")}
          >
            {/* 拖拽 handle：独立 drag 区 */}
            <motion.div
              drag="y"
              dragConstraints={{ top: 0, bottom: 0 }}
              dragElastic={{ top: 0.1, bottom: 0.4 }}
              onDragEnd={handleDragEnd}
              className="flex justify-center py-2 cursor-grab active:cursor-grabbing touch-none"
              role="button"
              tabIndex={0}
              aria-label="调整高度"
              data-focus-skip
            >
              <span
                aria-hidden
                className="block h-1 w-7 rounded-full bg-[var(--fg-3)]/80"
              />
            </motion.div>

            {/* 内容区：整体也能下拉（仅在 scrollTop===0 时） */}
            <motion.div
              drag={bodyDragLocked ? false : "y"}
              dragConstraints={{ top: 0, bottom: 0 }}
              dragElastic={bodyDragLocked ? 0 : { top: 0, bottom: 0.4 }}
              onDragEnd={handleDragEnd}
              className="flex-1 min-h-0 flex flex-col"
            >
              <div
                ref={contentRef}
                onPointerDown={onContentPointerDown}
                onPointerUp={onContentPointerUp}
                onPointerCancel={onContentPointerUp}
                className="flex-1 overflow-y-auto overscroll-contain scrollbar-thin"
                style={{ overscrollBehaviorY: "contain" }}
              >
                <span id={labelId} className="sr-only">
                  {ariaLabel ?? "底部面板"}
                </span>
                {children}
              </div>
            </motion.div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
