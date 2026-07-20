// LightboxGestures —— 手写 pointer events 手势层（client-only）。
//
// 对外暴露一个 `useLightboxGestures({...})` hook：挂到图片容器上即可获得
// 水平滑 / 下拉关闭 / 上拉展开 / pinch / 双击 / 单击 / 长按 菜单。
//
// React 19 规则：render 阶段不读 ref；ref 仅在 effect / event handler 内读写。
// 手势是"连续读值 + 偶尔写 state"，所以：
//   - 坐标 / 指针列表 / 当前模式：ref（热路径，避免每帧 setState）
//   - 外部可见状态（pinch zoom、translate.x）：framer-motion 的 MotionValue
// MotionValue 由调用方传入（用 useMotionValue 创建），hook 只读写。

import { useEffect, useRef } from "react";
import type { MotionValue } from "framer-motion";

import { projectMomentum, rubberBandDistance } from "@/lib/motion";

export type GestureMode =
  | "idle"
  | "swipe-h" // 横向切图
  | "dismiss" // 下拉关闭
  | "reveal" // 上拉展开参数面板
  | "pan" // pinch 放大后平移
  | "pinch"; // 双指缩放

export interface LightboxGestureCallbacks {
  onSwipeLeft: () => boolean | void; // 去下一张；返回 true 表示外部接管回弹动画
  onSwipeRight: () => boolean | void; // 去上一张；返回 true 表示外部接管回弹动画
  onDismiss: () => void; // 下拉关闭
  onRevealOpen: () => void; // 上拉展开参数面板
  onRevealClose: () => void; // 再上拉 / 下拉折叠参数面板
  onTap: () => void; // 单击，切换 chrome
  onDoubleTap: () => void; // 双击，1x <-> 2x
  onLongPress?: () => void; // 长按 600ms（默认不阻止 context menu）
  onPointerActivity?: () => void; // 任何 pointer 事件触发：重置 chrome 3s 计时器
  onBoundarySwipe?: (edge: "first" | "last") => void; // 首末张继续滑动：轻提示 / haptic
}

export interface LightboxGestureOptions {
  /** 按需绑定事件；未传时保持旧行为。 */
  enabled?: boolean;
  /** 屏幕宽度 / 高度（调用方提供；默认从 window 读） */
  width?: number;
  height?: number;
  /** 当前是否处在参数面板展开态（展开时上拉改为折叠） */
  revealOpen: boolean;
  /** 首末张标记：限制切图方向（橡皮筋） */
  isFirst: boolean;
  isLast: boolean;
  /** motion values：手势直接驱动（不 setState） */
  dragX: MotionValue<number>;
  dragY: MotionValue<number>;
  scale: MotionValue<number>;
  haloOpacity: MotionValue<number>;
}

const SWIPE_DX_RATIO = 0.4; // 超过 40% 屏宽触发切图
const DISMISS_DY = 120;
const REVEAL_DY = -80;
const LONG_PRESS_MS = 600;
const DOUBLE_TAP_MS = 280;
const TAP_SLOP = 8;
const MAX_SCALE = 4;
const POINTER_ACTIVITY_MOVE_THROTTLE_MS = 180;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function isVerticalMode(mode: GestureMode): boolean {
  return mode === "dismiss" || mode === "reveal";
}

function rubberClamp(
  value: number,
  min: number,
  max: number,
  dimension: number,
): number {
  if (value < min) {
    return min + rubberBandDistance(value - min, dimension);
  }
  if (value > max) {
    return max + rubberBandDistance(value - max, dimension);
  }
  return value;
}

