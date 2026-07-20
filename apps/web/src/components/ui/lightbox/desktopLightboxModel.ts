import type { GeneratedImage, Generation } from "@/lib/types";

import type {
  LightboxItem,
  LightboxParamBag,
} from "./types";

export const RESET_PAN_OFFSET = { x: 0, y: 0 };
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 5;
export const ZOOM_STEP = 0.25;
export const CLICK_ZOOM = 2;
export const CLICK_TAP_SLOP = 6;
export const CLICK_MAX_DURATION_MS = 400;
export const DESKTOP_THUMB_WINDOW_SIZE = 19;

export type ViewMode = "fit" | "actual" | "fill";
export type DownloadStatus = "idle" | "downloading" | "success" | "error";
export type ShareStatus = "idle" | "creating" | "success" | "error";
export type PanOffset = { x: number; y: number };

export type MousePanState = {
  pointerId: number;
  startX: number;
  startY: number;
  startOffset: PanOffset;
};

export type ImagePointerState = {
  pointerId: number;
  startX: number;
  startY: number;
  startOffset: PanOffset;
  canPan: boolean;
  moved: boolean;
  startTime: number;
};

export type ImageTransientState = {
  key: string;
  loadError: boolean;
  displayFailed: boolean;
  viewOriginal: boolean;
  viewMode: ViewMode;
  zoom: number;
  panOffset: PanOffset;
};

export type DesktopGalleryItem = {
  image: {
    id: string;
    data_url: string;
    preview_url?: string;
    thumb_url?: string;
    mime?: string;
    width?: number;
    height?: number;
    size_actual?: string;
    size_requested?: string;
    quality?: string;
    fast?: boolean;
    created_at?: string;
    filename?: string;
    parent_image_id?: string | null;
    from_generation_id?: string | null;
    metadata_jsonb?: Record<string, unknown> | null;
    diagnostics?: GeneratedImage["diagnostics"] | LightboxItem["diagnostics"];
    revised_prompt?: string | null;
    requested_params?: Record<string, unknown> | null;
    request_params?: Record<string, unknown> | null;
    effective_params?: Record<string, unknown> | null;
    actual_params?: Record<string, unknown> | null;
    provider_attempts?: LightboxItem["provider_attempts"];
  };
  prompt: string;
  started_at?: number;
};

export type DesktopImageMeta = DesktopGalleryItem["image"];
const EMPTY_IMAGE_META: DesktopImageMeta = {
  id: "",
  data_url: "",
};

export type TouchActions = {
  clampPanForCurrentView: (
    offset: PanOffset,
    zoom: number,
    viewMode: ViewMode,
  ) => PanOffset;
  gotoDelta: (delta: 1 | -1) => void;
  handleClose: () => void;
  updateImageState: (
    recipe: (state: ImageTransientState) => ImageTransientState,
  ) => void;
};

export type DesktopActionPresentation = {
  downloadTitle: string;
  downloadText: string;
  shareTitle: string;
  shareText: string;
};

export const EMPTY_DESKTOP_GALLERY: DesktopGalleryItem[] = [];
export const EMPTY_GENERATIONS: Record<string, Generation> = {};

function firstPresent<T>(
  ...values: Array<T | null | undefined>
): T | undefined {
  for (const value of values) {
    if (value !== null && value !== undefined) return value;
  }
  return undefined;
}

function valueOrNull<T>(value: T | null | undefined): T | null {
  return value === undefined ? null : value;
}

function asLightboxParamBag(value: unknown): LightboxParamBag | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as LightboxParamBag;
}

export function createImageState(key: string): ImageTransientState {
  return {
    key,
    loadError: false,
    displayFailed: false,
    viewOriginal: false,
    viewMode: "fit",
    zoom: 1,
    panOffset: RESET_PAN_OFFSET,
  };
}

