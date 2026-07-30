import type { CSSProperties } from "react";
import { format, formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";

import type { PublicShareImageOut, PublicShareOut } from "@/lib/types";

export type ShareImageSurface = "grid" | "single" | "lightbox" | "filmstrip";

export function expirationLabel(
  expiresAt: string | null | undefined,
): string | null {
  return expiresAt ? safeFormat(expiresAt, "yyyy-MM-dd HH:mm") : null;
}

export function normalizeShareImages(
  data: PublicShareOut,
): PublicShareImageOut[] {
  if (Array.isArray(data.images) && data.images.length > 0) {
    return data.images.map(normalizeShareImage);
  }
  return [
    normalizeShareImage({
      id: data.token,
      image_url: data.image_url,
      width: data.width,
      height: data.height,
      mime: data.mime,
      prompt: data.prompt,
    }),
  ];
}

function normalizeShareImage(
  image: PublicShareImageOut,
): PublicShareImageOut {
  return {
    ...image,
    width: Number.isFinite(image.width) ? Math.max(1, image.width) : 1,
    height: Number.isFinite(image.height) ? Math.max(1, image.height) : 1,
    mime: image.mime || "image/png",
    prompt: image.prompt ?? null,
  };
}

export function candidateUrls(
  image: PublicShareImageOut,
  surface: ShareImageSurface,
): string[] {
  const bySurface: Record<
    ShareImageSurface,
    Array<string | null | undefined>
  > = {
    grid: [
      image.preview_url,
      image.thumb_url,
      image.display_url,
      image.image_url,
    ],
    single: [
      image.display_url,
      image.preview_url,
      image.image_url,
      image.thumb_url,
    ],
    lightbox: [
      image.display_url,
      image.preview_url,
      image.image_url,
      image.thumb_url,
    ],
    filmstrip: [
      image.thumb_url,
      image.preview_url,
      image.display_url,
      image.image_url,
    ],
  };
  return uniqueUrls(bySurface[surface]);
}

export function lowQualityPlaceholderUrl(
  image: PublicShareImageOut,
  surface: ShareImageSurface,
): string | null {
  if (surface === "filmstrip") return null;
  return image.thumb_url || image.preview_url || null;
}

export function srcSetForImage(
  image: PublicShareImageOut,
): string | undefined {
  const entries: string[] = [];
  if (image.thumb_url) entries.push(`${image.thumb_url} 256w`);
  if (image.preview_url) entries.push(`${image.preview_url} 1024w`);
  if (image.display_url) entries.push(`${image.display_url} 2048w`);
  if (image.image_url && image.width > 2048) {
    entries.push(`${image.image_url} ${image.width}w`);
  }
  return entries.length > 1 ? entries.join(", ") : undefined;
}

export function sizesForSurface(surface: ShareImageSurface): string {
  if (surface === "grid") {
    return "(min-width: 1280px) 19vw, (min-width: 768px) 24vw, (min-width: 640px) 32vw, 48vw";
  }
  if (surface === "filmstrip") return "56px";
  return "100vw";
}

function uniqueUrls(
  urls: Array<string | null | undefined>,
): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const url of urls) {
    const clean = url?.trim();
    if (!clean || seen.has(clean)) continue;
    seen.add(clean);
    out.push(clean);
  }
  return out;
}

export function preloadShareImage(
  image: PublicShareImageOut | undefined,
  surface: ShareImageSurface,
) {
  if (!image || typeof window === "undefined") return;
  const src = candidateUrls(image, surface)[0];
  if (!src) return;
  const probe = new window.Image();
  probe.decoding = "async";
  probe.src = src;
  if (typeof probe.decode === "function") {
    void probe.decode().catch(() => undefined);
  }
}

export function scheduleIdle(callback: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  if ("requestIdleCallback" in window) {
    const id = window.requestIdleCallback(callback, { timeout: 900 });
    return () => window.cancelIdleCallback(id);
  }
  const id = globalThis.setTimeout(callback, 120);
  return () => globalThis.clearTimeout(id);
}

export function imageFrameStyle(
  image: PublicShareImageOut,
): CSSProperties {
  return {
    aspectRatio: `${image.width} / ${image.height}`,
  };
}

export function singleImageFrameStyle(
  image: PublicShareImageOut,
): CSSProperties {
  return {
    aspectRatio: `${image.width} / ${image.height}`,
    width: `min(94vw, 1240px, ${image.width}px)`,
    maxHeight: "78dvh",
  };
}

export function lightboxImageFrameStyle(
  image: PublicShareImageOut,
): CSSProperties {
  return {
    aspectRatio: `${image.width} / ${image.height}`,
    width: `min(96vw, ${image.width}px)`,
    maxHeight:
      "calc(100dvh - var(--share-lightbox-top-space, 5rem) - var(--share-lightbox-footer-space, 11rem))",
  };
}

export function sharePrompts(
  images: PublicShareImageOut[],
): string[] {
  return uniqueUrls(images.map((image) => image.prompt));
}

export function shareSizeLabel(
  images: PublicShareImageOut[],
): string {
  if (images.length === 1) {
    const image = images[0];
    return `${image.width} x ${image.height} · ${shareMimeLabel(image.mime)}`;
  }
  const first = images[0];
  const sameSize = images.every(
    (image) => image.width === first.width && image.height === first.height,
  );
  return sameSize ? `${first.width} x ${first.height}` : "多尺寸";
}

export function shareImageAlt(image: PublicShareImageOut): string {
  const prompt = image.prompt?.trim();
  return prompt ? prompt.slice(0, 120) : "分享图片";
}

function shareMimeLabel(mime: string): string {
  if (mime === "image/png") return "PNG 格式";
  if (mime === "image/jpeg") return "JPG 格式";
  if (mime === "image/webp") return "WEBP 格式";
  return mime;
}

export function safeDistanceToNow(iso: string): string {
  try {
    return formatDistanceToNow(new Date(iso), {
      addSuffix: true,
      locale: zhCN,
    });
  } catch {
    return iso;
  }
}

function safeFormat(iso: string, pattern: string): string {
  try {
    return format(new Date(iso), pattern, { locale: zhCN });
  } catch {
    return iso;
  }
}
