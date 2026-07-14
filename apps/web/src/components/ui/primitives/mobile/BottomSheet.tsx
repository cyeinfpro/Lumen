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

import {
  AnimatePresence,
  motion,
  type PanInfo,
  useDragControls,
  useIsPresent,
  useReducedMotion,
} from "framer-motion";
import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { SPRING, DURATION, EASE } from "@/lib/motion";
import { useModalLayer, usePortalReady } from "./useModalLayer";

export type SnapPoint = "auto" | `${number}%` | number;
const DEFAULT_SNAP_POINTS: SnapPoint[] = ["auto"];
const INTERACTIVE_CONTENT_SELECTOR = [
  "a[href]",
  "button",
  "input",
  "textarea",
  "select",
  "summary",
  "[contenteditable='true']",
  "[role='button']",
  "[role='slider']",
  "[data-bottom-sheet-drag-ignore]",
].join(",");

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
  /** 关闭后是否恢复触发元素焦点，默认 true。 */
  restoreFocus?: boolean;
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

export function BottomSheet({
  open,
  onClose,
  children,
  ariaLabel,
  dismissOnOverlay = true,
  dragCloseThreshold = 120,
  className = "",
  snapPoints = DEFAULT_SNAP_POINTS,
  defaultSnapIndex = 0,
  restoreFocus = true,
}: BottomSheetProps) {
  const portalReady = usePortalReady();
  if (!portalReady) return null;

  return createPortal(
    <AnimatePresence initial={false}>
      {open ? (
        <BottomSheetLayer
          key="bottom-sheet-layer"
          onClose={onClose}
          ariaLabel={ariaLabel}
          dismissOnOverlay={dismissOnOverlay}
          dragCloseThreshold={dragCloseThreshold}
          className={className}
          snapPoints={snapPoints}
          defaultSnapIndex={defaultSnapIndex}
          restoreFocus={restoreFocus}
        >
          {children}
        </BottomSheetLayer>
      ) : null}
    </AnimatePresence>,
    document.body,
  );
}

interface BottomSheetLayerProps {
  onClose: () => void;
  children: ReactNode;
  ariaLabel?: string;
  dismissOnOverlay: boolean;
  dragCloseThreshold: number;
  className: string;
  snapPoints: SnapPoint[];
  defaultSnapIndex: number;
  restoreFocus: boolean;
}

function readVisualViewport() {
  const viewport = window.visualViewport;
  return {
    height: Math.max(1, Math.round(viewport?.height ?? window.innerHeight)),
    offsetTop: Math.max(0, Math.round(viewport?.offsetTop ?? 0)),
  };
}

