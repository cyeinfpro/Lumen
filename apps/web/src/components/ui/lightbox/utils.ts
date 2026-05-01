import type { LightboxItem } from "./types";

export type LightboxMetadataRow = {
  label: string;
  value: string;
};

export type LightboxMetadataSection = {
  title: string;
  rows: LightboxMetadataRow[];
};

const FALLBACK_URL_BASE = "https://lumen.local";

function hasText(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function formatBooleanMode(value: boolean | undefined): string | null {
  if (value === true) return "快速";
  if (value === false) return "标准";
  return null;
}

export function extensionFromMime(mime: string | null | undefined): string | null {
  if (!mime) return null;
  const normalized = mime.split(";")[0]?.trim().toLowerCase();
  if (!normalized) return null;
  const imagePrefix = "image/";
  const ext = normalized.startsWith(imagePrefix)
    ? normalized.slice(imagePrefix.length)
    : normalized.split("/")[1];
  if (!ext) return null;
  if (ext === "jpeg" || ext === "pjpeg") return "jpg";
  if (ext === "svg+xml") return "svg";
  return ext.replace(/[^a-z0-9]+/g, "");
}

export function extensionFromSrc(
  src: string | null | undefined,
  baseUrl = FALLBACK_URL_BASE,
): string | null {
  if (!src) return null;
  if (src.startsWith("data:")) {
    const mimeMatch = src.match(/^data:([^;,]+)[;,]/);
    return extensionFromMime(mimeMatch?.[1]);
  }
  try {
    const pathname = new URL(src, baseUrl).pathname;
    const match = pathname.match(/\.([a-z0-9]+)$/i);
    return match?.[1]?.toLowerCase() ?? null;
  } catch {
    const match = src.split("?")[0]?.match(/\.([a-z0-9]+)$/i);
    return match?.[1]?.toLowerCase() ?? null;
  }
}

export function getLightboxMimeType(
  item: Pick<LightboxItem, "mime" | "mime_type" | "content_type" | "type">,
): string | null {
  const value = item.mime ?? item.mime_type ?? item.content_type ?? item.type;
  if (!hasText(value)) return null;
  return value.includes("/") ? value : null;
}

export function inferLightboxFileExtension(item: LightboxItem): string {
  return (
    extensionFromMime(getLightboxMimeType(item)) ??
    extensionFromSrc(item.filename ?? item.file_name) ??
    extensionFromSrc(item.url) ??
    extensionFromSrc(item.previewUrl) ??
    "png"
  );
}

export function getLightboxDownloadFilename(item: LightboxItem): string {
  const providedName = item.filename ?? item.file_name;
  if (hasText(providedName) && /\.[a-z0-9]+$/i.test(providedName)) {
    return providedName;
  }
  const ext = inferLightboxFileExtension(item);
  const base = hasText(providedName) ? providedName : `lumen-${item.id || "image"}`;
  return `${base}.${ext}`;
}

export function formatImageDimensions(
  item: Pick<LightboxItem, "size_actual" | "width" | "height">,
): string | null {
  if (hasText(item.size_actual)) return item.size_actual;
  if (
    typeof item.width === "number" &&
    typeof item.height === "number" &&
    item.width > 0 &&
    item.height > 0
  ) {
    return `${item.width} x ${item.height}`;
  }
  return null;
}

export function formatLightboxDate(
  value: string | number | Date | null | undefined,
  locale = "zh-CN",
): string | null {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(locale, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function buildCompactLightboxMetadata(item: LightboxItem): string[] {
  return [
    item.aspect_ratio ? `比例 ${item.aspect_ratio}` : null,
    formatImageDimensions(item),
    item.seed !== undefined && item.seed !== null ? `seed ${String(item.seed)}` : null,
    item.quality ? `render ${item.quality}` : null,
    formatBooleanMode(item.fast),
    item.model ?? item.model_id ?? null,
  ].filter((value): value is string => Boolean(value));
}

export function buildLightboxMetadataSections(
  item: LightboxItem,
): LightboxMetadataSection[] {
  const dimensions = formatImageDimensions(item);
  const mime = getLightboxMimeType(item);
  const type = item.type && item.type !== mime ? item.type : null;
  const createdAt = formatLightboxDate(item.created_at);
  const rows = {
    generation: [
      dimensions ? { label: "尺寸", value: dimensions } : null,
      item.aspect_ratio ? { label: "比例", value: item.aspect_ratio } : null,
      item.seed !== undefined && item.seed !== null
        ? { label: "Seed", value: String(item.seed) }
        : null,
      item.quality ? { label: "渲染", value: item.quality } : null,
      formatBooleanMode(item.fast)
        ? { label: "模式", value: formatBooleanMode(item.fast) as string }
        : null,
      item.model ?? item.model_id
        ? { label: "模型", value: item.model ?? item.model_id ?? "" }
        : null,
    ],
    file: [
      mime ? { label: "MIME", value: mime } : null,
      type ? { label: "类型", value: type } : null,
      { label: "扩展名", value: inferLightboxFileExtension(item) },
    ],
    record: [
      createdAt ? { label: "创建时间", value: createdAt } : null,
      { label: "ID", value: item.id },
    ],
  };

  return [
    { title: "生成参数", rows: compactRows(rows.generation) },
    { title: "文件信息", rows: compactRows(rows.file) },
    { title: "记录", rows: compactRows(rows.record) },
  ].filter((section) => section.rows.length > 0);
}

function compactRows(
  rows: Array<LightboxMetadataRow | null>,
): LightboxMetadataRow[] {
  return rows.filter((row): row is LightboxMetadataRow => Boolean(row?.value));
}
