"use client";

// Editorial 图片网格：用于商品图、参考图等只读展示。
// - 去除卡片边框 + bg + shadow，改为纯图 + hairline 分隔
// - hover micro scale；focus visible amber ring

import { Image as ImageIcon } from "lucide-react";
import Image from "next/image";

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
          "flex h-32 flex-col items-center justify-center gap-2 border border-dashed border-[var(--border)] text-[var(--fg-2)]",
          className,
        )}
      >
        <ImageIcon className="h-4 w-4" />
        <span className="font-mono text-[10px] uppercase tracking-[0.18em]">
          {emptyLabel}
        </span>
      </div>
    );
  }
  return (
    <div
      className={cn(
        "grid gap-1",
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
            "group relative aspect-[4/5] overflow-hidden bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          )}
        >
          <Image
            src={imageSrc(image)}
            alt="项目图片"
            width={compact ? 240 : 360}
            height={compact ? 300 : 450}
            sizes={compact ? "240px" : "(max-width: 768px) 50vw, 320px"}
            unoptimized
            className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.04]"
          />
          <span className="absolute left-2 top-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/90 mix-blend-difference">
            {String(index + 1).padStart(2, "0")}
          </span>
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
    <section className="border-t border-[var(--border)] py-3">
      <header className="mb-2 flex items-center justify-between gap-2">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          {title}
        </p>
        {trailing}
      </header>
      <ImageGrid images={images} compact onPreview={onPreview} />
    </section>
  );
}
