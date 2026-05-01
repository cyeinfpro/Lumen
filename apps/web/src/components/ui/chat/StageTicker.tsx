"use client";

import { AnimatePresence, motion } from "framer-motion";
import type { Generation } from "@/lib/types";

const STAGE_COPY: Record<Generation["stage"], string> = {
  queued: "排队中",
  understanding: "理解提示词",
  rendering: "渲染中",
  finalizing: "收尾",
};

interface StageTickerProps {
  gen: Generation;
  className?: string;
}

export function StageTicker({ gen, className }: StageTickerProps) {
  const running = gen.status === "running";
  const queued = gen.status === "queued";

  let copy: string;
  let animKey: string;
  if (queued) {
    copy = "排队中";
    animKey = "queued";
  } else if (gen.attempt > 1 && gen.status === "running") {
    copy = `${STAGE_COPY[gen.stage] ?? "处理中"} (第${gen.attempt}次)`;
    animKey = `${gen.stage}-${gen.attempt}`;
  } else {
    copy = STAGE_COPY[gen.stage] ?? "处理中";
    animKey = gen.stage;
  }

  return (
    <div
      className={[
        "relative flex shrink-0 items-center justify-end min-w-[96px] sm:min-w-[128px] h-5",
        className ?? "",
      ].join(" ")}
      aria-live="polite"
    >
      <AnimatePresence mode="wait">
        <motion.span
          key={animKey}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
          className={[
            "inline-flex items-center gap-1.5 text-xs",
            "text-[var(--color-lumen-amber)]",
          ].join(" ")}
        >
          <span className="relative flex items-center justify-center">
            <span className="absolute w-2 h-2 rounded-full bg-current opacity-40 animate-ping" />
            <span className="relative w-1.5 h-1.5 rounded-full bg-current" />
          </span>
          <span>{copy}</span>
          {running && typeof gen.elapsed === "number" && gen.elapsed > 0 && (
            <span className="text-neutral-500 font-mono tabular-nums">
              · {Math.floor(gen.elapsed / 1000)}s
            </span>
          )}
        </motion.span>
      </AnimatePresence>
    </div>
  );
}
