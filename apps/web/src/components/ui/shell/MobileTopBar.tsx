"use client";

import { type ReactNode, useEffect, useRef, useState } from "react";

export interface MobileTopBarProps {
  left?: ReactNode;
  right?: ReactNode;
  /** 当页面滚动超过 10px 时才玻璃化。需要挂 sentinel：<div data-topbar-sentinel /> */
  glassOnScroll?: boolean;
  className?: string;
}

export function MobileTopBar({
  left,
  right,
  glassOnScroll = true,
  className = "",
}: MobileTopBarProps) {
  const [glass, setGlass] = useState(false);
  const ref = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!glassOnScroll) return;
    const el = document.querySelector<HTMLElement>("[data-topbar-sentinel]");
    if (!el) {
      // fallback: 监听 window scroll
      const onScroll = () => setGlass(window.scrollY > 8);
      window.addEventListener("scroll", onScroll, { passive: true });
      onScroll();
      return () => window.removeEventListener("scroll", onScroll);
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          setGlass(!e.isIntersecting);
        }
      },
      { threshold: 0 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [glassOnScroll]);

  return (
    <header
      ref={ref}
      className={[
        "sticky top-0 left-0 right-0 safe-x",
        "transition-[background-color,backdrop-filter,border-color] duration-200",
        glass
          ? "bg-[var(--bg-0)]/72 backdrop-blur-xl mobile-perf-surface border-b border-[var(--border-subtle)]"
          : "bg-transparent border-b border-transparent",
        className,
      ].join(" ")}
      style={{
        zIndex: "var(--z-header, 10)" as unknown as number,
        paddingTop: "env(safe-area-inset-top, 0px)",
      }}
    >
      <div className="relative flex items-center h-10 max-w-[640px] mx-auto px-3 gap-2.5">
        <div className="flex-1 min-w-0 flex items-center gap-2">{left}</div>
        <div className="flex items-center gap-1.5">{right}</div>
      </div>
    </header>
  );
}
