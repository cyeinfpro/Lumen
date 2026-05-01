"use client";

const prewarmedImages = new Set<string>();

function runWhenIdle(work: () => void): void {
  if (typeof window === "undefined") return;
  const requestIdle = window.requestIdleCallback;
  if (typeof requestIdle === "function") {
    requestIdle.call(window, work, { timeout: 700 });
    return;
  }
  window.setTimeout(work, 80);
}

export function prewarmImage(src: string | null | undefined): void {
  if (!src || prewarmedImages.has(src) || typeof window === "undefined") return;
  prewarmedImages.add(src);
  runWhenIdle(() => {
    const img = new Image();
    img.decoding = "async";
    img.src = src;
    void img.decode?.().catch(() => undefined);
  });
}
