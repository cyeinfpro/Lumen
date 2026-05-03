"use client";

// 可单选图网格（饰品方案、可选材料）。
// 选中态：琥珀色外环 + 角标"已选"。点击图片触发预览，点击底部按钮触发选中切换。

import { Check } from "lucide-react";

import type { BackendImageMeta } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { imageSrc } from "../utils";

interface SelectableImageGridProps {
  images: BackendImageMeta[];
  selectedImageId: string | null;
  saving?: boolean;
  onSelect: (imageId: string | null) => void;
  onPreview: (image: BackendImageMeta, index: number) => void;
}

export function SelectableImageGrid({
  images,
  selectedImageId,
  saving,
  onSelect,
  onPreview,
}: SelectableImageGridProps) {
  return (
    <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
      {images.map((image, index) => {
        const selected = selectedImageId === image.id;
        return (
          <article
            key={image.id}
            className={cn(
              "overflow-hidden rounded-md border bg-white/[0.03] transition-all duration-[var(--dur-base)]",
              selected
                ? "border-[var(--border-amber)] shadow-[var(--shadow-amber)]"
                : "border-[var(--border)] hover:border-[var(--border-strong)]",
            )}
          >
            <div className="relative">
              <button
                type="button"
                onClick={() => onPreview(image, index)}
                className="block w-full overflow-hidden focus-visible:outline-none"
              >
                <img
                  src={imageSrc(image)}
                  alt="饰品预览"
                  loading="lazy"
                  className="aspect-[4/5] w-full object-cover transition-transform duration-[var(--dur-slow)] hover:scale-[1.02]"
                />
              </button>
              {selected ? (
                <span className="pointer-events-none absolute right-2 top-2 inline-flex h-6 items-center gap-1 rounded-full border border-[var(--border-amber)] bg-[var(--accent)] px-2 text-[10px] font-medium text-black shadow-[var(--shadow-amber)]">
                  <Check className="h-3 w-3" />
                  已选
                </span>
              ) : null}
            </div>
            <button
              type="button"
              onClick={() => onSelect(selected ? null : image.id)}
              disabled={saving}
              className={cn(
                "h-9 w-full border-t px-2 text-sm transition-colors",
                selected
                  ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--fg-0)]"
                  : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/[0.04]",
                "disabled:cursor-not-allowed disabled:opacity-60",
              )}
            >
              {saving ? "正在保存…" : selected ? "取消选择" : "选择此饰品方案"}
            </button>
          </article>
        );
      })}
    </div>
  );
}
