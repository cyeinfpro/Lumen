"use client";

import type { CSSProperties } from "react";
import type { AspectRatio } from "@/lib/types";
import { cn } from "@/lib/utils";

interface AspectRatioOption {
  value: AspectRatio;
  preview: {
    width: number;
    height: number;
  };
}

interface AspectRatioGroup {
  key: "landscape" | "portrait";
  label: string;
  options: AspectRatioOption[];
}

const ASPECT_RATIO_GROUPS: AspectRatioGroup[] = [
  {
    key: "landscape",
    label: "横向",
    options: [
      { value: "1:1", preview: { width: 1, height: 1 } },
      { value: "4:3", preview: { width: 4, height: 3 } },
      { value: "10:7", preview: { width: 10, height: 7 } },
      { value: "3:2", preview: { width: 3, height: 2 } },
      { value: "16:9", preview: { width: 16, height: 9 } },
      { value: "21:9", preview: { width: 21, height: 9 } },
    ],
  },
  {
    key: "portrait",
    label: "竖向",
    options: [
      { value: "4:5", preview: { width: 4, height: 5 } },
      { value: "3:4", preview: { width: 3, height: 4 } },
      { value: "7:10", preview: { width: 7, height: 10 } },
      { value: "2:3", preview: { width: 2, height: 3 } },
      { value: "9:16", preview: { width: 9, height: 16 } },
      { value: "9:21", preview: { width: 9, height: 21 } },
    ],
  },
];

function previewStyle(option: AspectRatioOption): CSSProperties {
  const { width, height } = option.preview;
  const isLandscape = width >= height;
  return {
    aspectRatio: `${width} / ${height}`,
    width: isLandscape ? (width === height ? 24 : 30) : undefined,
    height: isLandscape ? undefined : 30,
  };
}

export function AspectRatioPicker({
  value,
  onChange,
  onClose,
  variant = "popover",
  className,
}: {
  value: AspectRatio;
  onChange: (value: AspectRatio) => void;
  onClose?: () => void;
  variant?: "popover" | "sheet";
  className?: string;
}) {
  const isSheet = variant === "sheet";

  return (
    <div
      className={cn(
        isSheet
          ? "px-4 pb-5"
          : "w-[360px] max-w-[calc(100vw-24px)] p-2",
        className,
      )}
    >
      <div
        className={cn(
          "flex items-center justify-between border-b border-[var(--border-subtle)]",
          isSheet ? "py-3" : "px-1.5 pb-2.5 pt-1",
        )}
      >
        <span className="flex items-center gap-2">
          <span
            className={cn(
              "font-semibold text-[var(--fg-0)]",
              isSheet ? "text-[15px]" : "text-[12px]",
            )}
          >
            宽高比
          </span>
          <span className="h-1 w-1 rounded-full bg-[var(--amber-400)]/80" aria-hidden />
        </span>
        <span
          className={cn(
            "inline-flex h-6 min-w-12 items-center justify-center rounded-full border border-[var(--border-subtle)]",
            "bg-[var(--bg-2)] px-2 text-[11px] text-[var(--fg-1)] tabular-nums",
          )}
          style={{ fontFamily: "var(--font-mono)" }}
        >
          {value}
        </span>
      </div>

      <div className={cn(isSheet ? "space-y-2.5 pt-2.5" : "space-y-3 pt-3")}>
        {ASPECT_RATIO_GROUPS.map((group) => (
          <section key={group.key} aria-labelledby={`aspect-ratio-${group.key}`}>
            <div
              id={`aspect-ratio-${group.key}`}
              className={cn(
                "mb-2 flex items-center gap-2 px-1 text-[11px] font-medium text-[var(--fg-2)]",
                isSheet ? "mb-1.5" : "mb-2",
              )}
            >
              <span>{group.label}</span>
              <span className="h-px flex-1 bg-[var(--border-subtle)]" aria-hidden />
            </div>
            <div
              className={cn(
                "grid grid-cols-2 sm:grid-cols-3",
                isSheet ? "gap-1.5" : "gap-2",
              )}
            >
              {group.options.map((option) => {
                const selected = option.value === value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    aria-pressed={selected}
                    aria-label={`${group.label} ${option.value}`}
                    onClick={() => {
                      onChange(option.value);
                      onClose?.();
                    }}
                    className={cn(
                      "group relative cursor-pointer overflow-hidden rounded-[var(--radius-card)] border px-2.5 text-left",
                      isSheet ? "h-11" : "h-12",
                      "transition-[background-color,border-color,color,box-shadow] duration-200",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                      selected
                        ? "border-[var(--border-amber)] bg-[linear-gradient(135deg,rgba(242,169,58,0.18),rgba(242,169,58,0.07)_48%,rgba(242,169,58,0.03))] text-[var(--amber-300)] shadow-[var(--shadow-amber)]"
                        : "border-[var(--border-subtle)] bg-[var(--bg-2)]/80 text-[var(--fg-0)] hover:border-[var(--border-amber)] hover:bg-[var(--bg-3)]",
                    )}
                  >
                    {selected ? (
                      <span
                        className="absolute right-2 top-2 h-1.5 w-1.5 rounded-full bg-[var(--amber-400)] shadow-[var(--shadow-amber)]"
                        aria-hidden
                      />
                    ) : null}
                    <span className="relative flex h-full items-center gap-2">
                      <span
                        className={cn(
                          "flex h-8 w-9 shrink-0 items-center justify-center rounded-[7px] border transition-[background-color,border-color] duration-200",
                          selected
                            ? "border-[var(--amber-400)]/45 bg-[var(--amber-400)]/12"
                            : "border-[var(--border-subtle)] bg-[var(--bg-0)]/35 group-hover:border-[var(--border-amber)]",
                        )}
                        aria-hidden
                      >
                        <span
                          className={cn(
                            "block rounded-[3px] border transition-[background-color,border-color] duration-200",
                            selected
                              ? "border-[var(--amber-400)] bg-[var(--amber-400)]/28"
                              : "border-[var(--fg-2)]/70 bg-[var(--fg-3)]/45 group-hover:border-[var(--fg-1)]/80",
                          )}
                          style={previewStyle(option)}
                        />
                      </span>
                      <span
                        className="block min-w-0 text-[15px] font-semibold leading-none tabular-nums"
                        style={{ fontFamily: "var(--font-mono)" }}
                      >
                        {option.value}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
