"use client";

export type PrewarmPriority =
  | "visible"
  | "open-intent"
  | "neighbor"
  | "hover"
  | "idle";

export type PrewarmAssetKind = "thumb" | "preview" | "display" | "video";

export interface PrewarmRequest {
  priority: PrewarmPriority;
  assetKind: PrewarmAssetKind;
  deadlineMs?: number;
}

export interface PrewarmHandle {
  cancel: () => void;
}

export interface PrewarmMetrics {
  queueDepth: number;
  activeImages: number;
  activeVideos: number;
  cacheHits: number;
  scheduled: number;
  completed: number;
  failed: number;
  timedOut: number;
  dropped: Record<string, number>;
}

interface PrewarmEnvironment {
  hidden: boolean;
  saveData: boolean;
  effectiveType: string | null;
}

interface SchedulerOptions {
  maxQueue?: number;
  imageConcurrency?: number;
  videoConcurrency?: number;
  imageTimeoutMs?: number;
  videoTimeoutMs?: number;
  cacheLimit?: number;
  now?: () => number;
  environment?: () => PrewarmEnvironment;
  imageLoader?: (
    src: string,
    signal: AbortSignal,
    timeoutMs: number,
  ) => Promise<void>;
  videoLoader?: (
    src: string,
    signal: AbortSignal,
    timeoutMs: number,
  ) => Promise<void>;
}

type JobState = "queued" | "active";
type MediaKind = "image" | "video";

interface PrewarmJob {
  key: string;
  src: string;
  mediaKind: MediaKind;
  assetKind: PrewarmAssetKind;
  priority: PrewarmPriority;
  createdAt: number;
  deadlineAt: number;
  controller: AbortController;
  consumers: Set<symbol>;
  state: JobState;
}

const PRIORITY_WEIGHT: Record<PrewarmPriority, number> = {
  visible: 5,
  "open-intent": 4,
  neighbor: 3,
  hover: 2,
  idle: 1,
};

const DEFAULT_MAX_QUEUE = 32;
const DEFAULT_IMAGE_CONCURRENCY = 3;
const DEFAULT_VIDEO_CONCURRENCY = 1;
const DEFAULT_IMAGE_TIMEOUT_MS = 7_000;
const DEFAULT_VIDEO_TIMEOUT_MS = 7_000;
const DEFAULT_CACHE_LIMIT = 384;

function normalizeSrc(src: string | null | undefined): string | null {
  const value = src?.trim();
  return value ? value : null;
}

function browserEnvironment(): PrewarmEnvironment {
  if (typeof document === "undefined" || typeof navigator === "undefined") {
    return {
      hidden: false,
      saveData: false,
      effectiveType: null,
    };
  }
  const connection = (
    navigator as Navigator & {
      connection?: {
        saveData?: boolean;
        effectiveType?: string;
      };
    }
  ).connection;
  return {
    hidden: document.visibilityState === "hidden",
    saveData: Boolean(connection?.saveData),
    effectiveType: connection?.effectiveType ?? null,
  };
}

function weakConnection(environment: PrewarmEnvironment): boolean {
  return (
    environment.saveData ||
    environment.effectiveType === "slow-2g" ||
    environment.effectiveType === "2g"
  );
}

function prewarmDropReason(
  environment: PrewarmEnvironment,
  request: PrewarmRequest,
): string | null {
  if (environment.hidden && request.priority !== "open-intent") {
    return "page_hidden";
  }
  if (
    weakConnection(environment) &&
    (request.priority === "hover" ||
      request.priority === "idle" ||
      request.assetKind === "display")
  ) {
    return "constrained_network";
  }
  return null;
}

function loadImageWithTimeout(
  src: string,
  signal: AbortSignal,
  timeoutMs: number,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    let settled = false;
    let timeout: number | null = window.setTimeout(
      () => finish(new Error("image_prewarm_timeout")),
      timeoutMs,
    );
    const onAbort = () => finish(new DOMException("Aborted", "AbortError"));
    const cleanup = () => {
      if (timeout !== null) {
        window.clearTimeout(timeout);
        timeout = null;
      }
      signal.removeEventListener("abort", onAbort);
      image.onload = null;
      image.onerror = null;
      image.removeAttribute("src");
    };
    const finish = (error?: Error | DOMException) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };

    signal.addEventListener("abort", onAbort, { once: true });
    image.decoding = "async";
    image.onload = () => {
      const decoded = image.decode?.();
      if (decoded) {
        void decoded.then(() => finish(), () => {
          finish(new Error("image_prewarm_decode_failed"));
        });
      } else {
        finish();
      }
    };
    image.onerror = () => finish(new Error("image_prewarm_failed"));
    image.src = src;
  });
}

