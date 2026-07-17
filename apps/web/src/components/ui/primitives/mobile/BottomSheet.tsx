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
  animate,
  motion,
  type PanInfo,
  useDragControls,
  useIsPresent,
  useMotionValue,
  useReducedMotion,
} from "framer-motion";
import { X } from "lucide-react";
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
import {
  DURATION,
  EASE,
  GESTURE,
  SPRING,
  projectMomentum,
} from "@/lib/motion";
import { Pressable } from "./Pressable";
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
    startX: number;
    startY: number;
    scrollElement: HTMLElement;
  } | null>(null);
  const settleAnimationRef = useRef<{ stop: () => void } | null>(null);
  const settleFrameRef = useRef(0);
  const sheetDragControls = useDragControls();
  const sheetY = useMotionValue(0);
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

  const stopSettleAnimation = useCallback(() => {
    if (settleFrameRef.current !== 0) {
      window.cancelAnimationFrame(settleFrameRef.current);
      settleFrameRef.current = 0;
    }
    settleAnimationRef.current?.stop();
    settleAnimationRef.current = null;
  }, []);

  const settleSheet = useCallback(() => {
    stopSettleAnimation();
    if (reduceMotion) {
      sheetY.set(0);
      return;
    }
    const control = animate(sheetY, 0, SPRING.sheet);
    settleAnimationRef.current = control;
    void control.then(() => {
      if (settleAnimationRef.current === control) {
        settleAnimationRef.current = null;
      }
    });
  }, [reduceMotion, sheetY, stopSettleAnimation]);

  useEffect(() => {
    return () => stopSettleAnimation();
  }, [stopSettleAnimation]);

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
      sheetY.set(0);
      setSnapIndex((current) => (
        current === clampedInitial ? current : clampedInitial
      ));
    });
    return () => window.cancelAnimationFrame(raf);
  }, [clampedInitial, isPresent, sheetY]);

  const currentHeightPx = resolveSnapHeight(
    effectiveSnapPoints[snapIndex] ?? "auto",
    viewport.height,
  );

  const readSheetHeightLimit = useCallback(() => {
    const sheet = sheetRef.current;
    if (!sheet) return viewport.height;
    const maxHeight = Number.parseFloat(
      window.getComputedStyle(sheet).maxHeight,
    );
    return Number.isFinite(maxHeight) && maxHeight > 0
      ? Math.min(maxHeight, viewport.height)
      : viewport.height;
  }, [viewport.height]);

  const readSnapHeight = useCallback(
    (index: number) => {
      const point = effectiveSnapPoints[index] ?? "auto";
      const limit = readSheetHeightLimit();
      const resolved = resolveSnapHeight(point, viewport.height);
      if (resolved != null) return Math.min(resolved, limit);
      const handleHeight = 44;
      const naturalContentHeight =
        contentRef.current?.scrollHeight ??
        sheetRef.current?.scrollHeight ??
        limit;
      return Math.min(
        limit,
        Math.max(handleHeight, naturalContentHeight + handleHeight),
      );
    },
    [effectiveSnapPoints, readSheetHeightLimit, viewport.height],
  );

  const settleToSnap = useCallback(
    (targetIndex: number) => {
      const nextIndex = Math.max(
        0,
        Math.min(targetIndex, effectiveSnapPoints.length - 1),
      );
      const currentHeight =
        sheetRef.current?.getBoundingClientRect().height ??
        readSnapHeight(snapIndex);
      const targetHeight = readSnapHeight(nextIndex);
      const heightDelta = currentHeight - targetHeight;
      const releaseY = sheetY.get();

      stopSettleAnimation();
      sheetY.set(releaseY - heightDelta);
      setSnapIndex(nextIndex);
      settleFrameRef.current = window.requestAnimationFrame(() => {
        settleFrameRef.current = 0;
        settleSheet();
      });
    },
    [
      effectiveSnapPoints.length,
      readSnapHeight,
      settleSheet,
      sheetY,
      snapIndex,
      stopSettleAnimation,
    ],
  );

  // 拖拽结束：按方向切 snap 或关闭
  const handleDragEnd = useCallback(
    (event: { type?: string }, info: PanInfo) => {
      if (
        event.type === "pointercancel" ||
        event.type === "touchcancel"
      ) {
        settleSheet();
        return;
      }
      const dy = info.offset.y;
      const v = info.velocity.y;
      const projectedY = dy + projectMomentum(v);
      const direction =
        Math.abs(v) >= GESTURE.snapVelocity
          ? Math.sign(v)
          : Math.sign(projectedY);
      // 向下
      if (direction > 0) {
        // 先看是否已经在最低 snap 且超过关闭阈值
        const atLowest = snapIndex >= effectiveSnapPoints.length - 1;
        if (
          atLowest &&
          (projectedY > dragCloseThreshold ||
            v > GESTURE.dismissVelocity)
        ) {
          requestClose();
          return;
        }
        if (
          projectedY > GESTURE.snapDistance ||
          v > GESTURE.snapVelocity
        ) {
          settleToSnap(snapIndex + 1);
          return;
        }
      }
      // 向上
      if (direction < 0) {
        if (
          -projectedY > GESTURE.snapDistance ||
          -v > GESTURE.snapVelocity
        ) {
          settleToSnap(snapIndex - 1);
          return;
        }
      }
      settleSheet();
    },
    [
      dragCloseThreshold,
      effectiveSnapPoints.length,
      requestClose,
      settleSheet,
      settleToSnap,
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
        !event.isPrimary ||
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
        startX: event.clientX,
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
      const horizontalDistance = event.clientX - start.startX;
      const distance = event.clientY - start.startY;
      if (
        Math.max(
          Math.abs(horizontalDistance),
          Math.abs(distance),
        ) < GESTURE.intentSlop
      ) {
        return;
      }
      if (
        distance <= 0 ||
        Math.abs(distance) <=
          Math.abs(horizontalDistance) * GESTURE.directionBias
      ) {
        bodyDragStartRef.current = null;
        return;
      }
      bodyDragStartRef.current = null;
      stopSettleAnimation();
      sheetDragControls.start(event);
      if (event.cancelable) event.preventDefault();
    },
    [sheetDragControls, stopSettleAnimation],
  );
  const clearBodyDragStart = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (bodyDragStartRef.current?.pointerId === event.pointerId) {
        bodyDragStartRef.current = null;
      }
    },
    [],
  );

  const onHandleKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        settleToSnap(snapIndex + 1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        settleToSnap(snapIndex - 1);
      } else if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (effectiveSnapPoints.length > 1) {
          settleToSnap((snapIndex + 1) % effectiveSnapPoints.length);
        }
      }
    },
    [effectiveSnapPoints.length, settleToSnap, snapIndex],
  );

  const dragTravel = Math.max(
    GESTURE.snapDistance * 2,
    Math.min(240, viewport.height * 0.34),
  );
  const dragConstraints = {
    top: snapIndex > 0 ? -dragTravel : 0,
    bottom:
      snapIndex < effectiveSnapPoints.length - 1
        ? dragTravel
        : Math.max(dragTravel, dragCloseThreshold * 1.5),
  };
  const sliderValue = effectiveSnapPoints.length - snapIndex;

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
        className="relative mx-auto w-full max-w-[640px] rounded-t-[var(--radius-sheet)] bg-[var(--bg-1)]"
      >
        <motion.div
          ref={sheetRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={labelId}
          tabIndex={-1}
          onKeyDown={onSheetKeyDown}
          drag="y"
          dragControls={sheetDragControls}
          dragListener={false}
          dragConstraints={dragConstraints}
          dragElastic={{ top: 0.12, bottom: 0.18 }}
          dragMomentum={false}
          onDragStart={stopSettleAnimation}
          onDragEnd={handleDragEnd}
          style={{
            y: sheetY,
            ...(currentHeightPx != null ? { height: currentHeightPx } : {}),
          }}
          className={[
            "relative w-full",
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
          <div
            onPointerDown={(event) => {
              if (
                event.button !== 0 ||
                !event.isPrimary ||
                closingRef.current
              ) {
                return;
              }
              stopSettleAnimation();
              sheetDragControls.start(event);
            }}
            onKeyDown={onHandleKeyDown}
            className="flex min-h-11 shrink-0 items-center justify-center cursor-grab active:cursor-grabbing touch-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]/60"
            role="slider"
            tabIndex={0}
            aria-label="调整面板高度"
            aria-orientation="vertical"
            aria-valuemin={1}
            aria-valuemax={effectiveSnapPoints.length}
            aria-valuenow={sliderValue}
            aria-valuetext={`高度第 ${sliderValue} 级，共 ${effectiveSnapPoints.length} 级`}
            data-autofocus-skip
          >
            <span
              aria-hidden
              className="block h-1 w-7 rounded-full bg-[var(--fg-3)]/80"
            />
          </div>

          <div className="absolute right-1 top-0 z-20">
            <Pressable
              size="default"
              minHit
              pressScale="tight"
              haptic="light"
              onPress={requestClose}
              aria-label="关闭面板"
              data-autofocus-skip
              className="h-11 w-11 rounded-full text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]"
            >
              <X className="h-4 w-4" aria-hidden />
            </Pressable>
          </div>

          <div className="flex min-h-0 flex-1 touch-pan-y flex-col overflow-hidden">
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
