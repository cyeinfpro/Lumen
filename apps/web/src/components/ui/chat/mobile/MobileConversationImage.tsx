"use client";

import { memo, useRef, useState } from "react";
import { ImagePlus } from "lucide-react";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { ViewportImage } from "@/components/ui/ViewportImage";
import { cn } from "@/lib/utils";
import { tryCopyTextToClipboard } from "@/lib/clipboard";
import type { Generation, GeneratedImage } from "@/lib/types";
import { imageVariantUrl } from "@/lib/apiClient";
import { prewarmImage } from "@/features/assets";
import { aspectRatioToCss } from "@/lib/sizing";
import { imageResultToLightboxItem } from "@/lib/imageResultLightbox";
import type { LightboxItem } from "@/components/ui/lightbox/types";

function formatElapsed(generation: Generation): string | null {
  if (!generation.finished_at || !generation.started_at) return null;
  const ms = Math.max(0, generation.finished_at - generation.started_at);
  return `${(Math.round(ms / 100) / 10).toFixed(1)}s`;
}

function aspectRatioNumber(
  image: Pick<GeneratedImage, "width" | "height">,
  fallback: string,
): number | null {
  if (image.width && image.height && image.height > 0) {
    return image.width / image.height;
  }
  const match = fallback.match(/^(\d+)\s*:\s*(\d+)$/);
  if (!match) return null;
  const width = Number(match[1]);
  const height = Number(match[2]);
  return width > 0 && height > 0 ? width / height : null;
}

function singleImageWidthClass(ratio: number | null): string {
  if (ratio !== null && ratio < 0.58) return "max-w-[min(44%,176px)]";
  if (ratio !== null && ratio < 0.9) return "max-w-[min(60%,260px)]";
  if (ratio !== null && ratio > 1.7) return "max-w-[min(82%,340px)]";
  return "max-w-[min(76%,320px)]";
}

function openLightbox(
  items: LightboxItem[],
  initialId: string,
  fromRect: DOMRect | null,
) {
  if (typeof window === "undefined" || items.length === 0) return;
  window.dispatchEvent(
    new CustomEvent("lumen:open-lightbox", {
      detail: { items, initialId, fromRect: fromRect ?? undefined },
    }),
  );
}

function conversationImageSrc(image: GeneratedImage): string {
  return (
    image.preview_url ??
    image.thumb_url ??
    image.display_url ??
    image.data_url
  );
}

function lightboxThumbUrl(image: GeneratedImage): string | undefined {
  return image.thumb_url ?? image.preview_url;
}

function isFreeGeneration(
  generation: Generation,
  image: GeneratedImage,
): boolean {
  return (
    generation.billing_free === true ||
    generation.billing_label === "free" ||
    generation.is_dual_race_bonus === true ||
    image.billing_free === true ||
    image.billing_label === "free" ||
    image.is_dual_race_bonus === true
  );
}

interface FinalImageProps {
  gen: Generation;
  image: GeneratedImage;
  onEditImage: (id: string) => void;
  inGrid?: boolean;
}

export const FinalImage = memo(function FinalImage({
  gen,
  image,
  onEditImage,
  inGrid = false,
}: FinalImageProps) {
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [loaded, setLoaded] = useState(false);

  const ratioCss = aspectRatioToCss(gen.aspect_ratio);
  const ratio = aspectRatioNumber(image, gen.aspect_ratio);
  const isLongImage = ratio !== null && ratio < 0.58;
  const cardSrc = conversationImageSrc(image);
  const lightboxPreview =
    image.display_url ?? imageVariantUrl(image.id, "display2048");
  const free = isFreeGeneration(gen, image);
  const elapsed = formatElapsed(gen);
  const tail = [
    gen.aspect_ratio,
    image.size_actual || `${image.width}x${image.height}`,
    elapsed ?? null,
  ]
    .filter(Boolean)
    .join(" · ");

  const handleCopy = () => {
    void tryCopyTextToClipboard(gen.prompt).then((success) => {
      pushMobileToast(
        success ? "已复制 prompt" : "复制失败",
        success ? "success" : "danger",
      );
    });
  };

  const handleClick = () => {
    const rect = imgRef.current?.getBoundingClientRect() ?? null;
    const item = imageResultToLightboxItem(gen, image, {
      previewUrl: lightboxPreview,
      thumbUrl: lightboxThumbUrl(image),
      createdAt: gen.finished_at ?? gen.started_at,
    });
    openLightbox([item], image.id, rect);
  };

  const handlePreviewIntent = () => {
    prewarmImage(lightboxPreview);
  };

  return (
    <div
      className={cn(
        "flex w-full flex-col gap-1",
        inGrid ? "" : singleImageWidthClass(ratio),
      )}
    >
      <button
        type="button"
        onClick={handleClick}
        onPointerDown={handlePreviewIntent}
        onFocus={handlePreviewIntent}
        aria-label="查看大图"
        className={cn(
          "relative block w-full overflow-hidden p-0",
          "rounded-[var(--radius-md)] bg-[var(--bg-1)]",
          "shadow-[var(--shadow-1)]",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          isLongImage &&
            (inGrid
              ? "h-[min(24vh,168px)] min-h-[112px]"
              : "h-[min(30vh,220px)] min-h-[140px]"),
        )}
        style={
          isLongImage
            ? { contain: "layout paint" }
            : { aspectRatio: ratioCss, contain: "layout paint" }
        }
      >
        {!loaded && (
          <span
            aria-hidden
            className="absolute inset-0 bg-[var(--bg-2)] animate-pulse motion-reduce:animate-none"
          />
        )}
        <ViewportImage
          ref={imgRef}
          src={cardSrc}
          alt={gen.prompt}
          rootMargin={inGrid ? "320px 0px" : "520px 0px"}
          persistAfterVisible
          fetchPriority="low"
          onLoad={() => setLoaded(true)}
          className={cn(
            "w-full h-full transition-opacity duration-300 motion-reduce:transition-none",
            isLongImage ? "object-contain" : "object-cover",
            loaded ? "opacity-100" : "opacity-0",
          )}
        />
        {free && (
          <span className="pointer-events-none absolute left-2 top-2 z-10 rounded-full border border-[var(--border-strong)] bg-black/60 px-2 py-0.5 font-mono text-[10px] tracking-[0.14em] text-white backdrop-blur">
            free
          </span>
        )}
      </button>
      <div className="flex items-center gap-1.5 px-0.5">
        <button
          type="button"
          onClick={handleCopy}
          className={cn(
            "flex min-h-11 min-w-0 flex-1 items-center text-left text-[10px] tabular-nums text-[var(--fg-3)]",
            "truncate transition-colors hover:text-[var(--fg-1)] active:opacity-70 motion-reduce:transition-none",
          )}
          style={{ fontFamily: "var(--font-mono)" }}
          aria-label="复制 prompt"
          title={gen.prompt}
        >
          {tail}
        </button>
        <button
          type="button"
          onClick={() => onEditImage(image.id)}
          className={cn(
            "shrink-0 inline-flex min-h-11 items-center gap-1 px-2 rounded-full",
            "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
            "text-[10px] text-[var(--fg-2)] hover:text-[var(--fg-0)]",
            "active:scale-[0.95] transition-[background-color,color,transform] motion-reduce:transition-none",
          )}
          aria-label="用作参考图"
        >
          <ImagePlus className="w-3 h-3" aria-hidden />
          参考图
        </button>
      </div>
    </div>
  );
});
