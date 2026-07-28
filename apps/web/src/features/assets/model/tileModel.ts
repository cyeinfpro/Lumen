import { formatDistanceToNowStrict } from "date-fns";
import { zhCN } from "date-fns/locale";

import { imageVariantUrl } from "@/lib/apiClient";
import type { GenerationSummary } from "./contracts";
import type { GeneratedImage } from "@/lib/types";
import {
  assetCandidateVersion,
  confirmedOpenCandidates,
  gridAssetCandidates,
  hoverPrewarmCandidates,
  type AssetCandidate,
} from "./sourceCandidates";

export interface GenerationTileModel {
  imageId: string;
  sourceVersion: string;
  gridCandidates: AssetCandidate[];
  hoverPrewarmSources: string[];
  openPrewarmSources: string[];
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

export function createGenerationTileModel(
  item: GenerationSummary,
): GenerationTileModel {
  const promptCharacters = Array.from(item.prompt);
  const openCandidates = confirmedOpenCandidates(item.image);
  if (!openCandidates.some((entry) => entry.kind === "display2048")) {
    openCandidates.unshift({
      kind: "display2048",
      src: imageVariantUrl(item.image.id, "display2048"),
      width: 2048,
      ready: false,
    });
  }

  return {
    imageId: item.image.id,
    sourceVersion: assetCandidateVersion(item.image),
    gridCandidates: gridAssetCandidates(item.image),
    hoverPrewarmSources: hoverPrewarmCandidates(item.image).map(
      (entry) => entry.src,
    ),
    openPrewarmSources: openCandidates.map((entry) => entry.src),
    age: formatAge(item.created_at),
    width: Math.max(1, item.image.width || 1),
    height: Math.max(1, item.image.height || 1),
    promptShort: promptCharacters.slice(0, 68).join(""),
    promptTruncated: promptCharacters.length > 68,
    altText: promptCharacters.slice(0, 80).join("") || "生成作品",
  };
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
      item.image.preview_url ??
      item.image.display_url ??
      item.image.thumb_url ??
      undefined,
    thumb_url: item.image.thumb_url ?? undefined,
    width: item.image.width,
    height: item.image.height,
    parent_image_id: null,
    from_generation_id: item.id,
    size_requested: item.size_actual,
    size_actual: item.size_actual,
  };
}