function loadVideoMetadataWithTimeout(
  src: string,
  signal: AbortSignal,
  timeoutMs: number,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const video = document.createElement("video");
    let settled = false;
    let timeout: number | null = window.setTimeout(
      () => finish(new Error("video_metadata_prewarm_timeout")),
      timeoutMs,
    );
    const onAbort = () => finish(new DOMException("Aborted", "AbortError"));
    const cleanup = () => {
      if (timeout !== null) {
        window.clearTimeout(timeout);
        timeout = null;
      }
      signal.removeEventListener("abort", onAbort);
      video.onloadedmetadata = null;
      video.onerror = null;
      video.removeAttribute("src");
      try {
        video.load();
      } catch {
        // A detached media element may reject load during teardown.
      }
    };
    const finish = (error?: Error | DOMException) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (error) reject(error);
      else resolve();
    };

    signal.addEventListener("abort", onAbort, { once: true });
    video.preload = "metadata";
    video.muted = true;
    video.playsInline = true;
    video.onloadedmetadata = () => finish();
    video.onerror = () =>
      finish(new Error("video_metadata_prewarm_failed"));
    video.src = src;
    video.load();
  });
}

function incrementCounter(
  counters: Record<string, number>,
  reason: string,
): void {
  counters[reason] = (counters[reason] ?? 0) + 1;
}

export class PrewarmScheduler {
  private readonly maxQueue: number;
  private readonly imageConcurrency: number;
  private readonly videoConcurrency: number;
  private readonly imageTimeoutMs: number;
  private readonly videoTimeoutMs: number;
  private readonly cacheLimit: number;
  private readonly now: () => number;
  private readonly environment: () => PrewarmEnvironment;
  private readonly imageLoader: NonNullable<SchedulerOptions["imageLoader"]>;
  private readonly videoLoader: NonNullable<SchedulerOptions["videoLoader"]>;
  private readonly queue: PrewarmJob[] = [];
  private readonly jobs = new Map<string, PrewarmJob>();
  private readonly fulfilled = new Map<string, number>();
  private readonly dropped: Record<string, number> = {};
  private activeImages = 0;
  private activeVideos = 0;
  private cacheHits = 0;
  private scheduled = 0;
  private completed = 0;
  private failed = 0;
  private timedOut = 0;
  private started = false;
  private destroyed = false;

  constructor(options: SchedulerOptions = {}) {
    this.maxQueue = options.maxQueue ?? DEFAULT_MAX_QUEUE;
    this.imageConcurrency =
      options.imageConcurrency ?? DEFAULT_IMAGE_CONCURRENCY;
    this.videoConcurrency =
      options.videoConcurrency ?? DEFAULT_VIDEO_CONCURRENCY;
    this.imageTimeoutMs = options.imageTimeoutMs ?? DEFAULT_IMAGE_TIMEOUT_MS;
    this.videoTimeoutMs = options.videoTimeoutMs ?? DEFAULT_VIDEO_TIMEOUT_MS;
    this.cacheLimit = options.cacheLimit ?? DEFAULT_CACHE_LIMIT;
    this.now = options.now ?? Date.now;
    this.environment = options.environment ?? browserEnvironment;
    this.imageLoader = options.imageLoader ?? loadImageWithTimeout;
    this.videoLoader = options.videoLoader ?? loadVideoMetadataWithTimeout;
  }

