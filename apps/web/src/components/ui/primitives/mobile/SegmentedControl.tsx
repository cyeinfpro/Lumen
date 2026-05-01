"use client";

import { motion } from "framer-motion";
import { type ReactNode, useId } from "react";
import { SPRING } from "@/lib/motion";
import { Pressable } from "./Pressable";

export interface SegmentItem<V extends string = string> {
  value: V;
  label: ReactNode;
  badge?: ReactNode;
}

export interface SegmentedControlProps<V extends string = string> {
  value: V;
  onChange: (v: V) => void;
  items: SegmentItem<V>[];
  ariaLabel?: string;
  className?: string;
}

// SPRING.snap 已在 @/lib/motion 统一定义，此处直接引用

export function SegmentedControl<V extends string = string>({
  value,
  onChange,
  items,
  ariaLabel,
  className = "",
}: SegmentedControlProps<V>) {
  // 每个实例独立 layoutId，避免多个 SegmentedControl 同屏时 indicator 互相漫游
  const uid = useId();
  const layoutId = `segmented-indicator-${uid}`;
  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      className={[
        "relative flex items-center h-10 p-px rounded-full",
        "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
        className,
      ].join(" ")}
    >
      {items.map((item) => {
        const active = item.value === value;
        return (
          <Pressable
            key={item.value}
            role="tab"
            aria-selected={active}
            size="default"
            minHit={false}
            pressScale="soft"
            haptic="light"
            onPress={() => onChange(item.value)}
            className={[
              "relative z-[1] flex-1 h-9 px-3 rounded-full gap-1.5",
              "text-[13px] font-medium transition-colors",
              active ? "text-[var(--fg-0)]" : "text-[var(--fg-2)]",
            ].join(" ")}
          >
            {active && (
              <motion.span
                layoutId={layoutId}
                className="absolute inset-0 rounded-full bg-[var(--bg-0)] shadow-[var(--shadow-1)] border border-[var(--border)]"
                transition={SPRING.snap}
                aria-hidden
              />
            )}
            <span className="relative z-[1] flex w-full items-center justify-center gap-1.5">
              {item.label}
              {item.badge != null && (
                <span className="text-[10px] tracking-wider text-[var(--fg-2)]">
                  {item.badge}
                </span>
              )}
            </span>
          </Pressable>
        );
      })}
    </div>
  );
}