export function useLightboxGestures(
  target: React.RefObject<HTMLElement | null>,
  callbacks: LightboxGestureCallbacks,
  options: LightboxGestureOptions,
) {
  // 所有热路径状态放 ref —— render 阶段不会读
  const modeRef = useRef<GestureMode>("idle");
  const startXRef = useRef(0);
  const startYRef = useRef(0);
  const lastXRef = useRef(0);
  const lastYRef = useRef(0);
  const startTimeRef = useRef(0);
  const lastMoveTimeRef = useRef(0);
  const lastTapTimeRef = useRef(0);
  const tapTimerRef = useRef<number | null>(null);
  const longPressTimerRef = useRef<number | null>(null);
  const pointersRef = useRef<Map<number, { x: number; y: number }>>(new Map());
  const pinchStartDistRef = useRef(0);
  const pinchStartScaleRef = useRef(1);
  const panStartXRef = useRef(0);
  const panStartYRef = useRef(0);
  const movedRef = useRef(false);
  const vxRef = useRef(0);
  const vyRef = useRef(0);
  const lastActivityEmitRef = useRef(0);
  const rafRef = useRef<number | null>(null);
  const pendingMotionRef = useRef<{
    dragX?: number;
    dragY?: number;
    scale?: number;
    haloOpacity?: number;
  }>({});

  // callbacks / options 用 ref 保存最新值（避免 effect 重新绑定事件）
  const cbRef = useRef(callbacks);
  const optRef = useRef(options);
  const enabled = options.enabled !== false;

  useEffect(() => {
    cbRef.current = callbacks;
    optRef.current = options;
  });

  useEffect(() => {
    const pointers = pointersRef.current;
    const clearTapTimer = () => {
      if (tapTimerRef.current !== null) {
        window.clearTimeout(tapTimerRef.current);
        tapTimerRef.current = null;
      }
    };
    if (!enabled) {
      clearTapTimer();
      lastTapTimeRef.current = 0;
      pointers.clear();
      modeRef.current = "idle";
      return;
    }

    const el = target.current;
    if (!el) return;

    const getWidth = () => optRef.current.width ?? window.innerWidth;
    const getHeight = () => optRef.current.height ?? window.innerHeight;

    const flushPendingMotion = () => {
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      const pending = pendingMotionRef.current;
      pendingMotionRef.current = {};
      if (pending.dragX !== undefined) optRef.current.dragX.set(pending.dragX);
      if (pending.dragY !== undefined) optRef.current.dragY.set(pending.dragY);
      if (pending.scale !== undefined) optRef.current.scale.set(pending.scale);
      if (pending.haloOpacity !== undefined) {
        optRef.current.haloOpacity.set(pending.haloOpacity);
      }
    };

    const scheduleMotion = (next: typeof pendingMotionRef.current) => {
      Object.assign(pendingMotionRef.current, next);
      if (rafRef.current !== null) return;
      rafRef.current = window.requestAnimationFrame(() => {
        rafRef.current = null;
        const pending = pendingMotionRef.current;
        pendingMotionRef.current = {};
        if (pending.dragX !== undefined) optRef.current.dragX.set(pending.dragX);
        if (pending.dragY !== undefined) optRef.current.dragY.set(pending.dragY);
        if (pending.scale !== undefined) optRef.current.scale.set(pending.scale);
        if (pending.haloOpacity !== undefined) {
          optRef.current.haloOpacity.set(pending.haloOpacity);
        }
      });
    };

    const emitPointerActivity = (force = false) => {
      if (!cbRef.current.onPointerActivity) return;
      const now = performance.now();
      if (!force && now - lastActivityEmitRef.current < POINTER_ACTIVITY_MOVE_THROTTLE_MS) {
        return;
      }
      lastActivityEmitRef.current = now;
      cbRef.current.onPointerActivity();
    };

    const panBounds = () => {
      const currentScale = optRef.current.scale.get();
      if (currentScale <= 1.01) return { x: 0, y: 0 };
      return {
        x: Math.max(0, getWidth() * (currentScale - 1) * 0.5 + 48),
        y: Math.max(0, getHeight() * (currentScale - 1) * 0.5 + 48),
      };
    };

    const clearLongPress = () => {
      if (longPressTimerRef.current !== null) {
        window.clearTimeout(longPressTimerRef.current);
        longPressTimerRef.current = null;
      }
    };

    const resetPosition = () => {
      const { dragX, dragY, haloOpacity } = optRef.current;
      flushPendingMotion();
      dragX.set(0);
      dragY.set(0);
      haloOpacity.set(1);
    };

    const onPointerDown = (e: PointerEvent) => {
      // 只处理主指针（鼠标左键 / 触摸 / 笔）
      if (e.pointerType === "mouse" && e.button !== 0) return;
      pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
      emitPointerActivity(true);

      if (pointersRef.current.size === 2) {
        // 进入 pinch 模式
        clearLongPress();
        modeRef.current = "pinch";
        const pts = Array.from(pointersRef.current.values());
        pinchStartDistRef.current = Math.hypot(
          pts[0].x - pts[1].x,
          pts[0].y - pts[1].y,
        );
        pinchStartScaleRef.current = optRef.current.scale.get();
        return;
      }

      // 单指起点
      startXRef.current = e.clientX;
      startYRef.current = e.clientY;
      lastXRef.current = e.clientX;
      lastYRef.current = e.clientY;
      startTimeRef.current = performance.now();
      lastMoveTimeRef.current = startTimeRef.current;
      movedRef.current = false;
      vxRef.current = 0;
      vyRef.current = 0;

      if (optRef.current.scale.get() > 1.01) {
        // 放大状态下单指 = pan
        modeRef.current = "pan";
        panStartXRef.current = optRef.current.dragX.get();
        panStartYRef.current = optRef.current.dragY.get();
      } else {
        modeRef.current = "idle";
        panStartXRef.current = 0;
        panStartYRef.current = 0;
      }

      // 长按检测（不阻止 context menu，浏览器自动弹出保存菜单；这里只做 haptic 之类）
      clearLongPress();
      if (cbRef.current.onLongPress) {
        longPressTimerRef.current = window.setTimeout(() => {
          if (!movedRef.current) {
            cbRef.current.onLongPress?.();
          }
          longPressTimerRef.current = null;
        }, LONG_PRESS_MS);
      }

      try {
        el.setPointerCapture(e.pointerId);
      } catch {
        // 某些浏览器（尤其 passive：true 下）会抛，静默即可
      }
    };

    const handlePinchMove = (e: PointerEvent): boolean => {
      if (modeRef.current !== "pinch" || pointersRef.current.size !== 2) {
        return false;
      }
      const pts = Array.from(pointersRef.current.values());
      const distance = Math.hypot(
        pts[0].x - pts[1].x,
        pts[0].y - pts[1].y,
      );
      if (pinchStartDistRef.current > 0) {
        const scale = clamp(
          pinchStartScaleRef.current *
            (distance / pinchStartDistRef.current),
          1,
          MAX_SCALE,
        );
        scheduleMotion({ scale });
        if (scale <= 1.01) scheduleMotion({ dragX: 0, dragY: 0 });
      }
      movedRef.current = true;
      clearLongPress();
      if (e.cancelable) e.preventDefault();
      return true;
    };

    const updatePointerVelocity = (e: PointerEvent, now: number) => {
      const dt = Math.max(1, now - lastMoveTimeRef.current);
      vxRef.current = ((e.clientX - lastXRef.current) / dt) * 1000;
      vyRef.current = ((e.clientY - lastYRef.current) / dt) * 1000;
      lastXRef.current = e.clientX;
      lastYRef.current = e.clientY;
      lastMoveTimeRef.current = now;
    };

    const handlePanMove = (
      e: PointerEvent,
      dx: number,
      dy: number,
    ): boolean => {
      if (modeRef.current !== "pan") return false;
      const bounds = panBounds();
      scheduleMotion({
        dragX: rubberClamp(
          panStartXRef.current + dx,
          -bounds.x,
          bounds.x,
          getWidth(),
        ),
        dragY: rubberClamp(
          panStartYRef.current + dy,
          -bounds.y,
          bounds.y,
          getHeight(),
        ),
      });
      if (e.cancelable) e.preventDefault();
      return true;
    };

    const selectMoveMode = (dx: number, dy: number) => {
      if (modeRef.current !== "idle" || !movedRef.current) return;
      if (Math.abs(dx) > Math.abs(dy)) {
        modeRef.current = "swipe-h";
        return;
      }
      modeRef.current = dy > 0 ? "dismiss" : "reveal";
    };

    const handleDirectionalMove = (
      e: PointerEvent,
      dx: number,
      dy: number,
    ) => {
      if (modeRef.current === "swipe-h") {
        const atBoundary =
          (optRef.current.isFirst && dx > 0) ||
          (optRef.current.isLast && dx < 0);
        const dragX = atBoundary
          ? rubberBandDistance(dx, getWidth())
          : dx;
        scheduleMotion({ dragX });
        if (e.cancelable) e.preventDefault();
        return;
      }
      if (!isVerticalMode(modeRef.current)) return;
      const dragY = dy < 0 ? rubberBandDistance(dy, getHeight()) : dy;
      const haloOpacity = clamp(1 - Math.max(0, dy) / 400, 0, 1);
      scheduleMotion({ dragY, haloOpacity });
      if (e.cancelable) e.preventDefault();
    };

    const onPointerMove = (e: PointerEvent) => {
      const pointer = pointersRef.current.get(e.pointerId);
      if (!pointer) return;
      pointer.x = e.clientX;
      pointer.y = e.clientY;
      emitPointerActivity();
      if (handlePinchMove(e)) return;

      const dx = e.clientX - startXRef.current;
      const dy = e.clientY - startYRef.current;
      if (
        !movedRef.current &&
        (Math.abs(dx) > TAP_SLOP || Math.abs(dy) > TAP_SLOP)
      ) {
        movedRef.current = true;
        clearLongPress();
      }
      updatePointerVelocity(e, performance.now());
      if (handlePanMove(e, dx, dy)) return;
      selectMoveMode(dx, dy);
      handleDirectionalMove(e, dx, dy);
    };

    const continuePinchAfterRelease = (): boolean => {
      if (modeRef.current !== "pinch") return false;
      if (pointersRef.current.size === 1) {
        const [[, point]] = pointersRef.current.entries();
        startXRef.current = point.x;
        startYRef.current = point.y;
        modeRef.current = optRef.current.scale.get() > 1.01 ? "pan" : "idle";
        panStartXRef.current = optRef.current.dragX.get();
        panStartYRef.current = optRef.current.dragY.get();
        return true;
      }
      if (pointersRef.current.size === 0) {
        modeRef.current = "idle";
        if (optRef.current.scale.get() <= 1.01) {
          optRef.current.scale.set(1);
          resetPosition();
        }
      }
      return true;
    };

    const handleTapRelease = (duration: number): boolean => {
      if (movedRef.current || duration >= 300) return false;
      const now = performance.now();
      const gap = now - lastTapTimeRef.current;
      clearTapTimer();
      if (gap < DOUBLE_TAP_MS) {
        lastTapTimeRef.current = 0;
        cbRef.current.onDoubleTap();
      } else {
        lastTapTimeRef.current = now;
        tapTimerRef.current = window.setTimeout(() => {
          tapTimerRef.current = null;
          if (lastTapTimeRef.current !== now) return;
          lastTapTimeRef.current = 0;
          cbRef.current.onTap();
        }, DOUBLE_TAP_MS);
      }
      modeRef.current = "idle";
      resetPosition();
      return true;
    };

    const settleHorizontalSwipe = (dx: number) => {
      const threshold = getWidth() * SWIPE_DX_RATIO;
      const projectedX = dx + projectMomentum(vxRef.current);
      let handledByCallback = false;
      if (Math.abs(projectedX) > threshold) {
        if (projectedX < 0 && !optRef.current.isLast) {
          handledByCallback = cbRef.current.onSwipeLeft() === true;
        } else if (projectedX > 0 && !optRef.current.isFirst) {
          handledByCallback = cbRef.current.onSwipeRight() === true;
        } else if (projectedX < 0) {
          cbRef.current.onBoundarySwipe?.("last");
        } else {
          cbRef.current.onBoundarySwipe?.("first");
        }
      }
      if (!handledByCallback) resetPosition();
    };

    const settleVerticalSwipe = (dy: number) => {
      const projectedY = dy + projectMomentum(vyRef.current);
      if (projectedY > DISMISS_DY) {
        cbRef.current.onDismiss();
        return;
      }
      if (projectedY < REVEAL_DY) {
        if (optRef.current.revealOpen) {
          cbRef.current.onRevealClose();
        } else {
          cbRef.current.onRevealOpen();
        }
      }
      resetPosition();
    };

    const settlePan = () => {
      const bounds = panBounds();
      optRef.current.dragX.set(
        clamp(optRef.current.dragX.get(), -bounds.x, bounds.x),
      );
      optRef.current.dragY.set(
        clamp(optRef.current.dragY.get(), -bounds.y, bounds.y),
      );
    };

    const settlePointerRelease = (dx: number, dy: number) => {
      if (modeRef.current === "swipe-h") {
        settleHorizontalSwipe(dx);
        return;
      }
      if (isVerticalMode(modeRef.current)) {
        settleVerticalSwipe(dy);
        return;
      }
      if (modeRef.current === "pan") settlePan();
    };

    const onPointerUp = (e: PointerEvent) => {
      emitPointerActivity(true);
      const hadPointer = pointersRef.current.delete(e.pointerId);
      if (!hadPointer) return;
      clearLongPress();
      flushPendingMotion();
      if (continuePinchAfterRelease()) return;

      const dx = e.clientX - startXRef.current;
      const dy = e.clientY - startYRef.current;
      const duration = performance.now() - startTimeRef.current;
      try {
        el.releasePointerCapture(e.pointerId);
      } catch {
        /* noop */
      }
      if (handleTapRelease(duration)) return;
      settlePointerRelease(dx, dy);
      modeRef.current = pointersRef.current.size > 0 ? modeRef.current : "idle";
    };

    const onPointerCancel = (e: PointerEvent) => {
      pointersRef.current.delete(e.pointerId);
      clearLongPress();
      clearTapTimer();
      lastTapTimeRef.current = 0;
      modeRef.current = "idle";
      resetPosition();
    };

    el.addEventListener("pointerdown", onPointerDown);
    el.addEventListener("pointermove", onPointerMove);
    el.addEventListener("pointerup", onPointerUp);
    el.addEventListener("pointercancel", onPointerCancel);

    return () => {
      clearLongPress();
      clearTapTimer();
      lastTapTimeRef.current = 0;
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      pendingMotionRef.current = {};
      pointers.clear();
      modeRef.current = "idle";
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointermove", onPointerMove);
      el.removeEventListener("pointerup", onPointerUp);
      el.removeEventListener("pointercancel", onPointerCancel);
    };
  }, [target, enabled]);
}
