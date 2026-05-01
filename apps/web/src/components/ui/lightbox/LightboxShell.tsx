"use client";

// LightboxShell —— 移动端 Lightbox 的外壳容器：
// - 全屏 fixed 覆盖 + z-index
// - role=dialog / aria-modal
// - 打开时给 <main> 加 inert，关闭时还原
// - 接管 Esc、body scroll lock
// - focus trap：内部 Tab 在可聚焦节点之间循环，初始 focus 交给调用方指定的 ref

import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
} from "react";

export interface LightboxShellProps {
  onClose: () => void;
  ariaLabel?: string;
  /** 初始 focus target（通常是顶栏的 × 关闭按钮） */
  initialFocusRef?: React.RefObject<HTMLElement | null>;
  children: ReactNode;
  className?: string;
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export function LightboxShell({
  onClose,
  ariaLabel = "图片查看器",
  initialFocusRef,
  children,
  className,
}: LightboxShellProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  // body scroll lock
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  // <main> inert（React 19 支持原生 inert 属性；这里用 DOM 赋值避免 SSR 警告）
  useEffect(() => {
    const mainEls = Array.from(document.querySelectorAll("main"));
    const restore: Array<() => void> = [];
    for (const el of mainEls) {
      const prev = el.getAttribute("inert");
      const prevAria = el.getAttribute("aria-hidden");
      el.setAttribute("inert", "");
      el.setAttribute("aria-hidden", "true");
      restore.push(() => {
        if (prev === null) el.removeAttribute("inert");
        else el.setAttribute("inert", prev);
        if (prevAria === null) el.removeAttribute("aria-hidden");
        else el.setAttribute("aria-hidden", prevAria);
      });
    }
    return () => {
      restore.forEach((f) => f());
    };
  }, []);

  // 记录打开前的焦点，关闭时还原
  useEffect(() => {
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    const raf = requestAnimationFrame(() => {
      initialFocusRef?.current?.focus?.({ preventScroll: true });
    });
    return () => {
      cancelAnimationFrame(raf);
      const prev = previouslyFocusedRef.current;
      if (prev && typeof prev.focus === "function") {
        try {
          prev.focus({ preventScroll: true });
        } catch {
          /* noop */
        }
      }
    };
  }, [initialFocusRef]);

  // Esc 关闭 + focus trap
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === "Tab") {
        const root = rootRef.current;
        if (!root) return;
        const focusables = Array.from(
          root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
        ).filter((el) => !el.hasAttribute("data-focus-skip"));
        if (focusables.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
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
      }
    },
    [onClose],
  );

  return (
    <div
      ref={rootRef}
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel}
      onKeyDown={handleKeyDown}
      className={
        className ??
        "fixed inset-0 h-[100dvh] w-screen outline-none z-[var(--z-lightbox,80)] bg-[var(--bg-0)]"
      }
    >
      {children}
    </div>
  );
}