export function resolveImagePresentation(
  imageState: ImageTransientState,
  imageStateKey: string,
  imageSrc: string | null | undefined,
  imagePreviewSrc: string | null | undefined,
) {
  const activeImageState =
    imageState.key === imageStateKey
      ? imageState
      : createImageState(imageStateKey);
  const hasPreview = Boolean(imagePreviewSrc) && imagePreviewSrc !== imageSrc;
  const displaySrc =
    activeImageState.displayFailed || activeImageState.viewOriginal
      ? imageSrc
      : firstPresent(imagePreviewSrc, imageSrc);
  const sourceLabel =
    hasPreview &&
    !activeImageState.viewOriginal &&
    !activeImageState.displayFailed
      ? "预览"
      : "原图";
  return { activeImageState, displaySrc, sourceLabel };
}

function toDesktopGalleryImage(item: LightboxItem): DesktopImageMeta {
  return {
    id: item.id,
    data_url: item.url,
    preview_url: item.previewUrl,
    thumb_url: firstPresent(item.thumbUrl, item.previewUrl),
    mime: firstPresent(item.mime, item.mime_type, item.content_type),
    width: item.width,
    height: item.height,
    size_actual: item.size_actual,
    size_requested: item.size_requested,
    quality: item.quality,
    fast: item.fast,
    created_at: item.created_at,
    filename: firstPresent(item.filename, item.file_name),
    parent_image_id: valueOrNull(item.parent_image_id),
    from_generation_id: valueOrNull(
      firstPresent(item.from_generation_id, item.generation_id),
    ),
    metadata_jsonb: valueOrNull(item.metadata),
    diagnostics: valueOrNull(item.diagnostics),
    revised_prompt: valueOrNull(item.revised_prompt),
    requested_params: valueOrNull(
      firstPresent(item.requested_params, item.request_params),
    ),
    request_params: valueOrNull(
      firstPresent(item.request_params, item.requested_params),
    ),
    effective_params: valueOrNull(
      firstPresent(item.effective_params, item.actual_params),
    ),
    actual_params: valueOrNull(
      firstPresent(item.actual_params, item.effective_params),
    ),
    provider_attempts: item.provider_attempts,
  };
}

export function toDesktopGalleryItem(
  item: LightboxItem,
): DesktopGalleryItem {
  return {
    image: toDesktopGalleryImage(item),
    prompt: item.prompt ?? "",
  };
}

function extensionFromMime(
  mime: string | null | undefined,
): string | null {
  if (!mime) return null;
  const normalized = mime.split(";")[0]?.trim().toLowerCase();
  if (!normalized?.startsWith("image/")) return null;
  const ext = normalized.slice("image/".length);
  if (!ext) return null;
  if (ext === "jpeg") return "jpg";
  if (ext === "svg+xml") return "svg";
  return ext;
}

function extensionFromSrc(src: string): string | null {
  if (src.startsWith("data:")) {
    const mimeMatch = src.match(/^data:([^;]+);/);
    return extensionFromMime(mimeMatch?.[1]);
  }
  try {
    const pathname = new URL(src, window.location.href).pathname;
    const match = pathname.match(/\.([a-z0-9]+)$/i);
    return match?.[1]?.toLowerCase() ?? null;
  } catch {
    return null;
  }
}

export function downloadFilename(
  id: string | null,
  src: string,
  mime?: string,
  preferred?: string,
): string {
  if (preferred?.trim()) return preferred.trim();
  const ext = firstPresent(extensionFromMime(mime), extensionFromSrc(src), "png");
  return `lumen-${id ?? "image"}.${ext}`;
}

export async function fetchImageBlob(src: string): Promise<Blob> {
  const response = src.startsWith("data:")
    ? await fetch(src)
    : await fetch(src, { credentials: "include" });
  if (!response.ok) {
    throw new Error(`Image download failed: ${response.status}`);
  }
  return response.blob();
}

