"use client";

import { useEffect, useState } from "react";
import { motion, useMotionValue, useMotionValueEvent, useSpring } from "framer-motion";
import { cn } from "@/lib/utils";
import { usePrefersReducedMotion } from "@/styles/motion";

interface RollingTokenCounterProps {
  value: number;
  active?: boolean;
  className?: string;
  format?: (value: number) => string;
}

function defaultFormat(value: number): string {
  const n = Math.max(0, Math.round(value));
  return n.toLocaleString();
}

export function RollingTokenCounter({
  value,
  active = false,
  className,
  format = defaultFormat,
}: RollingTokenCounterProps) {
  const reducedMotion = usePrefersReducedMotion();
  const target = Math.max(0, Math.round(value));
  const motionValue = useMotionValue(target);
  const spring = useSpring(motionValue, { stiffness: 120, damping: 20 });
  const [displayText, setDisplayText] = useState(() => format(target));

  useMotionValueEvent(spring, "change", (latest) => {
    setDisplayText(format(latest));
  });

  useEffect(() => {
    motionValue.set(target);
  }, [motionValue, target]);

  if (reducedMotion) {
    return (
      <span className={cn("tabular-nums", className)} style={{ fontFamily: "var(--font-mono)" }}>
        {format(target)}
      </span>
    );
  }

  return (
      <motion.span
        aria-live={active ? "polite" : undefined}
        className={cn("inline-block tabular-nums", className)}
        style={{ fontFamily: "var(--font-mono)" }}
      >
      {displayText}
    </motion.span>
  );
}
