"use client";

import { useEffect, useState } from "react";

export interface KeyboardInsetState {
  /** 键盘遮挡的像素高度；不支持 visualViewport 或键盘未弹起时为 0 */
  inset: number;
  /** inset > 0 */
  isKeyboardOpen: boolean;
  /** Visual Viewport 顶部相对 Layout Viewport 的偏移 */
  viewportTop: number;
  /** 用户当前真正可见的视口高度 */
  viewportHeight: number;
  /** Visual Viewport 底边在 Layout Viewport 坐标系中的位置 */
  viewportBottom: number;
}

const INITIAL: KeyboardInsetState = {
  inset: 0,
  isKeyboardOpen: false,
  viewportTop: 0,
  viewportHeight: 0,
  viewportBottom: 0,
};
const KEYBOARD_THRESHOLD = 80;

function activeElementCanOpenKeyboard(): boolean {
  if (typeof document === "undefined") return false;
  const el = document.activeElement;
  if (!el) return false;
  if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
    return !el.readOnly && !el.disabled;
  }
  return el instanceof HTMLElement && el.isContentEditable;
}

/**
 * 监听 visualViewport，返回键盘遮挡高度。
 * 兼容：
 *   - iOS Safari 13+（visualViewport 可用）
 *   - Android Chrome 61+（同上）
 *   - 老 WebView → 返回 0，退化为无键盘感知
 */
export function useKeyboardInset(): KeyboardInsetState {
  const [state, setState] = useState<KeyboardInsetState>(INITIAL);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const vv = window.visualViewport;
    let frame = 0;
    // ~60fps throttle：高频 resize/scroll 时跳过过密的 RAF 调度，
    // 减少 effect 中重复 setState 与移动端电池消耗。
    let lastUpdate = 0;
    const MIN_INTERVAL_MS = 16;

    const update = () => {
      const now =
        typeof performance !== "undefined" ? performance.now() : Date.now();
      if (frame === 0 && now - lastUpdate < MIN_INTERVAL_MS) return;
      if (frame) window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        lastUpdate =
          typeof performance !== "undefined" ? performance.now() : Date.now();
        const viewportTop = vv?.offsetTop ?? 0;
        const viewportHeight = vv?.height ?? window.innerHeight;
        const viewportBottom = viewportTop + viewportHeight;
        const rawInset = window.innerHeight - viewportBottom;
        const maxInset = Math.floor(window.innerHeight * 0.7);
        const rounded = Math.round(Math.max(0, Math.min(rawInset, maxInset)));
        const inset =
          activeElementCanOpenKeyboard() && rounded >= KEYBOARD_THRESHOLD
            ? rounded
            : 0;
        setState((prev) => {
          const next = {
            inset,
            isKeyboardOpen: inset >= KEYBOARD_THRESHOLD,
            viewportTop: Math.round(viewportTop),
            viewportHeight: Math.round(viewportHeight),
            viewportBottom: Math.round(viewportBottom),
          };
          return prev.inset === next.inset &&
            prev.isKeyboardOpen === next.isKeyboardOpen &&
            prev.viewportTop === next.viewportTop &&
            prev.viewportHeight === next.viewportHeight &&
            prev.viewportBottom === next.viewportBottom
            ? prev
            : next;
        });
      });
    };

    vv?.addEventListener("resize", update, { passive: true });
    vv?.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update, { passive: true });
    window.addEventListener("focusin", update);
    window.addEventListener("focusout", update);
    update();

    return () => {
      vv?.removeEventListener("resize", update);
      vv?.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
      window.removeEventListener("focusin", update);
      window.removeEventListener("focusout", update);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, []);

  return state;
}
