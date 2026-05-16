const FALLBACK_UPLOAD_SOURCE_BYTES = 50 * 1024 * 1024;

export let MAX_UPLOAD_SOURCE_BYTES = FALLBACK_UPLOAD_SOURCE_BYTES;
export let MAX_UPLOAD_SOURCE_MIB = Math.ceil(
  MAX_UPLOAD_SOURCE_BYTES / (1024 * 1024),
);

export function setMaxUploadSourceBytes(bytes: number | null | undefined): void {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= 0) return;
  MAX_UPLOAD_SOURCE_BYTES = Math.floor(bytes);
  MAX_UPLOAD_SOURCE_MIB = Math.ceil(MAX_UPLOAD_SOURCE_BYTES / (1024 * 1024));
}

export function maxUploadSourceMessage(): string {
  return `图片不能超过 ${MAX_UPLOAD_SOURCE_MIB}MB，请先压缩后再上传`;
}
