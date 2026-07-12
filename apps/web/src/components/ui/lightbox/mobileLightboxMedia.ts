import type { LightboxItem } from "./types";

const decodedImageSources = new Set<string>();
const decodePromises = new Map<string, Promise<void>>();

export function markImageDecoded(src: string) {
  if (src) decodedImageSources.add(src);
}

export function isImageDecoded(src: string | null | undefined): boolean {
  return Boolean(src && decodedImageSources.has(src));
}

export function displayUrlForItem(
  item: LightboxItem,
  useOriginal: boolean,
): string {
  return useOriginal ? item.url : item.previewUrl || item.url;
}

export function posterUrlForItem(item: LightboxItem): string {
  return item.thumbUrl ?? item.previewUrl ?? item.url;
}

function abortable<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) {
    return Promise.reject(signal.reason ?? new Error("Aborted"));
  }
  const abortSignal = signal;
  return new Promise((resolve, reject) => {
    function cleanup() {
      abortSignal.removeEventListener("abort", onAbort);
    }
    function onAbort() {
      cleanup();
      reject(abortSignal.reason ?? new Error("Aborted"));
    }
    abortSignal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        cleanup();
        resolve(value);
      },
      (error) => {
        cleanup();
        reject(error);
      },
    );
  });
}

export function preloadImage(src: string, signal?: AbortSignal): Promise<void> {
  if (typeof window === "undefined") return Promise.resolve();
  if (decodedImageSources.has(src)) return Promise.resolve();

  let promise = decodePromises.get(src);
  if (!promise) {
    promise = new Promise((resolve, reject) => {
      const img = new Image();
      let settled = false;
      const cleanup = () => {
        img.onload = null;
        img.onerror = null;
      };
      const finish = () => {
        if (settled) return;
        settled = true;
        const decode =
          typeof img.decode === "function"
            ? img.decode().catch(() => undefined)
            : Promise.resolve();
        void decode.then(() => {
          markImageDecoded(src);
          cleanup();
          resolve();
        });
      };
      img.decoding = "async";
      img.onload = finish;
      img.onerror = () => {
        if (settled) return;
        settled = true;
        cleanup();
        decodePromises.delete(src);
        reject(new Error("Image preload failed"));
      };
      img.src = src;
      if (img.complete && img.naturalWidth > 0) finish();
    });
    decodePromises.set(src, promise);
  }

  return abortable(promise, signal);
}

export async function preloadLightboxItem(
  item: LightboxItem,
  signal?: AbortSignal,
): Promise<boolean> {
  const previewSrc = item.previewUrl || item.url;
  try {
    await preloadImage(previewSrc, signal);
    return false;
  } catch {
    if (signal?.aborted) throw signal.reason;
    if (item.previewUrl && item.previewUrl !== item.url) {
      await preloadImage(item.url, signal);
      return true;
    }
    throw new Error("Lightbox item preload failed");
  }
}
