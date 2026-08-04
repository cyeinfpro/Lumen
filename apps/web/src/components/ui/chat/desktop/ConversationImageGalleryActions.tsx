import type { ReactNode } from "react";

import { Button, Select } from "@/components/ui/primitives";
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
    <Button
      size="sm"
      variant="secondary"
      onClick={onClick}
      disabled={disabled || loading}
      loading={loading}
      leftIcon={icon}
      title={title}
      className={cn(
        "h-8 rounded-[var(--radius-control)] px-2.5",
        tone === "accent"
          ? "border-accent-border bg-accent-soft text-accent hover:bg-accent-soft"
          : tone === "danger"
            ? "border-danger-border bg-danger-soft text-danger hover:brightness-110"
            : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      {children}
    </Button>
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
      <p className="type-caption text-[var(--fg-1)]">
        将 {count} 张图片加入用户收藏，并自动识别气质标签。
      </p>
      <label className="block">
        <span className="mb-1 block type-caption text-[var(--fg-2)]">
          年龄段
        </span>
        <Select
          value={ageSegment}
          onChange={(event) =>
            onAgeSegmentChange(event.target.value as ModelLibraryItemAgeSegment)
          }
          className="h-9 min-h-9 bg-[var(--bg-0)] type-body-sm text-[var(--fg-0)]"
        >
          {FAVORITE_AGE_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </Select>
      </label>
      <div>
        <span className="mb-1 block type-caption text-[var(--fg-2)]">
          性别
        </span>
        <div className="flex gap-2">
          {FAVORITE_GENDER_OPTIONS.map(([value, label]) => (
            <Button
              key={value}
              size="sm"
              variant="secondary"
              aria-pressed={gender === value}
              onClick={() => onGenderChange(value)}
              className={cn(
                "h-8 px-3",
                gender === value
                  ? "border-accent-border bg-accent-soft text-accent"
                  : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
              )}
            >
              {label}
            </Button>
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
    <Button
      size="sm"
      variant="ghost"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "h-8 gap-1 px-2.5",
        active
          ? "bg-[var(--bg-2)] text-[var(--fg-0)]"
          : "text-[var(--fg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      {label}
      <span className="type-overline text-current">{count}</span>
    </Button>
  );
}
