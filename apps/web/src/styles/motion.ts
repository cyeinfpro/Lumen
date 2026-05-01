"use client";

import { useReducedMotion } from "framer-motion";

export const lumenMotion = {
  easeOut: [0.22, 1, 0.36, 1] as const,
  easeInOut: [0.4, 0, 0.2, 1] as const,
  toastEnterMs: 200,
  toastExitMs: 180,
  compactSettleMs: 600,
  shimmerSeconds: 1.6,
  spring: {
    type: "spring",
    stiffness: 120,
    damping: 20,
  } as const,
};

export function usePrefersReducedMotion(): boolean {
  return Boolean(useReducedMotion());
}

export function motionDuration(ms: number, reduced: boolean): number {
  return reduced ? 0 : ms / 1000;
}

export function reducedMotionTransition(reduced: boolean, durationMs = 200) {
  return {
    duration: motionDuration(durationMs, reduced),
    ease: lumenMotion.easeOut,
  };
}
