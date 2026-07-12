"use client";

import { usePathname } from "next/navigation";
import { type ReactNode } from "react";

import { getActiveNavKey } from "./navigation";

function isAnimatedRoute(pathname: string): boolean {
  return getActiveNavKey(pathname) !== null;
}

export function PageTransitions({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const animated = isAnimatedRoute(pathname);
  const navigationLevel = getActiveNavKey(pathname) ?? pathname;

  if (!animated) {
    return (
      <div className="flex-1 flex flex-col w-full min-h-0">
        {children}
      </div>
    );
  }

  return (
    <div
      key={navigationLevel}
      data-lumen-motion-page
      data-navigation-level={navigationLevel}
      className="flex-1 flex flex-col w-full min-h-0"
    >
      {children}
    </div>
  );
}
