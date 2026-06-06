"use client";

const IMAGE_CACHE_LIMIT = 384;
const VIDEO_CACHE_LIMIT = 96;
const IMAGE_PREWARM_CONCURRENCY = 3;
const VIDEO_PREWARM_CONCURRENCY = 1;
const VIDEO_METADATA_TIMEOUT_MS = 7000;

type PrewarmEntry = {
  status: "pending" | "fulfilled";
  promise: Promise<void>;
};

const prewarmedImages = new Map<string, PrewarmEntry>();
const prewarmedVideos = new Map<string, PrewarmEntry>();
const imageQueue: Array<() => void> = [];
const videoQueue: Array<() => void> = [];
let activeImagePrewarms = 0;
let activeVideoPrewarms = 0;

function runWhenIdle(work: () => void): void {
  if (typeof window === "undefined") return;
  const requestIdle = window.requestIdleCallback;
  if (typeof requestIdle === "function") {
    requestIdle.call(window, work, { timeout: 700 });
    return;
  }
  window.setTimeout(work, 80);
}

function normalizeSrc(src: string | null | undefined): string | null {
  const trimmed = src?.trim();
  return trimmed ? trimmed : null;
}

function prunePrewarmMap(map: Map<string, PrewarmEntry>, limit: number): void {
  while (map.size > limit) {
    let victim: string | null = null;
    for (const [key, entry] of map) {
      if (entry.status === "fulfilled") {
        victim = key;
        break;
      }
      victim ??= key;
    }
    if (!victim) return;
    map.delete(victim);
  }
}

function rememberPrewarm(
  map: Map<string, PrewarmEntry>,
  key: string,
  entry: PrewarmEntry,
  limit: number,
): void {
  if (map.has(key)) map.delete(key);
  map.set(key, entry);
  prunePrewarmMap(map, limit);
}

function loadImage(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    let settled = false;
    const finish = (ok: boolean) => {
      if (settled) return;
      settled = true;
      if (ok) resolve();
      else reject(new Error("image_prewarm_failed"));
    };

    img.decoding = "async";
    img.onload = () => {
      const decode = img.decode?.();
      if (decode) void decode.then(() => finish(true), () => finish(false));
      else finish(true);
    };
    img.onerror = () => finish(false);
    img.src = src;
  });
}

function loadVideoMetadata(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    let settled = false;
    let timeout: number | null = window.setTimeout(() => {
      finish(false);
    }, VIDEO_METADATA_TIMEOUT_MS);

    const cleanup = () => {
      if (timeout !== null) {
        window.clearTimeout(timeout);
        timeout = null;
      }
      video.onloadedmetadata = null;
      video.onerror = null;
      video.removeAttribute("src");
      try {
        video.load();
      } catch {
        /* no-op */
      }
    };
    const finish = (ok: boolean) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (ok) resolve();
      else reject(new Error("video_metadata_prewarm_failed"));
    };

    video.preload = "metadata";
    video.muted = true;
    video.playsInline = true;
    video.onloadedmetadata = () => finish(true);
    video.onerror = () => finish(false);
    video.src = src;
    video.load();
  });
}

function drainQueue(
  queue: Array<() => void>,
  activeCount: () => number,
  increment: () => void,
  maxActive: number,
): void {
  while (activeCount() < maxActive) {
    const next = queue.shift();
    if (!next) return;
    increment();
    next();
  }
}

function queuePrewarm(
  src: string,
  map: Map<string, PrewarmEntry>,
  queue: Array<() => void>,
  limit: number,
  activeCount: () => number,
  increment: () => void,
  decrement: () => void,
  maxActive: number,
  loader: (src: string) => Promise<void>,
): void {
  const current = map.get(src);
  if (current) {
    rememberPrewarm(map, src, current, limit);
    return;
  }

  const promise = new Promise<void>((resolve, reject) => {
    queue.push(() => {
      runWhenIdle(() => {
        loader(src)
          .then(resolve, reject)
          .finally(() => {
            decrement();
            drainQueue(queue, activeCount, increment, maxActive);
          });
      });
    });
    drainQueue(queue, activeCount, increment, maxActive);
  });
  const entry: PrewarmEntry = { status: "pending", promise };
  rememberPrewarm(map, src, entry, limit);

  void promise.then(
    () => {
      const latest = map.get(src);
      if (latest === entry) {
        rememberPrewarm(map, src, { ...entry, status: "fulfilled" }, limit);
      }
    },
    () => {
      if (map.get(src) === entry) map.delete(src);
    },
  );
}

export function prewarmImage(src: string | null | undefined): void {
  const normalized = normalizeSrc(src);
  if (!normalized || typeof window === "undefined") return;
  queuePrewarm(
    normalized,
    prewarmedImages,
    imageQueue,
    IMAGE_CACHE_LIMIT,
    () => activeImagePrewarms,
    () => {
      activeImagePrewarms += 1;
    },
    () => {
      activeImagePrewarms = Math.max(0, activeImagePrewarms - 1);
    },
    IMAGE_PREWARM_CONCURRENCY,
    loadImage,
  );
}

export function prewarmImages(
  sources: Array<string | null | undefined>,
  max = 3,
): void {
  if (typeof window === "undefined" || max <= 0) return;
  const seen = new Set<string>();
  const normalized = sources
    .map(normalizeSrc)
    .filter((src): src is string => Boolean(src))
    .filter((src) => {
      if (seen.has(src)) return false;
      seen.add(src);
      return true;
    })
    .slice(0, max);
  for (const src of normalized) prewarmImage(src);
}

export function prewarmVideoMetadata(src: string | null | undefined): void {
  const normalized = normalizeSrc(src);
  if (!normalized || typeof window === "undefined") return;
  queuePrewarm(
    normalized,
    prewarmedVideos,
    videoQueue,
    VIDEO_CACHE_LIMIT,
    () => activeVideoPrewarms,
    () => {
      activeVideoPrewarms += 1;
    },
    () => {
      activeVideoPrewarms = Math.max(0, activeVideoPrewarms - 1);
    },
    VIDEO_PREWARM_CONCURRENCY,
    loadVideoMetadata,
  );
}
