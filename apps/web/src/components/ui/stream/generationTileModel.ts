import { formatDistanceToNowStrict } from "date-fns";
import { zhCN } from "date-fns/locale";

import { imageVariantUrl } from "@/lib/apiClient";
import type { GenerationSummary } from "@/lib/queries/stream";
import type { GeneratedImage } from "@/lib/types";

export interface GenerationTileModel {
  imageId: string;
  imageSources: string[];
  imageSrcSet: string | undefined;
  lightboxPrewarmSources: Array<string | null | undefined>;
  age: string;
  width: number;
  height: number;
  promptShort: string;
  promptTruncated: boolean;
  altText: string;
}

function formatAge(iso: string): string {
  try {
    return formatDistanceToNowStrict(new Date(iso), {
      addSuffix: false,
      locale: zhCN,
    });
  } catch {
    return "";
  }
}

function mimeFromOutputFormat(
  format: string | null | undefined,
): string | undefined {
  if (format === "jpeg") return "image/jpeg";
  if (format === "png") return "image/png";
  if (format === "webp") return "image/webp";
  return undefined;
}

function extensionFromMime(mime: string | null | undefined): string {
  if (!mime) return "png";
  const normalized = mime.split(";")[0]?.trim().toLowerCase();
  if (normalized === "image/jpeg") return "jpg";
  if (normalized === "image/png") return "png";
  if (normalized === "image/webp") return "webp";
  return "png";
}

function imageMimeFor(item: GenerationSummary): string | undefined {
  return item.image.mime ?? mimeFromOutputFormat(item.output_format);
}

function uniqueImageSources(item: GenerationSummary): string[] {
  const seen = new Set<string>();
  return [
    item.image.thumb_url,
    item.image.preview_url,
    item.image.display_url,
    item.image.url,
  ]
    .filter((source): source is string => Boolean(source?.trim()))
    .filter((source) => {
      if (seen.has(source)) return false;
      seen.add(source);
      return true;
    });
}

function imageSrcSetFor(item: GenerationSummary): string | undefined {
  const seen = new Set<string>();
  const candidates: Array<[string | null | undefined, number]> = [
    [item.image.thumb_url, 256],
    [item.image.preview_url, 1024],
  ];
  const srcSet = candidates
    .filter(([source]) => Boolean(source?.trim()))
    .filter(([source]) => {
      const value = source as string;
      if (seen.has(value)) return false;
      seen.add(value);
      return true;
    })
    .map(([source, width]) => `${source} ${width}w`);
  return srcSet.length > 0 ? srcSet.join(", ") : undefined;
}

export function createGenerationTileModel(
  item: GenerationSummary,
): GenerationTileModel {
  const promptCharacters = Array.from(item.prompt);
  const lightboxPreview =
    item.image.display_url ??
    item.image.preview_url ??
    imageVariantUrl(item.image.id, "display2048");

  return {
    imageId: item.image.id,
    imageSources: uniqueImageSources(item),
    imageSrcSet: imageSrcSetFor(item),
    lightboxPrewarmSources: [lightboxPreview, item.image.preview_url],
    age: formatAge(item.created_at),
    width: Math.max(1, item.image.width || 1),
    height: Math.max(1, item.image.height || 1),
    promptShort: promptCharacters.slice(0, 68).join(""),
    promptTruncated: promptCharacters.length > 68,
    altText: promptCharacters.slice(0, 80).join("") || "生成作品",
  };
}

export function imageSourceFailed(
  sourceCount: number,
  sourceIndex: number,
): boolean {
  return sourceCount === 0 || sourceIndex >= sourceCount;
}

export function imageDownloadName(item: GenerationSummary): string {
  return `${item.id}.${extensionFromMime(imageMimeFor(item))}`;
}

export function buildGeneratedImage(
  item: GenerationSummary,
): GeneratedImage {
  return {
    id: item.image.id,
    data_url: item.image.url,
    mime: imageMimeFor(item),
    display_url: item.image.display_url ?? item.image.url,
    preview_url:
      item.image.preview_url ?? item.image.display_url ?? item.image.thumb_url,
    thumb_url: item.image.thumb_url,
    width: item.image.width,
    height: item.image.height,
    parent_image_id: null,
    from_generation_id: item.id,
    size_requested: item.size_actual,
    size_actual: item.size_actual,
  };
}
