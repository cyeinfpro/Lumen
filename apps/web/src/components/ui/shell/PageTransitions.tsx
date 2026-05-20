"use client";

import { motion, useReducedMotion } from "framer-motion";
import { usePathname } from "next/navigation";
import { type ReactNode } from "react";

import { getActiveNavKey } from "./navigation";

function isAnimatedRoute(pathname: string): boolean {
  return getActiveNavKey(pathname) !== null;
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
