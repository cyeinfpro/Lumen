"use client";

import { motion } from "framer-motion";
import {
  type KeyboardEvent,
  type ReactNode,
  useId,
  useRef,
} from "react";
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
  density?: "default" | "compact";
}

// SPRING.snap 已在 @/lib/motion 统一定义，此处直接引用

export function SegmentedControl<V extends string = string>({
  value,
  onChange,
  items,
  ariaLabel,
  className = "",
  density = "default",
}: SegmentedControlProps<V>) {
  // 每个实例独立 layoutId，避免多个 SegmentedControl 同屏时 indicator 互相漫游
  const uid = useId();
  const layoutId = `segmented-indicator-${uid}`;
  const compact = density === "compact";
  const itemRefs = useRef<Array<HTMLElement | null>>([]);
  const selectedIndex = items.findIndex((item) => item.value === value);
  const tabStopIndex = selectedIndex >= 0 ? selectedIndex : 0;

  const selectAndFocus = (index: number) => {
    const item = items[index];
    if (!item) return;
    onChange(item.value);
    itemRefs.current[index]?.focus({ preventScroll: true });
  };

  const handleKeyDown = (
    event: KeyboardEvent<HTMLElement>,
    index: number,
  ) => {
    if (items.length === 0) return;

    let nextIndex: number | null = null;
    switch (event.key) {
      case "ArrowLeft":
      case "ArrowUp":
        nextIndex = (index - 1 + items.length) % items.length;
        break;
      case "ArrowRight":
      case "ArrowDown":
        nextIndex = (index + 1) % items.length;
        break;
      case "Home":
        nextIndex = 0;
        break;
      case "End":
        nextIndex = items.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    selectAndFocus(nextIndex);
  };

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      aria-orientation="horizontal"
      className={[
        compact
          ? "relative flex min-h-8 items-center rounded-[var(--radius-control)] p-px"
          : "relative flex min-h-11 items-center rounded-[var(--radius-card)] p-px md:min-h-10",
        "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
        className,
      ].join(" ")}
    >
      {items.map((item, index) => {
        const active = item.value === value;
        return (
          <Pressable
            key={item.value}
            ref={(node) => {
              itemRefs.current[index] = node;
            }}
            role="tab"
            aria-selected={active}
            tabIndex={index === tabStopIndex ? 0 : -1}
            size="default"
            minHit={false}
            pressScale="soft"
            haptic="light"
            onPress={() => onChange(item.value)}
            onKeyDown={(event) => handleKeyDown(event, index)}
            className={[
              compact
                ? "relative z-[1] min-h-7 flex-1 min-w-0 rounded-[var(--radius-sm)] px-2 gap-1"
                : "relative z-[1] min-h-10 flex-1 min-w-0 rounded-[var(--radius-md)] px-2 sm:px-3 gap-1.5 md:min-h-9",
              compact
                ? "text-[12px] font-medium transition-colors"
                : "text-[13px] font-medium transition-colors",
              "focus-visible:z-[2] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
              active ? "text-[var(--fg-0)]" : "text-[var(--fg-2)]",
            ].join(" ")}
          >
            {active && (
              <motion.span
                layoutId={layoutId}
                className={[
                  "absolute inset-0 border border-[var(--border)] shadow-[var(--shadow-1)]",
                  compact
                    ? "rounded-[var(--radius-sm)] bg-[var(--bg-1)]"
                    : "rounded-[var(--radius-md)] bg-[var(--bg-0)]",
                ].join(" ")}
                transition={SPRING.snap}
                aria-hidden
              />
            )}
            <span className="relative z-[1] flex w-full min-w-0 items-center justify-center gap-1.5 whitespace-nowrap">
              {item.label}
              {item.badge != null && (
                <span className="shrink-0 text-[10px] tracking-wider text-[var(--fg-2)]">
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
