"use client";

// Editorial 可单选图网格：去除卡片，纯图 + hairline + minimal 选择按钮。
// 选中态：amber ring + 按钮高亮 + 角标。

import { Check } from "lucide-react";
import Image from "next/image";

import { Spinner } from "@/components/ui/primitives/Spinner";
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
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      {images.map((image, index) => {
        const selected = selectedImageId === image.id;
        return (
          <article key={image.id} className="group">
            <div className="relative">
              <button
                type="button"
                onClick={() => onPreview(image, index)}
                className={cn(
                  "relative block aspect-[4/5] w-full overflow-hidden bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
                  selected && "ring-1 ring-inset ring-[var(--border-amber)]",
                )}
              >
                <Image
                  src={imageSrc(image)}
                  alt="饰品预览"
                  fill
                  sizes="(max-width: 768px) 50vw, 360px"
                  unoptimized
                  className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
                />
              </button>
              <span className="pointer-events-none absolute left-2 top-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/90 mix-blend-difference">
                N°{String(index + 1).padStart(2, "0")}
              </span>
              {selected ? (
                <span className="pointer-events-none absolute right-2 top-2 inline-flex items-center gap-1.5 rounded-full bg-[var(--amber-400)] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--accent-on)] shadow-[var(--shadow-amber)]">
                  <Check className="h-3 w-3" />
                  已选中
                </span>
              ) : null}
            </div>
            <button
              type="button"
              onClick={() => onSelect(selected ? null : image.id)}
              disabled={saving}
              className={cn(
                "mt-2 flex h-10 w-full items-center justify-center font-mono text-[10px] uppercase tracking-[0.18em] transition-colors",
                selected
                  ? "border-b border-[var(--border-amber)] text-[var(--amber-300)]"
                  : "border-b border-[var(--border)] text-[var(--fg-1)] hover:border-[var(--border-strong)] hover:text-[var(--fg-0)]",
                "disabled:cursor-not-allowed disabled:opacity-60",
              )}
            >
              {saving ? "保存中…" : selected ? "取消选择" : "选择此饰品"}
            </button>
          </article>
        );
      })}
    </div>
  );
}

export function SelectableImageGridLoading({
  count = 4,
  label = "生成中",
}: {
  count?: number;
  label?: string;
}) {
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      {Array.from({ length: count }).map((_, index) => (
        <article key={index}>
          <div className="flex aspect-[4/5] flex-col items-center justify-center gap-2 bg-[var(--bg-2)] text-[var(--fg-2)]">
            <Spinner size={20} />
            <span className="font-mono text-[10px] uppercase tracking-[0.18em]">
              {label}
            </span>
          </div>
          <div className="mt-2 h-10 border-b border-[var(--border)]" />
        </article>
      ))}
    </div>
  );
}
