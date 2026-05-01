"use client";

import { useCallback, useEffect, useState } from "react";

export type HapticKind =
  | "light"      // 主按压 / tab 切换 / toggle 开
  | "medium"     // 成功 / 确认 / 切换到新模式
  | "strong"     // 警告 / destructive 前置
  | "success"    // 操作成功（send 后）
  | "warning"    // 误操作提示
  | "error";     // 明确失败

const HAPTIC_KEY = "lumen.haptic.enabled";
const PATTERNS: Record<HapticKind, number | number[]> = {
  light: 8,
  medium: 14,
  strong: 22,
  success: [10, 40, 10],
  warning: [16, 60, 16],
  error: [30, 40, 30],
};

function readEnabled(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const raw = window.localStorage.getItem(HAPTIC_KEY);
    return raw === null ? true : raw === "1";
  } catch {
    return true;
  }
}

export function useHaptic() {
  const [enabled, setEnabledState] = useState<boolean>(true);

  useEffect(() => {
    const hydrateTimer = window.setTimeout(() => setEnabledState(readEnabled()), 0);
    const onStorage = (e: StorageEvent) => {
      if (e.key === HAPTIC_KEY) setEnabledState(readEnabled());
    };
    window.addEventListener("storage", onStorage);
    return () => {
      window.clearTimeout(hydrateTimer);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const haptic = useCallback(
    (kind: HapticKind = "light") => {
      if (!enabled) return;
      if (typeof navigator === "undefined") return;
      const vib = (navigator as Navigator).vibrate?.bind(navigator);
      if (!vib) return;
      vib(PATTERNS[kind]);
    },
    [enabled],
  );

  const setEnabled = useCallback((v: boolean) => {
    setEnabledState(v);
    try {
      window.localStorage.setItem(HAPTIC_KEY, v ? "1" : "0");
    } catch {
      /* no-op (private mode) */
    }
  }, []);

  return { haptic, enabled, setEnabled };
}