  connect(): void {
    if (this.started || this.destroyed) return;
    this.started = true;
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.onVisibilityChange);
    }
  }

  scheduleImage(
    src: string | null | undefined,
    request: PrewarmRequest,
  ): PrewarmHandle {
    return this.schedule("image", src, request);
  }

  scheduleVideo(
    src: string | null | undefined,
    request: Omit<PrewarmRequest, "assetKind">,
  ): PrewarmHandle {
    return this.schedule("video", src, {
      ...request,
      assetKind: "video",
    });
  }

  scheduleImages(
    sources: Array<string | null | undefined>,
    request: PrewarmRequest,
    max = 3,
  ): PrewarmHandle {
    const handles: PrewarmHandle[] = [];
    const seen = new Set<string>();
    for (const source of sources) {
      const normalized = normalizeSrc(source);
      if (!normalized || seen.has(normalized)) continue;
      seen.add(normalized);
      handles.push(this.scheduleImage(normalized, request));
      if (handles.length >= max) break;
    }
    return {
      cancel: () => {
        for (const handle of handles) handle.cancel();
      },
    };
  }

  snapshot(): PrewarmMetrics {
    return {
      queueDepth: this.queue.length,
      activeImages: this.activeImages,
      activeVideos: this.activeVideos,
      cacheHits: this.cacheHits,
      scheduled: this.scheduled,
      completed: this.completed,
      failed: this.failed,
      timedOut: this.timedOut,
      dropped: { ...this.dropped },
    };
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    if (this.started && typeof document !== "undefined") {
      document.removeEventListener(
        "visibilitychange",
        this.onVisibilityChange,
      );
    }
    this.started = false;
    for (const job of this.jobs.values()) job.controller.abort();
    this.queue.length = 0;
    this.jobs.clear();
    this.fulfilled.clear();
  }

  private readonly onVisibilityChange = () => {
    if (!this.environment().hidden) {
      this.drain();
      return;
    }
    for (const job of this.queue.slice()) {
      if (job.priority !== "open-intent") {
        this.dropJob(job, "page_hidden");
      }
    }
  };

  private schedule(
    mediaKind: MediaKind,
    source: string | null | undefined,
    request: PrewarmRequest,
  ): PrewarmHandle {
    const src = normalizeSrc(source);
    if (!src || this.destroyed) return { cancel: () => undefined };
    const policyDrop = prewarmDropReason(this.environment(), request);
    if (policyDrop) {
      incrementCounter(this.dropped, policyDrop);
      return { cancel: () => undefined };
    }

    const key = `${mediaKind}:${src}`;
    if (this.fulfilled.has(key)) {
      this.cacheHits += 1;
      const fulfilledAt = this.fulfilled.get(key) ?? this.now();
      this.fulfilled.delete(key);
      this.fulfilled.set(key, fulfilledAt);
      return { cancel: () => undefined };
    }

    const consumer = Symbol(key);
    const existing = this.jobs.get(key);
    if (existing) {
      return this.reuseJob(existing, consumer, request.priority);
    }

    if (!this.reserveQueueSlot(request.priority)) {
      incrementCounter(this.dropped, "queue_full");
      return { cancel: () => undefined };
    }

    const now = this.now();
    const timeoutMs =
      request.deadlineMs ??
      (mediaKind === "image" ? this.imageTimeoutMs : this.videoTimeoutMs);
    const job: PrewarmJob = {
      key,
      src,
      mediaKind,
      assetKind: request.assetKind,
      priority: request.priority,
      createdAt: now,
      deadlineAt: now + timeoutMs,
      controller: new AbortController(),
      consumers: new Set([consumer]),
      state: "queued",
    };
    this.queue.push(job);
    this.jobs.set(key, job);
    this.scheduled += 1;
    this.sortQueue();
    this.drain();
    return {
      cancel: () => this.cancelConsumer(job, consumer),
    };
  }

  private reuseJob(
    job: PrewarmJob,
    consumer: symbol,
    priority: PrewarmPriority,
  ): PrewarmHandle {
    this.cacheHits += 1;
    job.consumers.add(consumer);
    if (PRIORITY_WEIGHT[priority] > PRIORITY_WEIGHT[job.priority]) {
      job.priority = priority;
      this.sortQueue();
    }
    return {
      cancel: () => this.cancelConsumer(job, consumer),
    };
  }

  private reserveQueueSlot(priority: PrewarmPriority): boolean {
    if (this.queue.length < this.maxQueue) return true;
    const candidate = this.queue[this.queue.length - 1];
    if (
      !candidate ||
      PRIORITY_WEIGHT[candidate.priority] >= PRIORITY_WEIGHT[priority]
    ) {
      return false;
    }
    this.dropJob(candidate, "replaced_by_priority");
    return true;
  }

  private cancelConsumer(job: PrewarmJob, consumer: symbol): void {
    job.consumers.delete(consumer);
    if (job.consumers.size > 0) return;
    if (job.state === "queued") {
      this.dropJob(job, "cancelled");
      return;
    }
    job.controller.abort();
  }

  private dropJob(job: PrewarmJob, reason: string): void {
    const index = this.queue.indexOf(job);
    if (index >= 0) this.queue.splice(index, 1);
    this.jobs.delete(job.key);
    job.controller.abort();
    incrementCounter(this.dropped, reason);
  }

  private sortQueue(): void {
    this.queue.sort(
      (a, b) =>
        PRIORITY_WEIGHT[b.priority] - PRIORITY_WEIGHT[a.priority] ||
        a.createdAt - b.createdAt,
    );
  }

  private canStart(job: PrewarmJob): boolean {
    return job.mediaKind === "image"
      ? this.activeImages < this.imageConcurrency
      : this.activeVideos < this.videoConcurrency;
  }

  private drain(): void {
    if (this.destroyed) return;
    while (true) {
      const index = this.queue.findIndex((job) => this.canStart(job));
      if (index < 0) return;
      const [job] = this.queue.splice(index, 1);
      if (!job) return;
      this.start(job);
    }
  }

  private start(job: PrewarmJob): void {
    job.state = "active";
    if (job.mediaKind === "image") this.activeImages += 1;
    else this.activeVideos += 1;
    const timeoutMs = Math.max(1, job.deadlineAt - this.now());
    const loader =
      job.mediaKind === "image" ? this.imageLoader : this.videoLoader;
    void loader(job.src, job.controller.signal, timeoutMs)
      .then(() => {
        this.completed += 1;
        this.rememberFulfilled(job.key);
      })
      .catch((error: unknown) => {
        if (
          error instanceof Error &&
          error.message.toLowerCase().includes("timeout")
        ) {
          this.timedOut += 1;
        } else if (!job.controller.signal.aborted) {
          this.failed += 1;
        }
      })
      .finally(() => {
        this.jobs.delete(job.key);
        if (job.mediaKind === "image") {
          this.activeImages = Math.max(0, this.activeImages - 1);
        } else {
          this.activeVideos = Math.max(0, this.activeVideos - 1);
        }
        this.drain();
      });
  }

  private rememberFulfilled(key: string): void {
    this.fulfilled.delete(key);
    this.fulfilled.set(key, this.now());
    while (this.fulfilled.size > this.cacheLimit) {
      const oldest = this.fulfilled.keys().next().value;
      if (typeof oldest !== "string") return;
      this.fulfilled.delete(oldest);
    }
  }
}