export function preloadImage(
  src: string | null | undefined,
  signal?: AbortSignal,
): Promise<void> {
  if (!src || typeof window === "undefined") return Promise.resolve();
  if (signal?.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const img = new Image();
    const cleanup = () => {
      img.onload = null;
      img.onerror = null;
      signal?.removeEventListener("abort", onAbort);
    };
    const onAbort = () => {
      cleanup();
      img.src = "";
      reject(signal?.reason ?? new DOMException("Aborted", "AbortError"));
    };
    img.decoding = "async";
    img.onload = () => {
      cleanup();
      resolve();
    };
    img.onerror = () => {
      cleanup();
      reject(new Error("Image preload failed"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    img.src = src;
    if (img.complete && img.naturalWidth > 0) {
      cleanup();
      resolve();
    }
  });
}

export function clampZoom(value: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
}

export function clampPanOffset(
  offset: PanOffset,
  zoom: number,
  viewMode: ViewMode,
  viewport: { width: number; height: number },
  imageSize: { width: number; height: number },
): PanOffset {
  if (zoom <= 1 && viewMode === "fit") return RESET_PAN_OFFSET;

  const scaledWidth = imageSize.width * zoom;
  const scaledHeight = imageSize.height * zoom;
  const overflowX = Math.max(0, (scaledWidth - viewport.width) / 2);
  const overflowY = Math.max(0, (scaledHeight - viewport.height) / 2);
  const slackX = viewport.width * 0.08;
  const slackY = viewport.height * 0.08;
  const maxX = overflowX + slackX;
  const maxY = overflowY + slackY;

  return {
    x: Math.min(maxX, Math.max(-maxX, offset.x)),
    y: Math.min(maxY, Math.max(-maxY, offset.y)),
  };
}

export function formatZoom(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function labelForViewMode(viewMode: ViewMode): string {
  if (viewMode === "actual") return "100%";
  if (viewMode === "fill") return "填满";
  return "适应";
}

export function findCurrentImageMeta(
  gallery: DesktopGalleryItem[],
  imageId: string | null | undefined,
): DesktopImageMeta | null {
  if (!imageId) return null;
  return gallery.find((entry) => entry.image.id === imageId)?.image ?? null;
}

type CurrentLightboxSource = {
  imageId?: string | null;
  imageSrc?: string | null;
  imagePreviewSrc?: string | null;
  imageAlt?: string | null;
};

type ResolvedCurrentLightboxSource = Omit<
  CurrentLightboxSource,
  "imageId" | "imageSrc"
> & {
  imageId: string;
  imageSrc: string;
};

function findEventItem(
  items: LightboxItem[] | null | undefined,
  imageId: string,
): LightboxItem | undefined {
  if (!items) return undefined;
  return items.find((item) => item.id === imageId);
}

function valueOrUndefined<T>(
  value: T | null | undefined,
): T | undefined {
  return value === null ? undefined : value;
}

function fallbackCurrentLightboxItem(
  lightbox: ResolvedCurrentLightboxSource,
  currentImageMeta: DesktopImageMeta | null,
): LightboxItem {
  const meta = currentImageMeta ?? EMPTY_IMAGE_META;
  return {
    id: lightbox.imageId,
    url: lightbox.imageSrc,
    previewUrl: firstPresent(
      lightbox.imagePreviewSrc,
      meta.preview_url,
    ),
    thumbUrl: firstPresent(meta.thumb_url, meta.preview_url),
    prompt: valueOrUndefined(lightbox.imageAlt),
    width: meta.width,
    height: meta.height,
    size_actual: meta.size_actual,
    size_requested: meta.size_requested,
    quality: meta.quality,
    fast: meta.fast,
    mime: meta.mime,
    filename: meta.filename,
    created_at: meta.created_at,
    parent_image_id: valueOrNull(meta.parent_image_id),
    from_generation_id: valueOrNull(meta.from_generation_id),
    diagnostics: asLightboxParamBag(meta.diagnostics),
    revised_prompt: valueOrNull(meta.revised_prompt),
    requested_params: valueOrNull(
      firstPresent(meta.requested_params, meta.request_params),
    ),
    request_params: valueOrNull(
      firstPresent(meta.request_params, meta.requested_params),
    ),
    effective_params: valueOrNull(
      firstPresent(meta.effective_params, meta.actual_params),
    ),
    actual_params: valueOrNull(
      firstPresent(meta.actual_params, meta.effective_params),
    ),
    provider_attempts: meta.provider_attempts,
    metadata: valueOrUndefined(meta.metadata_jsonb),
  };
}

export function buildCurrentLightboxItem(
  lightbox: CurrentLightboxSource,
  currentImageMeta: DesktopImageMeta | null,
  storeEventItems: LightboxItem[] | null | undefined,
  eventItems: LightboxItem[] | null,
): LightboxItem | null {
  if (!lightbox.imageId || !lightbox.imageSrc) return null;
  const directItem = firstPresent(
    findEventItem(storeEventItems, lightbox.imageId),
    findEventItem(eventItems, lightbox.imageId),
  );
  if (directItem) return directItem;
  return fallbackCurrentLightboxItem(
    {
      ...lightbox,
      imageId: lightbox.imageId,
      imageSrc: lightbox.imageSrc,
    },
    currentImageMeta,
  );
}

export function currentGalleryIndex(
  gallery: DesktopGalleryItem[],
  imageId: string | null | undefined,
): number {
  if (!imageId) return -1;
  return gallery.findIndex((entry) => entry.image.id === imageId);
}

export function desktopThumbnailItems(
  gallery: DesktopGalleryItem[],
  currentIndex: number,
): Array<{ entry: DesktopGalleryItem; index: number }> {
  if (
    gallery.length <= DESKTOP_THUMB_WINDOW_SIZE ||
    currentIndex < 0
  ) {
    return gallery.map((entry, index) => ({ entry, index }));
  }
  const radius = Math.floor(DESKTOP_THUMB_WINDOW_SIZE / 2);
  let start = Math.max(0, currentIndex - radius);
  const end = Math.min(
    gallery.length,
    start + DESKTOP_THUMB_WINDOW_SIZE,
  );
  start = Math.max(0, end - DESKTOP_THUMB_WINDOW_SIZE);
  return gallery
    .slice(start, end)
    .map((entry, offset) => ({ entry, index: start + offset }));
}

function downloadPresentation(
  status: DownloadStatus,
): Pick<
  DesktopActionPresentation,
  "downloadTitle" | "downloadText"
> {
  if (status === "downloading") {
    return { downloadTitle: "正在下载...", downloadText: "下载中" };
  }
  if (status === "success") {
    return { downloadTitle: "已开始下载", downloadText: "已下载" };
  }
  if (status === "error") {
    return {
      downloadTitle: "下载失败，已尝试打开原图",
      downloadText: "失败",
    };
  }
  return { downloadTitle: "下载原图（D）", downloadText: "下载" };
}

function sharePresentation(
  status: ShareStatus,
): Pick<DesktopActionPresentation, "shareTitle" | "shareText"> {
  if (status === "creating") {
    return { shareTitle: "正在生成分享链接...", shareText: "分享中" };
  }
  if (status === "success") {
    return { shareTitle: "分享链接已复制", shareText: "已复制" };
  }
  if (status === "error") {
    return { shareTitle: "分享失败", shareText: "失败" };
  }
  return { shareTitle: "生成公开分享链接", shareText: "分享" };
}

export function desktopActionPresentation(
  downloadStatus: DownloadStatus,
  shareStatus: ShareStatus,
): DesktopActionPresentation {
  return {
    ...downloadPresentation(downloadStatus),
    ...sharePresentation(shareStatus),
  };
}

export function resolvePanBoundsInput(
  viewportRect: Pick<DOMRect, "width" | "height"> | undefined,
  imageElement: Pick<HTMLImageElement, "offsetWidth" | "offsetHeight"> | null,
  currentImageMeta: DesktopImageMeta | null,
): {
  viewport: { width: number; height: number };
  imageSize: { width: number; height: number };
} {
  const width = firstPositive(
    imageElement?.offsetWidth,
    currentImageMeta?.width,
    viewportRect?.width,
    1,
  );
  const height = firstPositive(
    imageElement?.offsetHeight,
    currentImageMeta?.height,
    viewportRect?.height,
    1,
  );
  return {
    viewport: {
      width: Math.max(1, viewportRect?.width ?? window.innerWidth),
      height: Math.max(1, viewportRect?.height ?? window.innerHeight),
    },
    imageSize: { width, height },
  };
}

function firstPositive(
  ...values: Array<number | null | undefined>
): number {
  for (const value of values) {
    if (typeof value === "number" && value > 0) return value;
  }
  return 1;
}
