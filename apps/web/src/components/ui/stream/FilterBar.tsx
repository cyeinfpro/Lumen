"use client";

import { Chip } from "@/components/ui/primitives/mobile";
import type { StreamFeedFilters } from "@/lib/queries/stream";
import { Eraser, Gauge, Image as ImageIcon, SlidersHorizontal } from "lucide-react";

const RATIO_CHOICES: Array<{ value: string | null; label: string }> = [
  { value: null, label: "全部" },
  { value: "9:16", label: "竖图 9:16" },
  { value: "4:5", label: "海报 4:5" },
  { value: "3:4", label: "竖版 3:4" },
  { value: "16:9", label: "横图 16:9" },
  { value: "1:1", label: "方图 1:1" },
  { value: "21:9", label: "超宽 21:9" },
];

export interface FilterBarProps {
  open: boolean;
  filters: StreamFeedFilters;
  onChange: (next: StreamFeedFilters) => void;
  onClear?: () => void;
}

function hasAnyFilter(filters: StreamFeedFilters): boolean {
  return Boolean(filters.ratio || filters.has_ref || filters.fast);
}

export function FilterBar({ open, filters, onChange, onClear }: FilterBarProps) {
  const ratio = filters.ratio ?? null;
  const active = hasAnyFilter(filters);

  return (
    <div className={`stream-collapse ${open ? "open" : ""}`}>
      <div>
        <div
          className="w-full overflow-x-auto border-b border-[var(--border-subtle)] bg-[var(--bg-0)]/60 backdrop-blur-md no-scrollbar"
          style={{ scrollbarWidth: "none" }}
          role="group"
          aria-label="筛选"
        >
          <div className="flex min-w-max items-center gap-2 px-3 py-2.5">
            <span className="inline-flex h-8 items-center gap-1.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2.5 text-[11px] text-[var(--fg-2)]">
              <SlidersHorizontal className="h-3.5 w-3.5" />
              筛选
            </span>
            {RATIO_CHOICES.map((r) => {
              const selected = ratio === r.value;
              return (
                <Chip
                  key={r.label}
                  active={selected}
                  onClick={() =>
                    onChange({
                      ...filters,
                      ratio: r.value ?? undefined,
                    })
                  }
                >
                  {r.label}
                </Chip>
              );
            })}
            <span
              aria-hidden
              className="mx-1 inline-block h-5 w-px bg-[var(--border-subtle)]"
            />
            <Chip
              active={Boolean(filters.has_ref)}
              icon={<ImageIcon className="h-3.5 w-3.5" />}
              onClick={() =>
                onChange({ ...filters, has_ref: !filters.has_ref })
              }
            >
              含参考图
            </Chip>
            <Chip
              active={Boolean(filters.fast)}
              icon={<Gauge className="h-3.5 w-3.5" />}
              onClick={() => onChange({ ...filters, fast: !filters.fast })}
            >
              Fast
            </Chip>
            {active && (
              <Chip
                active={false}
                icon={<Eraser className="h-3.5 w-3.5" />}
                onClick={() => {
                  if (onClear) onClear();
                  else onChange({});
                }}
                className="text-[var(--fg-0)]"
              >
                清除
              </Chip>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
