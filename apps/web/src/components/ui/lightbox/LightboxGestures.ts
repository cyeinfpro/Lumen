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
const SWIPE_VX = 600;
const DISMISS_DY = 120;
const DISMISS_VY = 500;
const REVEAL_DY = -80;
const LONG_PRESS_MS = 600;
const DOUBLE_TAP_MS = 280;
const TAP_SLOP = 8;
const RUBBER = 20; // 首末张橡皮筋 20px
const MAX_SCALE = 4;
const POINTER_ACTIVITY_MOVE_THROTTLE_MS = 180;

function rubberBand(dx: number): number {
  // 20px 内线性，之后指数衰减
  const sign = dx < 0 ? -1 : 1;
  const v = Math.abs(dx);
  if (v <= RUBBER) return dx;
  return sign * (RUBBER + (v - RUBBER) * 0.2);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function rubberClamp(value: number, min: number, max: number): number {
  if (value < min) return min - rubberBand(min - value);
  if (value > max) return max + rubberBand(value - max);
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
    if (!enabled) {
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

    const onPointerMove = (e: PointerEvent) => {
      const p = pointersRef.current.get(e.pointerId);
      if (!p) return;
      p.x = e.clientX;
      p.y = e.clientY;
      emitPointerActivity();

      const now = performance.now();
      if (modeRef.current === "pinch" && pointersRef.current.size === 2) {
        const pts = Array.from(pointersRef.current.values());
        const d = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
        if (pinchStartDistRef.current > 0) {
          const next = Math.min(
            MAX_SCALE,
            Math.max(1, pinchStartScaleRef.current * (d / pinchStartDistRef.current)),
          );
          scheduleMotion({ scale: next });
          if (next <= 1.01) {
            scheduleMotion({ dragX: 0, dragY: 0 });
          }
        }
        movedRef.current = true;
        clearLongPress();
        if (e.cancelable) e.preventDefault();
        return;
      }

      const dx = e.clientX - startXRef.current;
      const dy = e.clientY - startYRef.current;
      const absDx = Math.abs(dx);
      const absDy = Math.abs(dy);

      // 超出 tap slop 就标记 moved
      if (!movedRef.current && (absDx > TAP_SLOP || absDy > TAP_SLOP)) {
        movedRef.current = true;
        clearLongPress();
      }

      // 估计速度
      const dt = Math.max(1, now - lastMoveTimeRef.current);
      vxRef.current = (e.clientX - lastXRef.current) / dt * 1000;
      vyRef.current = (e.clientY - lastYRef.current) / dt * 1000;
      lastXRef.current = e.clientX;
      lastYRef.current = e.clientY;
      lastMoveTimeRef.current = now;

      if (modeRef.current === "pan") {
        // pinch 放大后平移（由外部 motion x/y 直接驱动）
        const bounds = panBounds();
        scheduleMotion({
          dragX: rubberClamp(panStartXRef.current + dx, -bounds.x, bounds.x),
          dragY: rubberClamp(panStartYRef.current + dy, -bounds.y, bounds.y),
        });
        if (e.cancelable) e.preventDefault();
        return;
      }

      // 首次决定方向
      if (modeRef.current === "idle" && movedRef.current) {
        if (absDx > absDy) {
          modeRef.current = "swipe-h";
        } else if (dy > 0) {
          modeRef.current = "dismiss";
        } else {
          modeRef.current = "reveal";
        }
      }

      if (modeRef.current === "swipe-h") {
        // 首末张橡皮筋
        let outDx = dx;
        if (optRef.current.isFirst && dx > 0) outDx = rubberBand(dx);
        else if (optRef.current.isLast && dx < 0) outDx = -rubberBand(-dx);
        scheduleMotion({ dragX: outDx });
        if (e.cancelable) e.preventDefault();
      } else if (modeRef.current === "dismiss") {
        const fade = Math.max(0, 1 - dy / 400);
        scheduleMotion({ dragY: dy, haloOpacity: fade });
        if (e.cancelable) e.preventDefault();
      } else if (modeRef.current === "reveal") {
        // 上拉只记录速度 + 方向；阈值达成后在 pointerup 里触发
        scheduleMotion({ dragY: Math.max(-60, dy) });
        if (e.cancelable) e.preventDefault();
      }
    };

    const onPointerUp = (e: PointerEvent) => {
      emitPointerActivity(true);
      const hadPointer = pointersRef.current.delete(e.pointerId);
      if (!hadPointer) return;
      clearLongPress();
      flushPendingMotion();

      // 从 pinch 回落到单指：继续剩余手指为 pan
      if (modeRef.current === "pinch") {
        if (pointersRef.current.size === 1) {
          const [[, pt]] = pointersRef.current.entries();
          startXRef.current = pt.x;
          startYRef.current = pt.y;
          modeRef.current = optRef.current.scale.get() > 1.01 ? "pan" : "idle";
          panStartXRef.current = optRef.current.dragX.get();
          panStartYRef.current = optRef.current.dragY.get();
        } else if (pointersRef.current.size === 0) {
          modeRef.current = "idle";
          // pinch 结束后接近 1x 时回正，释放平移引用。
          if (optRef.current.scale.get() <= 1.01) {
            optRef.current.scale.set(1);
            resetPosition();
          }
        }
        return;
      }

      const dx = e.clientX - startXRef.current;
      const dy = e.clientY - startYRef.current;
      const absDx = Math.abs(dx);
      const absDy = Math.abs(dy);
      const dur = performance.now() - startTimeRef.current;

      try {
        el.releasePointerCapture(e.pointerId);
      } catch {
        /* noop */
      }

      // 单击 / 双击判断
      if (!movedRef.current && dur < 300) {
        const now = performance.now();
        const gap = now - lastTapTimeRef.current;
        if (gap < DOUBLE_TAP_MS) {
          lastTapTimeRef.current = 0;
          cbRef.current.onDoubleTap();
        } else {
          lastTapTimeRef.current = now;
          // 推迟单击判定：若 280ms 内未再次 tap，执行 onTap
          window.setTimeout(() => {
            if (lastTapTimeRef.current === now) {
              lastTapTimeRef.current = 0;
              cbRef.current.onTap();
            }
          }, DOUBLE_TAP_MS);
        }
        modeRef.current = "idle";
        resetPosition();
        return;
      }

      if (modeRef.current === "swipe-h") {
        const threshold = getWidth() * SWIPE_DX_RATIO;
        const fastEnough = Math.abs(vxRef.current) > SWIPE_VX;
        let handledByCallback = false;
        if (absDx > threshold || fastEnough) {
          if (dx < 0 && !optRef.current.isLast) {
            handledByCallback = cbRef.current.onSwipeLeft() === true;
          } else if (dx > 0 && !optRef.current.isFirst) {
            handledByCallback = cbRef.current.onSwipeRight() === true;
          } else if (dx < 0 && optRef.current.isLast) {
            cbRef.current.onBoundarySwipe?.("last");
          } else if (dx > 0 && optRef.current.isFirst) {
            cbRef.current.onBoundarySwipe?.("first");
          }
        }
        if (!handledByCallback) resetPosition();
      } else if (modeRef.current === "dismiss") {
        if (dy > DISMISS_DY || vyRef.current > DISMISS_VY) {
          cbRef.current.onDismiss();
        } else {
          resetPosition();
        }
      } else if (modeRef.current === "reveal") {
        if (dy < REVEAL_DY || vyRef.current < -SWIPE_VX) {
          if (optRef.current.revealOpen) {
            cbRef.current.onRevealClose();
          } else {
            cbRef.current.onRevealOpen();
          }
        }
        resetPosition();
      } else if (modeRef.current === "pan") {
        const bounds = panBounds();
        optRef.current.dragX.set(
          clamp(optRef.current.dragX.get(), -bounds.x, bounds.x),
        );
        optRef.current.dragY.set(
          clamp(optRef.current.dragY.get(), -bounds.y, bounds.y),
        );
        void absDy;
      }

      modeRef.current = pointersRef.current.size > 0 ? modeRef.current : "idle";
    };

    const onPointerCancel = (e: PointerEvent) => {
      pointersRef.current.delete(e.pointerId);
      clearLongPress();
      modeRef.current = "idle";
      resetPosition();
    };

    el.addEventListener("pointerdown", onPointerDown);
    el.addEventListener("pointermove", onPointerMove);
    el.addEventListener("pointerup", onPointerUp);
    el.addEventListener("pointercancel", onPointerCancel);

    return () => {
      clearLongPress();
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
