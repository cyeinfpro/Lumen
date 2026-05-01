"use client";

import { Images } from "lucide-react";
import { motion } from "framer-motion";
import { useChatStore } from "@/store/useChatStore";
import { cn } from "@/lib/utils";

const OPTIONS = [1, 2, 4, 6, 8] as const;

export function ImageCountPicker() {
  const count = useChatStore((s) => s.composer.params.count ?? 1);
  const setImageCount = useChatStore((s) => s.setImageCount);

  return (
    <div
      role="radiogroup"
      aria-label="选择生成张数"
      className={cn(
        "inline-flex items-center gap-1 rounded-full border border-white/10 bg-white/5 p-0.5",
        "shadow-[0_1px_0_rgba(255,255,255,0.04)_inset]",
      )}
    >
      <span
        className="hidden sm:inline-flex items-center gap-1 px-2 text-[11px] font-medium text-neutral-400"
        aria-hidden
      >
        <Images className="h-3.5 w-3.5" />
        张数
      </span>
      {OPTIONS.map((option) => {
        const active = count === option;
        return (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={`生成 ${option} 张图片`}
            title={`生成 ${option} 张图片`}
            onClick={() => setImageCount(option)}
            className={cn(
              // MED #7：移动端 44×44 触控区，桌面端回到 28×28 紧凑外观
              "relative min-h-[44px] min-w-[44px] md:min-h-0 md:min-w-0 md:h-7 md:w-7",
              "inline-flex items-center justify-center rounded-full px-2 text-xs font-medium tabular-nums",
              "transition-colors duration-150 active:scale-[0.96]",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/60",
              active ? "text-black" : "text-neutral-400 hover:text-white",
            )}
          >
            {active && (
              <motion.span
                layoutId="image-count-pill"
                transition={{ type: "spring", damping: 28, stiffness: 380 }}
                className="absolute inset-0 -z-10 rounded-full bg-[var(--color-lumen-amber)] shadow-[0_0_14px_rgba(242,169,58,0.28)]"
              />
            )}
            {option}
          </button>
        );
      })}
    </div>
  );
}
