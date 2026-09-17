export interface ImageDownloadFile {
  url: string;
  filename: string;
}

const MIME_EXTENSIONS: Record<string, string> = {
  "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
  "image/gif": "gif", "image/avif": "avif", "image/svg+xml": "svg",
};

export function imageDownloadFilename(filename: string, mime: string): string {
  const clean = filename.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "-").trim() || "image";
  const extension = MIME_EXTENSIONS[mime];
  return extension ? `${clean.replace(/\.[a-z0-9]{1,8}$/i, "")}.${extension}` : clean;
}

/** Fetch before clicking: expired URLs must not silently download an HTML error page. */
export async function downloadImageFile(
  file: ImageDownloadFile,
  signal: AbortSignal,
): Promise<void> {
  if (!file.url) throw new Error("图片下载地址不可用");
  if (signal.aborted) throw new DOMException("已停止下载", "AbortError");
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  let timedOut = false;
  const timer = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 30_000);
  try {
    const response = await fetch(file.url, { credentials: "same-origin", signal: controller.signal });
    if (!response.ok) throw new Error(`图片下载失败（HTTP ${response.status}）`);
    const blob = await response.blob();
    if (!blob.size || (!blob.type.startsWith("image/") && blob.type !== "application/octet-stream")) {
      throw new Error("下载地址未返回有效图片，请刷新后重试");
    }
    if (controller.signal.aborted) throw new DOMException("已停止下载", "AbortError");
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    try {
      anchor.href = url;
      anchor.download = imageDownloadFilename(file.filename, blob.type);
      anchor.hidden = true;
      document.body.appendChild(anchor);
      anchor.click();
    } finally {
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  } catch (error) {
    if (timedOut && !signal.aborted) throw new Error("图片下载超时，请重试");
    throw error;
  } finally {
    window.clearTimeout(timer);
    signal.removeEventListener("abort", abort);
  }
}
