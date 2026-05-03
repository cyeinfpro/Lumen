"use client";

// 图片网格：用于商品图、参考图等只读展示。点击触发外部 onPreview。
// hover 浮起 + 琥珀外环（与 SelectableImageGrid 风格保持一致）。

import { Image as ImageIcon } from "lucide-react";

import type { BackendImageMeta } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { imageSrc } from "../utils";

interface ImageGridProps {
  images: BackendImageMeta[];
  compact?: boolean;
  className?: string;
  onPreview?: (image: BackendImageMeta, index: number) => void;
  emptyLabel?: string;
}

export function ImageGrid({
  images,
  compact = false,
  className,
  onPreview,
  emptyLabel = "暂无图片",
}: ImageGridProps) {
  if (!images.length) {
    return (
      <div
        className={cn(
          "flex h-28 flex-col items-center justify-center gap-2 rounded-md border border-dashed border-[var(--border)] bg-[var(--bg-2)] text-sm text-[var(--fg-2)]",
          className,
        )}
      >
        <ImageIcon className="h-4 w-4" />
        {emptyLabel}
      </div>
    );
  }
  return (
    <div
      className={cn(
        "grid gap-2",
        compact ? "grid-cols-2" : "grid-cols-2 md:grid-cols-3",
        className,
      )}
    >
      {images.map((image, index) => (
        <button
          key={image.id}
          type="button"
          onClick={() => onPreview?.(image, index)}
          className={cn(
            "group overflow-hidden rounded-md border border-[var(--border)] bg-[var(--bg-2)] transition-all duration-[var(--dur-base)]",
            "hover:border-[var(--border-strong)] hover:shadow-[var(--shadow-2)]",
            "focus-visible:shadow-[var(--ring)] focus-visible:outline-none",
          )}
        >
          <img
            src={imageSrc(image)}
            alt="项目图片"
            loading="lazy"
            className="aspect-[4/5] w-full object-cover transition-transform duration-[var(--dur-slow)] group-hover:scale-[1.02]"
          />
        </button>
      ))}
    </div>
  );
}

export function ReferenceBlock({
  title,
  images,
  onPreview,
  trailing,
}: {
  title: string;
  images: BackendImageMeta[];
  onPreview?: (image: BackendImageMeta, index: number) => void;
  trailing?: React.ReactNode;
}) {
  return (
    <div className="rounded-md border border-[var(--border)] bg-white/[0.03] p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="text-sm font-medium text-[var(--fg-1)]">{title}</p>
        {trailing}
      </div>
      <ImageGrid images={images} compact onPreview={onPreview} />
    </div>
  );
}
