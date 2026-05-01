/**
 * Lumen 移动端 motion 单一数字源。
 * 所有 framer-motion 动画参数从此处引用 —— 严禁组件内硬编码
 * `damping:` / `stiffness:` / `cubic-bezier(` / 任意 ms 数字。
 *
 * 对应 CSS token：见 apps/web/src/app/globals.css 的 --dur-* / --ease-*。
 */

export const SPRING = {
  /** Tab / Segmented / 短距位移 */
  snap:    { type: "spring" as const, stiffness: 420, damping: 32, mass: 0.9 },
  /** Bottom sheet / tray / 中距面板 */
  sheet:   { type: "spring" as const, stiffness: 380, damping: 32, mass: 1.0 },
  /** 手势拖拽回弹 / Lightbox dismiss */
  gesture: { type: "spring" as const, stiffness: 300, damping: 28, mass: 1.0 },
  /** 长距 hero、低频入场 */
  soft:    { type: "spring" as const, stiffness: 220, damping: 26, mass: 1.1 },
} as const;

export const DURATION = {
  instant: 0.08,  // --dur-instant
  quick:   0.14,  // --dur-quick
  normal:  0.22,  // --dur-normal
  page:    0.28,  // --dur-page
  slow:    0.42,  // --dur-slow
} as const;

/** cubic-bezier 形式的 easing，与 --ease-* CSS var 数值一致 */
export const EASE = {
  shutter: [0.16, 1, 0.3, 1] as const,
  develop: [0.22, 1, 0.36, 1] as const,
  curtain: [0.80, 0, 0.20, 1] as const,
} as const;

export const OPACITY = {
  hover: 0.92,
  press: 0.88,
  disabled: 0.40,
  hint: 0.60,
} as const;

export const PRESS_SCALE = {
  tight: 0.96,
  soft: 0.98,
  none: 1,
} as const;

export type SpringName = keyof typeof SPRING;
export type PressScaleName = keyof typeof PRESS_SCALE;