export function createPrewarmScheduler(
  options: SchedulerOptions = {},
): PrewarmScheduler {
  return new PrewarmScheduler(options);
}

// Compatibility helpers for callers that do not own a long-lived surface.
// Stateful, bounded scheduling belongs to a component-owned PrewarmScheduler.
export function prewarmImage(src: string | null | undefined): void {
  const normalized = normalizeSrc(src);
  if (!normalized || typeof window === "undefined") return;
  const controller = new AbortController();
  void loadImageWithTimeout(
    normalized,
    controller.signal,
    DEFAULT_IMAGE_TIMEOUT_MS,
  ).catch(() => undefined);
}

export function prewarmImages(
  sources: Array<string | null | undefined>,
  max = 3,
): void {
  const seen = new Set<string>();
  for (const source of sources) {
    const normalized = normalizeSrc(source);
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    prewarmImage(normalized);
    if (seen.size >= max) break;
  }
}

export function prewarmVideoMetadata(
  src: string | null | undefined,
): void {
  const normalized = normalizeSrc(src);
  if (!normalized || typeof window === "undefined") return;
  const controller = new AbortController();
  void loadVideoMetadataWithTimeout(
    normalized,
    controller.signal,
    DEFAULT_VIDEO_TIMEOUT_MS,
  ).catch(() => undefined);
}
