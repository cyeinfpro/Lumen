"use client";

import { motion, useReducedMotion } from "framer-motion";
import { usePathname } from "next/navigation";
import { type ReactNode } from "react";

// 底部 Tab 路由仍保留 pathname key，但不做整页退出等待，避免露出黑底。
const ANIMATED_ROUTES = ["/", "/stream", "/me"];

function isAnimatedRoute(pathname: string): boolean {
  if (pathname === "/") return true;
  return ANIMATED_ROUTES.some((r) => r !== "/" && pathname.startsWith(r));
}

export function PageTransitions({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const reduce = useReducedMotion();
  const animated = isAnimatedRoute(pathname);

  if (!animated) {
    return <div className="flex-1 flex flex-col w-full min-h-0">{children}</div>;
  }

  return (
    <motion.div
      key={pathname}
      initial={false}
      animate={reduce ? { opacity: 1 } : { opacity: 1, y: 0, scale: 1 }}
      transition={{
        type: "spring",
        stiffness: 360,
        damping: 34,
        mass: 0.8,
      }}
      className="flex-1 flex flex-col w-full min-h-0"
    >
      {children}
    </motion.div>
  );
}