function BottomSheetLayer({
  onClose,
  children,
  ariaLabel,
  dismissOnOverlay,
  dragCloseThreshold,
  className,
  snapPoints,
  defaultSnapIndex,
  restoreFocus,
}: BottomSheetLayerProps) {
  const sheetRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const closingRef = useRef(false);
  const bodyDragStartRef = useRef<{
    pointerId: number;
    startY: number;
    scrollElement: HTMLElement;
  } | null>(null);
  const bodyDragControls = useDragControls();
  const isPresent = useIsPresent();
  const reduceMotion = useReducedMotion();
  const labelId = useId();
  const effectiveSnapPoints =
    snapPoints.length > 0 ? snapPoints : DEFAULT_SNAP_POINTS;
  const clampedInitial = Math.max(
    0,
    Math.min(defaultSnapIndex, effectiveSnapPoints.length - 1),
  );
  const [snapIndex, setSnapIndex] = useState<number>(clampedInitial);
  const [viewport, setViewport] = useState(readVisualViewport);

  useEffect(() => {
    closingRef.current = !isPresent;
  }, [isPresent]);

  const requestClose = useCallback(() => {
    if (closingRef.current) return;
    closingRef.current = true;
    onClose();
  }, [onClose]);

  useBodyScrollLock(true);
  const onSheetKeyDown = useModalLayer({
    open: true,
    rootRef: sheetRef,
    onClose: requestClose,
    restoreFocus,
  });

  useEffect(() => {
    const visualViewport = window.visualViewport;
    let raf = 0;
    const update = () => {
      raf = 0;
      const next = readVisualViewport();
      setViewport((current) =>
        current.height === next.height && current.offsetTop === next.offsetTop
          ? current
          : next,
      );
    };
    const scheduleUpdate = () => {
      if (raf !== 0) return;
      raf = window.requestAnimationFrame(update);
    };

    scheduleUpdate();
    window.addEventListener("resize", scheduleUpdate);
    visualViewport?.addEventListener("resize", scheduleUpdate);
    visualViewport?.addEventListener("scroll", scheduleUpdate);
    return () => {
      if (raf !== 0) window.cancelAnimationFrame(raf);
      window.removeEventListener("resize", scheduleUpdate);
      visualViewport?.removeEventListener("resize", scheduleUpdate);
      visualViewport?.removeEventListener("scroll", scheduleUpdate);
    };
  }, []);

  useEffect(() => {
    if (!isPresent) return;
    const raf = window.requestAnimationFrame(() => {
      setSnapIndex((current) => (
        current === clampedInitial ? current : clampedInitial
      ));
    });
    return () => window.cancelAnimationFrame(raf);
  }, [clampedInitial, isPresent]);

  const currentHeightPx = resolveSnapHeight(
    effectiveSnapPoints[snapIndex] ?? "auto",
    viewport.height,
  );

  // 拖拽结束：按方向切 snap 或关闭
  const handleDragEnd = useCallback(
    (_e: unknown, info: PanInfo) => {
      const dy = info.offset.y;
      const v = info.velocity.y;
      // 向下
      if (dy > 0) {
        // 先看是否已经在最低 snap 且超过关闭阈值
        const atLowest = snapIndex >= effectiveSnapPoints.length - 1;
        if (atLowest && (dy > dragCloseThreshold || v > 500)) {
          requestClose();
          return;
        }
        if (dy > 48 || v > 350) {
          setSnapIndex((i) =>
            Math.min(effectiveSnapPoints.length - 1, i + 1),
          );
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
    [
      dragCloseThreshold,
      effectiveSnapPoints.length,
      requestClose,
      snapIndex,
    ],
  );

  // 内容区只在滚动到顶且手势明确向下时才接管拖拽，避免 pointerdown
  // 后异步 setState 让 Framer Motion 抢走正常的向上滚动。
  const onContentPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const content = contentRef.current;
      const target = event.target;
      const scrollElement = closestScrollableElement(target, content);
      if (
        event.pointerType === "mouse" ||
        !content ||
        !scrollElement ||
        scrollElement.scrollTop > 0 ||
        (target instanceof Element &&
          target.closest(INTERACTIVE_CONTENT_SELECTOR))
      ) {
        bodyDragStartRef.current = null;
        return;
      }
      bodyDragStartRef.current = {
        pointerId: event.pointerId,
        startY: event.clientY,
        scrollElement,
      };
    },
    [],
  );
  const onContentPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const start = bodyDragStartRef.current;
      if (
        !start ||
        start.pointerId !== event.pointerId ||
        !start.scrollElement.isConnected ||
        start.scrollElement.scrollTop > 0
      ) {
        return;
      }
      const distance = event.clientY - start.startY;
      if (distance < -8) {
        bodyDragStartRef.current = null;
        return;
      }
      if (distance > 8) {
        bodyDragStartRef.current = null;
        bodyDragControls.start(event);
      }
    },
    [bodyDragControls],
  );
  const clearBodyDragStart = useCallback(() => {
    bodyDragStartRef.current = null;
  }, []);

  const onHandleKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSnapIndex((i) =>
          Math.min(effectiveSnapPoints.length - 1, i + 1),
        );
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSnapIndex((i) => Math.max(0, i - 1));
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (effectiveSnapPoints.length > 1) {
          setSnapIndex((i) => (i + 1) % effectiveSnapPoints.length);
        }
      }
    },
    [effectiveSnapPoints.length],
  );

  return (
    <motion.div
      data-lumen-modal-layer
      className="fixed inset-x-0 flex items-end justify-center mobile-dialog-shell"
      style={{
        zIndex: "var(--z-dialog, 90)" as unknown as number,
        top: viewport.offsetTop,
        bottom: "auto",
        height: viewport.height,
        "--mobile-dialog-viewport-height": `${viewport.height}px`,
      } as CSSProperties}
      initial="hidden"
      animate="visible"
      exit="hidden"
      variants={{ hidden: {}, visible: {} }}
    >
      <motion.div
        className="absolute inset-0 bg-[var(--surface-scrim)] backdrop-blur-sm mobile-perf-surface"
        variants={{
          hidden: { opacity: 0 },
          visible: { opacity: 1 },
        }}
        transition={
          reduceMotion
            ? { duration: 0 }
            : { duration: DURATION.normal, ease: EASE.develop }
        }
        onClick={() => dismissOnOverlay && requestClose()}
        aria-hidden
      />
      <motion.div
        ref={sheetRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelId}
        tabIndex={-1}
        onKeyDown={onSheetKeyDown}
        variants={
          reduceMotion
            ? {
                hidden: { opacity: 0 },
                visible: { opacity: 1 },
              }
            : {
                hidden: { y: "100%", opacity: 0.6 },
                visible: { y: 0, opacity: 1 },
              }
        }
        transition={reduceMotion ? { duration: 0 } : SPRING.sheet}
        style={
          currentHeightPx != null
            ? { height: currentHeightPx }
            : undefined
        }
        className={[
          "relative w-full max-w-[640px] mx-auto",
          "rounded-t-[var(--radius-sheet)] bg-[var(--bg-1)] border-t border-[var(--border-subtle)]",
          "shadow-[var(--shadow-3)]",
          "mobile-perf-surface",
          "flex min-h-0 flex-col overflow-hidden",
          currentHeightPx == null
            ? "mobile-dialog-sheet"
            : "max-h-[var(--mobile-dialog-max-height)]",
          "safe-x",
          "focus:outline-none",
          className,
        ].join(" ")}
      >
        <motion.div
          drag="y"
          dragConstraints={{ top: 0, bottom: 0 }}
          dragElastic={{ top: 0.1, bottom: 0.4 }}
          onDragEnd={handleDragEnd}
          onKeyDown={onHandleKeyDown}
          className="flex min-h-11 shrink-0 items-center justify-center cursor-grab active:cursor-grabbing touch-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]/60"
          role="slider"
          tabIndex={0}
          aria-label="调整面板高度"
          aria-valuemin={1}
          aria-valuemax={effectiveSnapPoints.length}
          aria-valuenow={snapIndex + 1}
          aria-valuetext={`第 ${snapIndex + 1} 档，共 ${effectiveSnapPoints.length} 档`}
          data-autofocus-skip
        >
          <span
            aria-hidden
            className="block h-1 w-7 rounded-full bg-[var(--fg-3)]/80"
          />
        </motion.div>

        <motion.div
          drag="y"
          dragControls={bodyDragControls}
          dragListener={false}
          dragConstraints={{ top: 0, bottom: 0 }}
          dragElastic={{ top: 0, bottom: 0.4 }}
          onDragEnd={handleDragEnd}
          className="flex min-h-0 flex-1 touch-pan-y flex-col overflow-hidden"
        >
          <div
            ref={contentRef}
            onPointerDown={onContentPointerDown}
            onPointerMove={onContentPointerMove}
            onPointerUp={clearBodyDragStart}
            onPointerCancel={clearBodyDragStart}
            className="mobile-dialog-scroll flex-1 overflow-y-auto overscroll-contain scrollbar-thin"
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
  );
}

function closestScrollableElement(
  target: EventTarget | null,
  boundary: HTMLElement | null,
): HTMLElement | null {
  if (!boundary) return null;
  let element: Element | null =
    target instanceof Element ? target : boundary;
  while (element && boundary.contains(element)) {
    if (
      element instanceof HTMLElement &&
      element.scrollHeight > element.clientHeight
    ) {
      const overflowY = window.getComputedStyle(element).overflowY;
      if (
        overflowY === "auto" ||
        overflowY === "scroll" ||
        overflowY === "overlay"
      ) {
        return element;
      }
    }
    if (element === boundary) break;
    element = element.parentElement;
  }
  return boundary;
}
