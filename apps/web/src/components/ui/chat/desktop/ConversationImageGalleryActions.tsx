import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";

import type { ModelLibraryItemAgeSegment } from "@/lib/apiClient";
import { cn } from "@/lib/utils";

export type GalleryFavoriteGender = "female" | "male";

const FAVORITE_AGE_OPTIONS: Array<[ModelLibraryItemAgeSegment, string]> = [
  ["user_favorites", "用户收藏"],
  ["toddler", "幼儿"],
  ["child", "儿童"],
  ["teen", "青少年"],
  ["young_adult", "青年"],
  ["adult", "熟龄"],
  ["middle_aged", "中年"],
  ["senior", "老年"],
];

const FAVORITE_GENDER_OPTIONS: Array<[GalleryFavoriteGender, string]> = [
  ["female", "女"],
  ["male", "男"],
];

export function GalleryActionButton({
  children,
  icon,
  tone = "default",
  loading,
  disabled,
  title,
  onClick,
}: {
  children: ReactNode;
  icon: ReactNode;
  tone?: "default" | "accent" | "danger";
  loading?: boolean;
  disabled?: boolean;
  title?: string;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || loading}
      title={title}
      className={cn(
        "inline-flex h-7 min-h-11 items-center gap-1.5 rounded-[var(--radius-card)] px-2.5 text-[11px]",
        "border transition-colors disabled:cursor-not-allowed disabled:opacity-55",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        tone === "accent"
          ? "border-[rgba(242,169,58,0.32)] bg-[rgba(242,169,58,0.15)] text-[var(--amber-300)] hover:bg-[rgba(242,169,58,0.22)]"
          : tone === "danger"
            ? "border-danger-border bg-danger-soft text-danger hover:brightness-110"
            : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      {loading ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
      ) : (
        icon
      )}
      {children}
    </button>
  );
}

export function FavoriteOptionsForm({
  count,
  ageSegment,
  gender,
  onAgeSegmentChange,
  onGenderChange,
}: {
  count: number;
  ageSegment: ModelLibraryItemAgeSegment;
  gender: GalleryFavoriteGender;
  onAgeSegmentChange: (value: ModelLibraryItemAgeSegment) => void;
  onGenderChange: (value: GalleryFavoriteGender) => void;
}) {
  return (
    <div className="mt-3 space-y-3">
      <p className="text-[12px] leading-5 text-[var(--fg-1)]">
        将 {count} 张图片加入用户收藏，并自动识别气质标签。
      </p>
      <label className="block">
        <span className="mb-1 block font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
          年龄段
        </span>
        <select
          value={ageSegment}
          onChange={(event) =>
            onAgeSegmentChange(event.target.value as ModelLibraryItemAgeSegment)
          }
          className={cn(
            "h-9 w-full rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] px-2.5",
            "text-[13px] text-[var(--fg-0)] focus:border-[var(--border-amber)] focus:outline-none",
          )}
        >
          {FAVORITE_AGE_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <div>
        <span className="mb-1 block font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
          性别
        </span>
        <div className="flex gap-2">
          {FAVORITE_GENDER_OPTIONS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              aria-pressed={gender === value}
              onClick={() => onGenderChange(value)}
              className={cn(
                "inline-flex h-8 min-h-11 items-center rounded-[var(--radius-card)] border px-3 text-[12px] transition-colors",
                gender === value
                  ? "border-[var(--border-amber)] bg-[var(--amber-soft)] text-[var(--amber-300)]"
                  : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

export function FilterButton({
  active,
  label,
  count,
  onClick,
}: {
  active: boolean;
  label: string;
  count: number;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "inline-flex h-7 min-h-11 items-center gap-1 rounded-[var(--radius-control)] px-2.5 text-[11px] transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        active
          ? "bg-[var(--bg-2)] text-[var(--fg-0)]"
          : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      {label}
      <span className="font-mono text-[10px] text-current/65">{count}</span>
    </button>
  );
}
