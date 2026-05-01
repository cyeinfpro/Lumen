"use client";

import { motion } from "framer-motion";
import { useChatStore } from "@/store/useChatStore";
import { cn } from "@/lib/utils";
import type { Quality, RenderQualityChoice } from "@/lib/types";

const OPTIONS: { value: Quality; label: string }[] = [
  { value: "1k", label: "1K" },
  { value: "2k", label: "2K" },
  { value: "4k", label: "4K" },
];

const RENDER_OPTIONS: { value: RenderQualityChoice; label: string }[] = [
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

export function QualityPicker() {
  const quality = useChatStore((s) => s.composer.params.quality ?? "2k");
  const setQuality = useChatStore((s) => s.setQuality);

  return (
    <div
      className={cn(
        "inline-flex items-center h-7 rounded-full border",
        "bg-white/5 border-white/10",
      )}
      role="radiogroup"
      aria-label="尺寸选择"
    >
      {OPTIONS.map(({ value, label }) => {
        const active = quality === value;
        return (
          <motion.button
            key={value}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => setQuality(value)}
            whileHover={{ scale: 1.03 }}
            whileTap={{ scale: 0.94 }}
            transition={{ type: "spring", stiffness: 400, damping: 25 }}
            className={cn(
              "relative px-2 h-full text-xs font-medium rounded-full",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
              "transition-colors duration-150",
              active
                ? "bg-[var(--color-lumen-amber)]/15 border-[var(--color-lumen-amber)]/40 text-[var(--color-lumen-amber)]"
                : "text-neutral-400 hover:text-white",
            )}
          >
            {label}
          </motion.button>
        );
      })}
    </div>
  );
}

export function RenderQualityPicker() {
  const renderQuality = useChatStore((s) => {
    const q = s.composer.params.render_quality;
    return q === "low" || q === "medium" || q === "high" ? q : "medium";
  });
  const setRenderQuality = useChatStore((s) => s.setRenderQuality);

  return (
    <div
      className={cn(
        "inline-flex items-center h-7 rounded-full border",
        "bg-white/5 border-white/10",
      )}
      role="radiogroup"
      aria-label="渲染质量"
    >
      {RENDER_OPTIONS.map(({ value, label }) => {
        const active = renderQuality === value;
        return (
          <motion.button
            key={value}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => setRenderQuality(value)}
            whileHover={{ scale: 1.03 }}
            whileTap={{ scale: 0.94 }}
            transition={{ type: "spring", stiffness: 400, damping: 25 }}
            className={cn(
              "relative px-2 h-full text-xs font-medium rounded-full",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
              "transition-colors duration-150",
              active
                ? "bg-[var(--color-lumen-amber)]/15 border-[var(--color-lumen-amber)]/40 text-[var(--color-lumen-amber)]"
                : "text-neutral-400 hover:text-white",
            )}
          >
            {label}
          </motion.button>
        );
      })}
    </div>
  );
}
