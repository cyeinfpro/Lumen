import { API_BASE, apiFetch } from "./http";

// —— 图像上传 / 反代 ——

export interface UploadedImage {
  id: string;
  width: number;
  height: number;
  url: string;
  display_url?: string | null;
  preview_url?: string | null;
  thumb_url?: string | null;
  mime?: string;
  metadata_jsonb?: Record<string, unknown> | null;
}

export interface UploadImageOptions {
  signal?: AbortSignal;
  purpose?: "inpaint_mask" | "volcano_asset";
}

export function uploadImage(
  file: File,
  opts: UploadImageOptions = {},
): Promise<UploadedImage> {
  const fd = new FormData();
  fd.append("file", file);
  if (opts.purpose) fd.append("purpose", opts.purpose);
  return apiFetch<UploadedImage>("/images/upload", {
    method: "POST",
    signal: opts.signal,
    body: fd,
  });
}

export function imageBinaryUrl(imageId: string): string {
  return `${API_BASE.replace(/\/$/, "")}/images/${imageId}/binary`;
}

export function imageVariantUrl(
  imageId: string,
  kind: "display2048" | "preview1024" | "thumb256",
): string {
  return `${API_BASE.replace(/\/$/, "")}/images/${imageId}/variants/${kind}`;
}
