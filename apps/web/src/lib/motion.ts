/**
 * Lumen 移动端 motion 单一数字源。
 * 所有 framer-motion 动画参数从此处引用 —— 严禁组件内硬编码
 * `damping:` / `stiffness:` / `cubic-bezier(` / 任意 ms 数字。
 *
 * 对应 CSS token：见 apps/web/src/app/globals.css 的 --dur-* / --ease-*。
 */

export const SPRING = {
  /** Tab / Segmented / 短距位移 */
  snap:    { type: "spring" as const, stiffness: 460, damping: 38, mass: 0.7 },
  /** Bottom sheet / tray / 中距面板 */
  sheet:   { type: "spring" as const, stiffness: 360, damping: 34, mass: 0.85 },
  /** 侧栏抽屉：临界阻尼，避免功能型界面出现多余回弹 */
  drawer:  { type: "spring" as const, stiffness: 420, damping: 40, mass: 0.9 },
  /** 手势拖拽回弹 / Lightbox dismiss */
  gesture: { type: "spring" as const, stiffness: 300, damping: 28, mass: 1.0 },
  /** Toast 堆叠：短距、可中断，保留轻微物理感 */
  toast:   { type: "spring" as const, stiffness: 420, damping: 34, mass: 0.75 },
  /** 长距 hero、低频入场 */
  soft:    { type: "spring" as const, stiffness: 220, damping: 26, mass: 1.1 },
} as const;

export const DURATION = {
  instant: 0.09,  // --dur-instant
  quick:   0.14,  // --dur-quick
  normal:  0.18,  // --dur-normal
  page:    0.18,  // --dur-page
  panel:   0.24,
  sheet:   0.28,
  slow:    0.32,  // --dur-slow
} as const;

/** cubic-bezier 形式的 easing，与 --ease-* CSS var 数值一致 */
export const EASE = {
  shutter: [0.16, 1, 0.3, 1] as const,
  develop: [0.22, 1, 0.36, 1] as const,
  curtain: [0.80, 0, 0.20, 1] as const,
} as const;

export const PRESS_SCALE = {
  tight: 0.96,
  soft: 0.98,
  none: 1,
} as const;

export const GESTURE = {
  intentSlop: 8,
  directionBias: 1.15,
  decelerationRate: 0.99,
  snapDistance: 48,
  snapVelocity: 350,
  dismissVelocity: 700,
} as const;

/**
 * Apple-style exponential momentum projection.
 * Returns the additional distance (px) implied by a release velocity (px/s).
 */
export function projectMomentum(
  velocity: number,
  decelerationRate = GESTURE.decelerationRate,
): number {
  if (!Number.isFinite(velocity)) return 0;
  const requestedRate = Number.isFinite(decelerationRate)
    ? decelerationRate
    : GESTURE.decelerationRate;
  const rate = Math.min(0.999, Math.max(0.9, requestedRate));
  return (velocity / 1_000) * (rate / (1 - rate));
}

/**
 * Progressive boundary resistance. The output stays continuous while growing
 * more slowly than the user's pointer once it moves beyond a natural edge.
 */
export function rubberBandDistance(
  distance: number,
  dimension: number,
  constant = 0.55,
): number {
  if (!Number.isFinite(distance) || distance === 0) return 0;
  const size = Number.isFinite(dimension)
    ? Math.max(1, Math.abs(dimension))
    : 1;
  const strength = Number.isFinite(constant)
    ? Math.max(0.01, constant)
    : 0.55;
  const magnitude =
    (Math.abs(distance) * size * strength) /
    (size + strength * Math.abs(distance));
  return Math.sign(distance) * magnitude;
}

export function resolveDrawerMotion(
  reduceMotion: boolean | null,
  scrimDuration: number = DURATION.normal,
) {
  if (reduceMotion) {
    return {
      scrimTransition: { duration: 0, ease: EASE.develop },
      panelInitial: { opacity: 0 },
      panelAnimate: { opacity: 1 },
      panelExit: { opacity: 0 },
      panelTransition: { duration: 0 },
    } as const;
  }
  return {
    scrimTransition: { duration: scrimDuration, ease: EASE.develop },
    panelInitial: { x: "-100%", opacity: 1 },
    panelAnimate: { x: 0, opacity: 1 },
    panelExit: { x: "-100%", opacity: 1 },
    panelTransition: SPRING.drawer,
  } as const;
}

export type SpringName = keyof typeof SPRING;
export type PressScaleName = keyof typeof PRESS_SCALE;
