"use client";

// Spec §3.5：移动端横屏且 max-height: 480px 时给一条温和提示，允许用户关闭并 localStorage 记住。
// 不强制锁屏；仅视觉。

import { X } from "lucide-react";
import { useEffect, useState } from "react";

const KEY = "lumen.landscape-banner.dismissed";

export function LandscapeBanner() {
  const [show, setShow] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      if (window.localStorage.getItem(KEY) === "1") return;
    } catch {
      /* private mode */
    }
    const mql = window.matchMedia("(orientation: landscape) and (max-height: 480px)");
    const update = () => setShow(mql.matches);
    update();
    mql.addEventListener("change", update);
    return () => mql.removeEventListener("change", update);
  }, []);

  if (!show) return null;

  return (
    <div
      role="status"
      className="sticky top-0 left-0 right-0 flex items-center gap-2 px-3 py-1.5 bg-[var(--bg-1)]/92 border-b border-[var(--border-subtle)] text-xs text-[var(--fg-1)] backdrop-blur-xl safe-x"
      style={{ zIndex: "var(--z-header, 10)" as unknown as number }}
    >
      <span className="flex-1 truncate">竖屏体验更佳</span>
      <button
        type="button"
        aria-label="关闭提示"
        onClick={() => {
          try {
            window.localStorage.setItem(KEY, "1");
          } catch {
            /* no-op */
          }
          setShow(false);
        }}
        className="inline-flex items-center justify-center w-8 h-8 -mr-1 rounded-full text-[var(--fg-2)] active:bg-[var(--bg-2)]"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  );
}
