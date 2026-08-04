"use client";

import type { CSSProperties } from "react";
import { Button } from "@/components/ui/primitives";
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
          <span className={isSheet ? "type-card-title" : "type-label"}>
            宽高比
          </span>
          <span className="h-1 w-1 rounded-full bg-[var(--fg-3)]" aria-hidden />
        </span>
        <span
          className={cn(
            "inline-flex h-6 min-w-12 items-center justify-center rounded-full border border-[var(--border-subtle)]",
            "bg-[var(--bg-2)] px-2 type-caption text-[var(--fg-1)] tabular-nums",
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
                "mb-2 flex items-center gap-2 px-1 type-caption text-[var(--fg-2)]",
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
                  <Button
                    key={option.value}
                    variant="ghost"
                    size="md"
                    aria-pressed={selected}
                    aria-label={`${group.label} ${option.value}`}
                    onClick={() => {
                      onChange(option.value);
                      onClose?.();
                    }}
                    className={cn(
                      "group relative cursor-pointer justify-start overflow-hidden rounded-[var(--radius-card)] border px-2.5 text-left",
                      isSheet ? "h-11" : "h-12",
                      "transition-[background-color,border-color,color,box-shadow] duration-200",
                      selected
                        ? "border-accent-border bg-accent-soft text-accent"
                        : "border-[var(--border-subtle)] bg-[var(--bg-2)]/80 text-[var(--fg-0)] hover:border-[var(--border-strong)] hover:bg-[var(--bg-3)]",
                    )}
                  >
                    {selected ? (
                      <span
                        className="absolute right-2 top-2 h-1.5 w-1.5 rounded-full bg-[var(--accent)]"
                        aria-hidden
                      />
                    ) : null}
                    <span className="relative flex h-full items-center gap-2">
                      <span
                        className={cn(
                          "flex h-8 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)] border transition-[background-color,border-color] duration-200",
                          selected
                            ? "border-accent-border bg-accent-soft"
                            : "border-[var(--border-subtle)] bg-[var(--bg-0)]/35 group-hover:border-[var(--border-strong)]",
                        )}
                        aria-hidden
                      >
                        <span
                          className={cn(
                            "block rounded-[var(--radius-control)] border transition-[background-color,border-color] duration-200",
                            selected
                              ? "border-accent-border bg-accent-soft"
                              : "border-[var(--border-strong)] bg-[var(--bg-3)] group-hover:border-[var(--fg-1)]",
                          )}
                          style={previewStyle(option)}
                        />
                      </span>
                      <span
                        className="type-body block min-w-0 leading-none tabular-nums text-[var(--fg-0)]"
                        style={{ fontFamily: "var(--font-mono)" }}
                      >
                        {option.value}
                      </span>
                    </span>
                  </Button>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
