const MAX_DIM = 2048;
const MIN_COMPRESSED_DIM = 512;
const UPLOAD_TARGET_BYTES = 8 * 1024 * 1024;
const UPLOAD_HARD_MAX_BYTES = 50 * 1024 * 1024;
const UPLOAD_MIME = new Set(["image/png", "image/jpeg", "image/webp"]);
const ENCODE_QUALITIES = [0.9, 0.82, 0.74, 0.66, 0.58];

export function imageFilenameForMime(name: string, mime: string): string {
  const ext =
    mime === "image/webp" ? "webp" : mime === "image/png" ? "png" : "jpg";
  const base = name.trim().replace(/\.[^.]*$/, "") || "image";
  return `${base}.${ext}`;
}

function imageEncodeError(): Error {
  const error = new Error(
    "图像压缩失败：浏览器无法编码当前图片，请换张图试试",
  );
  (error as Error & { code?: string }).code = "image_encode_failed";
  return error;
}

function uploadAbortError(signal?: AbortSignal): DOMException {
  const reason = signal?.reason;
  if (reason instanceof DOMException) return reason;
  return new DOMException("上传已取消", "AbortError");
}

function throwIfUploadAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw uploadAbortError(signal);
}

function loadBrowserImage(
  file: File,
  signal?: AbortSignal,
): Promise<{ img: HTMLImageElement; url: string }> {
  throwIfUploadAborted(signal);
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    let settled = false;

    const cleanup = () => {
      signal?.removeEventListener("abort", onAbort);
    };
    const resolveOnce = () => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve({ img, url });
    };
    const rejectOnce = (error: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      URL.revokeObjectURL(url);
      reject(error);
    };
    const onAbort = () => rejectOnce(uploadAbortError(signal));

    signal?.addEventListener("abort", onAbort, { once: true });
    img.onload = resolveOnce;
    img.onerror = () => rejectOnce(new Error("读取图片失败"));
    img.src = url;
  });
}

function drawImageToCanvas(
  img: HTMLImageElement,
  maxSide: number,
  background: string | null,
): HTMLCanvasElement {
  const width = img.naturalWidth;
  const height = img.naturalHeight;
  const scale = Math.min(1, maxSide / Math.max(width, height));
  const nextWidth = Math.max(1, Math.round(width * scale));
  const nextHeight = Math.max(1, Math.round(height * scale));
  const canvas = document.createElement("canvas");
  canvas.width = nextWidth;
  canvas.height = nextHeight;
  const context = canvas.getContext("2d");
  if (!context) throw imageEncodeError();
  if (background) {
    context.fillStyle = background;
    context.fillRect(0, 0, nextWidth, nextHeight);
  }
  context.drawImage(img, 0, 0, nextWidth, nextHeight);
  return canvas;
}

function canvasToBlob(
  canvas: HTMLCanvasElement,
  mime: "image/webp" | "image/jpeg",
  quality: number,
): Promise<Blob | null> {
  return new Promise((resolve) => {
    canvas.toBlob((blob) => resolve(blob), mime, quality);
  });
}

async function encodeImageForUpload(
  img: HTMLImageElement,
  maxSide: number,
  signal?: AbortSignal,
): Promise<{ blob: Blob; mime: "image/webp" | "image/jpeg" }> {
  let best: { blob: Blob; mime: "image/webp" | "image/jpeg" } | null = null;

  for (const mime of ["image/webp", "image/jpeg"] as const) {
    throwIfUploadAborted(signal);
    const canvas = drawImageToCanvas(
      img,
      maxSide,
      mime === "image/jpeg" ? "#fff" : null,
    );
    for (const quality of ENCODE_QUALITIES) {
      throwIfUploadAborted(signal);
      const blob = await canvasToBlob(canvas, mime, quality);
      throwIfUploadAborted(signal);
      if (!blob || blob.type !== mime) continue;
      if (!best || blob.size < best.blob.size) best = { blob, mime };
      if (blob.size <= UPLOAD_TARGET_BYTES) return { blob, mime };
    }
  }

  if (!best) throw imageEncodeError();
  return best;
}

export function nextCompressedSide(
  currentSide: number,
  encodedBytes: number,
): number {
  const ratio = Math.sqrt(UPLOAD_TARGET_BYTES / Math.max(encodedBytes, 1));
  const shrink = Math.max(0.65, Math.min(0.9, ratio * 0.92));
  return Math.max(MIN_COMPRESSED_DIM, Math.floor(currentSide * shrink));
}

export async function compressToMaxDim(
  file: File,
  options: {
    maxSourceBytes: number;
    maxSourceMessage: string;
    signal?: AbortSignal;
  },
): Promise<File> {
  const { maxSourceBytes, maxSourceMessage, signal } = options;
  if (file.size > maxSourceBytes) {
    throw new Error(maxSourceMessage);
  }

  const { img, url } = await loadBrowserImage(file, signal);
  try {
    throwIfUploadAborted(signal);
    const { naturalWidth: width, naturalHeight: height } = img;
    if (!width || !height) throw new Error("读取图片失败");

    const supportedOriginal = UPLOAD_MIME.has(file.type);
    const oversizedDimensions = Math.max(width, height) > MAX_DIM;
    const oversizedBytes = file.size > UPLOAD_TARGET_BYTES;
    const shouldNormalizeOriginal = file.type === "image/jpeg";
    if (
      supportedOriginal &&
      !shouldNormalizeOriginal &&
      !oversizedDimensions &&
      !oversizedBytes
    ) {
      return file;
    }

    let maxSide = Math.min(MAX_DIM, Math.max(width, height));
    let encoded: { blob: Blob; mime: "image/webp" | "image/jpeg" } | null =
      null;
    for (let attempt = 0; attempt < 6; attempt += 1) {
      encoded = await encodeImageForUpload(img, maxSide, signal);
      if (
        encoded.blob.size <= UPLOAD_TARGET_BYTES ||
        maxSide <= MIN_COMPRESSED_DIM
      ) {
        break;
      }
      maxSide = nextCompressedSide(maxSide, encoded.blob.size);
    }

    if (!encoded) throw imageEncodeError();
    throwIfUploadAborted(signal);
    if (encoded.blob.size > UPLOAD_HARD_MAX_BYTES) {
      throw new Error("图片文件过大，请换一张较小的图片或先压缩后再上传");
    }

    return new File(
      [encoded.blob],
      imageFilenameForMime(file.name, encoded.mime),
      {
        type: encoded.mime,
        lastModified: file.lastModified,
      },
    );
  } finally {
    URL.revokeObjectURL(url);
  }
}
