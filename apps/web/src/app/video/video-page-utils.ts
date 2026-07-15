import { imageVariantUrl } from "@/lib/apiClient";

export function cleanReferencePreviewUrl(
  value: string | null | undefined,
): string | null {
  const clean = value?.trim();
  if (!clean || /^asset:\/\//i.test(clean)) return null;
  return clean;
}

export function imageReferencePreviewUrl(image: {
  id: string;
  thumb_url?: string | null;
  preview_url?: string | null;
  display_url?: string | null;
  url?: string | null;
}): string {
  return (
    cleanReferencePreviewUrl(image.preview_url) ??
    cleanReferencePreviewUrl(image.display_url) ??
    cleanReferencePreviewUrl(image.thumb_url) ??
    cleanReferencePreviewUrl(image.url) ??
    imageVariantUrl(image.id, "display2048")
  );
}

export function motionSafeScrollBehavior(): ScrollBehavior {
  if (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    return "auto";
  }
  return "smooth";
}
