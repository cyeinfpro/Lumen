"use client";

import type { LightboxItem } from "@/components/ui/lightbox/types";
import { imageVariantUrl } from "@/lib/apiClient";
import type { GenerationSummary } from "@/lib/queries/stream";

interface LightboxSourceOptions {
  preferMobilePreview?: boolean;
}

function mimeFromOutputFormat(format: string | null | undefined): string | undefined {
  if (format === "jpeg") return "image/jpeg";
  if (format === "png") return "image/png";
  if (format === "webp") return "image/webp";
  return undefined;
}

export function generationToLightboxItem(
  item: GenerationSummary,
  options: LightboxSourceOptions = {},
): LightboxItem {
  const imageId = item.image.id;
  return {
    id: imageId,
    url: item.image.url,
    previewUrl: options.preferMobilePreview
      ? imageVariantUrl(imageId, "preview1024")
      : (item.image.display_url ?? imageVariantUrl(imageId, "display2048")),
    thumbUrl: imageVariantUrl(imageId, "thumb256"),
    prompt: item.prompt,
    width: item.image.width,
    height: item.image.height,
    aspect_ratio: item.aspect_ratio,
    size_actual: item.size_actual,
    quality: item.quality ?? undefined,
    mime: item.image.mime ?? mimeFromOutputFormat(item.output_format),
    type: item.output_format ? `requested/${item.output_format}` : undefined,
    fast: item.fast,
    created_at: item.created_at,
  };
}

export function openStreamLightbox(
  items: GenerationSummary[],
  initialGenerationId: string,
  fromRect: DOMRect,
) {
  if (typeof window === "undefined") return;
  const preferMobilePreview =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(max-width: 767px)").matches;
  const lbItems = items.map((item) =>
    generationToLightboxItem(item, { preferMobilePreview }),
  );
  const current = items.find((it) => it.id === initialGenerationId);
  const initialId = current ? current.image.id : lbItems[0]?.id;
  if (!initialId) return;

  window.dispatchEvent(
    new CustomEvent("lumen:open-lightbox", {
      detail: { items: lbItems, initialId, fromRect },
    }),
  );
}
