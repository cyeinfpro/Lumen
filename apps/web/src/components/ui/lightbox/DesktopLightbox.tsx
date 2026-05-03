"use client";

// Desktop Lightbox。
// 保留键盘快捷键（j/k/Arrow/D/E/Esc）、鼠标双击缩放、背景点击关闭、缩略图条、
// 触摸手势（pinch / pan / swipe）；展示层优先使用 display2048，下载仍走原图。
//
// Phase 6 分流后，移动端（<768px）走 MobileLightbox；桌面端继续使用本文件。

import { motion, AnimatePresence } from "framer-motion";
import {
  ArrowUpRight,
  ChevronLeft,
  ChevronRight,
  X,
  Download,
  Edit2,
  Info,
  RefreshCw,
  ZoomIn,
  ZoomOut,
  ExternalLink,
  Copy,
  Share2,
  Check,
  Loader2,
  AlertCircle,
  type LucideIcon,
} from "lucide-react";
import { useUiStore } from "@/store/useUiStore";
import { useChatStore } from "@/store/useChatStore";
import type { Generation } from "@/lib/types";
import {
  CLOSE_EVENT,
  OPEN_EVENT,
  type LightboxItem,
  type OpenLightboxDetail,
} from "./types";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { cn } from "@/lib/utils";
import { Tooltip } from "@/components/ui/primitives/Tooltip";
import { useCreateShareMutation } from "@/lib/queries";

const RESET_PAN_OFFSET = { x: 0, y: 0 };
const MIN_ZOOM = 1;
const MAX_ZOOM = 5;
const ZOOM_STEP = 0.25;
const DESKTOP_THUMB_WINDOW_SIZE = 19;
const EMPTY_GENERATIONS: Record<string, Generation> = {};
type ViewMode = "fit" | "actual" | "fill";
type DownloadStatus = "idle" | "downloading" | "success" | "error";
type ShareStatus = "idle" | "creating" | "success" | "error";
type PanOffset = { x: number; y: number };
type MousePanState = {
  pointerId: number;
  startX: number;
  startY: number;
  startOffset: PanOffset;
};
type ImageTransientState = {
  key: string;
  loadError: boolean;
  displayFailed: boolean;
  viewOriginal: boolean;
  viewMode: ViewMode;
  zoom: number;
  panOffset: PanOffset;
};
type DesktopGalleryItem = {
  image: {
    id: string;
    data_url: string;
    preview_url?: string;
    thumb_url?: string;
    mime?: string;
    width?: number;
    height?: number;
    size_actual?: string;
    quality?: string;
    fast?: boolean;
    created_at?: string;
  };
  prompt: string;
  started_at?: number;
};
const EMPTY_DESKTOP_GALLERY: DesktopGalleryItem[] = [];
type TouchActions = {
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

function createImageState(key: string): ImageTransientState {
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

function toDesktopGalleryItem(item: LightboxItem): DesktopGalleryItem {
  return {
    image: {
      id: item.id,
      data_url: item.url,
      preview_url: item.previewUrl,
      thumb_url: item.thumbUrl ?? item.previewUrl,
      mime: item.mime ?? item.mime_type ?? item.content_type,
      width: item.width,
      height: item.height,
      size_actual: item.size_actual,
      quality: item.quality,
      fast: item.fast,
      created_at: item.created_at,
    },
    prompt: item.prompt ?? "",
  };
}

function extensionFromMime(mime: string | null | undefined): string | null {
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

function downloadFilename(id: string | null, src: string, mime?: string): string {
  const ext = extensionFromMime(mime) ?? extensionFromSrc(src) ?? "png";
  return `lumen-${id ?? "image"}.${ext}`;
}

async function fetchImageBlob(src: string): Promise<Blob> {
  const response = src.startsWith("data:")
    ? await fetch(src)
    : await fetch(src, { credentials: "include" });
  if (!response.ok) {
    throw new Error(`Image download failed: ${response.status}`);
  }
  return response.blob();
}

async function writeClipboardText(text: string): Promise<void> {
  if (!navigator.clipboard?.writeText) {
    throw new Error("clipboard unavailable");
  }
  await navigator.clipboard.writeText(text);
}

function preloadImage(
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

function clampZoom(value: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
}

function clampPanOffset(
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

function formatZoom(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function labelForViewMode(viewMode: ViewMode): string {
  if (viewMode === "actual") return "100%";
  if (viewMode === "fill") return "填满";
  return "适应";
}

function formatImageDate(value: string | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function DesktopLightbox() {
  const lightbox = useUiStore((s) => s.lightbox);
  const openLightbox = useUiStore((s) => s.openLightbox);
  const closeLightbox = useUiStore((s) => s.closeLightbox);
  const createShareMutation = useCreateShareMutation();
  const lightboxOpen = lightbox.open;
  const storeEventItems = lightbox.eventItems;
  const imageActionsAvailable = useChatStore((s) =>
    lightbox.imageId ? Boolean(s.imagesById[lightbox.imageId]) : false,
  );
  const [eventGallery, setEventGallery] = useState<DesktopGalleryItem[] | null>(
    null,
  );
  const imageStateKey = `${lightbox.imageSrc ?? ""}\n${lightbox.imagePreviewSrc ?? ""}`;
  const [imageState, setImageState] = useState(() =>
    createImageState(imageStateKey),
  );
  const activeImageState =
    imageState.key === imageStateKey
      ? imageState
      : createImageState(imageStateKey);
  const activeLoadError = activeImageState.loadError;
  const activeDisplayFailed = activeImageState.displayFailed;
  const activeViewOriginal = activeImageState.viewOriginal;
  const activeViewMode = activeImageState.viewMode;
  const activeZoom = activeImageState.zoom;
  const activePanOffset = activeImageState.panOffset;
  const updateImageState = useCallback(
    (recipe: (state: ImageTransientState) => ImageTransientState) => {
      setImageState((prev) =>
        recipe(prev.key === imageStateKey ? prev : createImageState(imageStateKey)),
      );
    },
    [imageStateKey],
  );

  const hasPreview =
    Boolean(lightbox.imagePreviewSrc) && lightbox.imagePreviewSrc !== lightbox.imageSrc;
  const displaySrc =
    activeDisplayFailed || activeViewOriginal
      ? lightbox.imageSrc
      : (lightbox.imagePreviewSrc ?? lightbox.imageSrc);
  const sourceLabel = hasPreview && !activeViewOriginal && !activeDisplayFailed
    ? "预览"
    : "原图";

  // 边界提示 / 键盘动作反馈
  const [edgeHint, setEdgeHint] = useState<null | "first" | "last">(null);
  const [, setSlideDir] = useState<1 | -1>(1);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [promptCopied, setPromptCopied] = useState(false);
  const [downloadStatus, setDownloadStatus] = useState<DownloadStatus>("idle");
  const [shareStatus, setShareStatus] = useState<ShareStatus>("idle");
  const [pendingImageId, setPendingImageId] = useState<string | null>(null);
  const [mousePan, setMousePan] = useState<MousePanState | null>(null);
  const isPanning = mousePan !== null;
  const [mainImageLoaded, setMainImageLoaded] = useState(false);
  const switchSeqRef = useRef(0);
  const preloadAbortRef = useRef<AbortController | null>(null);

  // 当前会话所有成功的 generation → image，按 started_at 升序
  const generations = useChatStore((s) =>
    lightboxOpen ? s.generations : EMPTY_GENERATIONS,
  );
  const chatGallery = useMemo<DesktopGalleryItem[]>(() => {
    if (!lightboxOpen) return EMPTY_DESKTOP_GALLERY;
    const list = Object.values(generations).filter(
      (g) => g.status === "succeeded" && g.image,
    );
    list.sort((a, b) => a.started_at - b.started_at);
    return list.map((g) => ({
      image: g.image!,
      prompt: g.prompt,
      started_at: g.started_at,
    }));
  }, [generations, lightboxOpen]);
  const gallery = useMemo(() => {
    // 优先使用 store 直传的 eventItems（来自 openLightboxFromItems 调用）。
    if (
      storeEventItems?.some((entry) => entry.id === lightbox.imageId)
    ) {
      return storeEventItems.map(toDesktopGalleryItem);
    }
    // 回退到 CustomEvent 传入的 eventGallery（向后兼容）。
    if (
      eventGallery?.some((entry) => entry.image.id === lightbox.imageId)
    ) {
      return eventGallery;
    }
    return chatGallery;
  }, [chatGallery, eventGallery, storeEventItems, lightbox.imageId]);

  const downloadAnchorRef = useRef<HTMLAnchorElement>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const imageWrapRef = useRef<HTMLDivElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const dialogTitleId = useId();

  const hideActiveImageLayer = useCallback(() => {
    const wrap = imageWrapRef.current;
    if (wrap) {
      wrap.style.transition = "opacity 80ms linear";
      wrap.style.opacity = "0";
    }

    const img = imageRef.current;
    if (img) {
      img.style.transition = "opacity 80ms linear";
      img.style.opacity = "0";
      img.style.visibility = "hidden";
    }
  }, []);

  const handleClose = useCallback(() => {
    hideActiveImageLayer();
    switchSeqRef.current += 1;
    preloadAbortRef.current?.abort();
    preloadAbortRef.current = null;
    setEventGallery(null);
    setDetailsOpen(false);
    setPromptCopied(false);
    setPendingImageId(null);
    setMousePan(null);
    closeLightbox();
  }, [closeLightbox, hideActiveImageLayer]);

  useEffect(() => {
    const onOpen = (e: Event) => {
      const detail = (e as CustomEvent<OpenLightboxDetail>).detail;
      if (!detail?.items?.length) return;
      const nextGallery = detail.items.map(toDesktopGalleryItem);
      const target =
        nextGallery.find((entry) => entry.image.id === detail.initialId) ??
        nextGallery[0];
      if (!target) return;
      switchSeqRef.current += 1;
      preloadAbortRef.current?.abort();
      preloadAbortRef.current = null;
      setEventGallery(nextGallery);
      setSlideDir(1);
      setEdgeHint(null);
      setPendingImageId(null);
      openLightbox(
        target.image.id,
        target.image.data_url,
        target.prompt,
        target.image.preview_url ?? target.image.thumb_url,
      );
    };
    const onClose = () => {
      handleClose();
    };
    window.addEventListener(OPEN_EVENT, onOpen as EventListener);
    window.addEventListener(CLOSE_EVENT, onClose);
    return () => {
      window.removeEventListener(OPEN_EVENT, onOpen as EventListener);
      window.removeEventListener(CLOSE_EVENT, onClose);
    };
  }, [handleClose, openLightbox]);

  // 从 gallery 反查元数据（尺寸 / mime）
  const currentImageMeta = useMemo(() => {
    if (!lightbox.imageId) return null;
    for (const g of gallery) {
      if (g.image?.id === lightbox.imageId) return g.image;
    }
    return null;
  }, [gallery, lightbox.imageId]);

  const currentIdx = useMemo(() => {
    if (!lightbox.imageId) return -1;
    return gallery.findIndex((g) => g.image?.id === lightbox.imageId);
  }, [gallery, lightbox.imageId]);

  // 手势读 ref 避免 effect 重订阅；state 写入由 setZoom/setPanOffset 触发渲染
  const zoomRef = useRef(activeZoom);
  const viewModeRef = useRef(activeViewMode);
  const panOffsetRef = useRef(activePanOffset);
  const touchActionsRef = useRef<TouchActions>({
    clampPanForCurrentView: (offset) => offset,
    gotoDelta: () => {},
    handleClose: () => {},
    updateImageState,
  });
  useEffect(() => {
    zoomRef.current = activeZoom;
    viewModeRef.current = activeViewMode;
    panOffsetRef.current = activePanOffset;
  }, [activeZoom, activeViewMode, activePanOffset]);

  const getPanBoundsInput = useCallback(() => {
    const viewportRect = imageWrapRef.current?.getBoundingClientRect();
    const img = imageRef.current;
    const width =
      img?.offsetWidth || currentImageMeta?.width || viewportRect?.width || 1;
    const height =
      img?.offsetHeight || currentImageMeta?.height || viewportRect?.height || 1;
    return {
      viewport: {
        width: Math.max(1, viewportRect?.width ?? window.innerWidth),
        height: Math.max(1, viewportRect?.height ?? window.innerHeight),
      },
      imageSize: {
        width: Math.max(1, width),
        height: Math.max(1, height),
      },
    };
  }, [currentImageMeta?.height, currentImageMeta?.width]);

  const clampPanForCurrentView = useCallback(
    (offset: PanOffset, zoom: number, viewMode: ViewMode) => {
      const { viewport, imageSize } = getPanBoundsInput();
      return clampPanOffset(offset, zoom, viewMode, viewport, imageSize);
    },
    [getPanBoundsInput],
  );

  const handleDownload = useCallback(() => {
    const src = lightbox.imageSrc;
    const id = lightbox.imageId;
    if (!src || downloadStatus === "downloading") return;
    setDownloadStatus("downloading");
    void (async () => {
      const a = downloadAnchorRef.current;
      if (!a) {
        setDownloadStatus("idle");
        return;
      }
      try {
        const blob = await fetchImageBlob(src);
        const objectUrl = URL.createObjectURL(blob);
        a.href = objectUrl;
        a.download = downloadFilename(id, src, blob.type || currentImageMeta?.mime);
        a.click();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        setDownloadStatus("success");
        window.setTimeout(() => setDownloadStatus("idle"), 1400);
      } catch {
        setDownloadStatus("error");
        window.open(src, "_blank", "noopener,noreferrer");
        window.setTimeout(() => setDownloadStatus("idle"), 1800);
      }
    })();
  }, [currentImageMeta, downloadStatus, lightbox.imageSrc, lightbox.imageId]);

  const handleIterate = useCallback(() => {
    const id = lightbox.imageId;
    if (!id) return;
    const hit = useChatStore.getState().imagesById[id];
    if (!hit) return;
    handleClose();
    useChatStore.getState().promoteImageToReference(id);
  }, [lightbox.imageId, handleClose]);

  const handleUpscale = useCallback(() => {
    const id = lightbox.imageId;
    if (!id) return;
    handleClose();
    void useChatStore.getState().upscaleImage(id);
  }, [lightbox.imageId, handleClose]);

  const handleReroll = useCallback(() => {
    const id = lightbox.imageId;
    if (!id) return;
    handleClose();
    void useChatStore.getState().rerollImage(id);
  }, [lightbox.imageId, handleClose]);

  const setZoom = useCallback(
    (nextValue: number | ((current: number) => number)) => {
      updateImageState((state) => {
        const raw =
          typeof nextValue === "function" ? nextValue(state.zoom) : nextValue;
        const zoom = clampZoom(raw);
        const viewMode = zoom > 1 && state.viewMode === "fit" ? "actual" : state.viewMode;
        return {
          ...state,
          zoom,
          viewMode,
          panOffset: clampPanForCurrentView(
            zoom <= 1 && viewMode === "fit" ? RESET_PAN_OFFSET : state.panOffset,
            zoom,
            viewMode,
          ),
        };
      });
    },
    [clampPanForCurrentView, updateImageState],
  );

  const resetView = useCallback(() => {
    updateImageState((state) => ({
      ...state,
      viewMode: "fit",
      zoom: 1,
      panOffset: RESET_PAN_OFFSET,
    }));
  }, [updateImageState]);

  const setViewMode = useCallback(
    (viewMode: ViewMode) => {
      updateImageState((state) => ({
        ...state,
        viewMode,
        zoom: 1,
        panOffset: RESET_PAN_OFFSET,
      }));
    },
    [updateImageState],
  );

  const handleOpenOriginal = useCallback(() => {
    if (!lightbox.imageSrc) return;
    window.open(lightbox.imageSrc, "_blank", "noopener,noreferrer");
  }, [lightbox.imageSrc]);

  const handleCopyPrompt = useCallback(() => {
    const prompt = lightbox.imageAlt?.trim();
    if (!prompt || typeof navigator === "undefined") return;
    const write = navigator.clipboard?.writeText(prompt);
    if (!write) return;
    void write.then(() => {
      setPromptCopied(true);
      window.setTimeout(() => setPromptCopied(false), 1400);
    });
  }, [lightbox.imageAlt]);

  const resetShareStatusSoon = useCallback(() => {
    window.setTimeout(() => setShareStatus("idle"), 1600);
  }, []);

  const handleShare = useCallback(() => {
    const imageId = lightbox.imageId;
    if (!imageId || shareStatus === "creating" || createShareMutation.isPending) {
      return;
    }

    setShareStatus("creating");
    void (async () => {
      let link: string;
      try {
        const share = await createShareMutation.mutateAsync({
          imageId,
          show_prompt: false,
        });
        link = share.url;
      } catch {
        setShareStatus("error");
        resetShareStatusSoon();
        return;
      }

      if (typeof navigator !== "undefined" && typeof navigator.share === "function") {
        try {
          await navigator.share({
            title: "Lumen image",
            text: "Lumen image",
            url: link,
          });
          setShareStatus("success");
          resetShareStatusSoon();
          return;
        } catch (error) {
          if (error instanceof DOMException && error.name === "AbortError") {
            setShareStatus("idle");
            return;
          }
        }
      }

      try {
        await writeClipboardText(link);
        setShareStatus("success");
      } catch {
        setShareStatus("error");
        window.prompt("复制分享链接", link);
      } finally {
        resetShareStatusSoon();
      }
    })();
  }, [
    createShareMutation,
    lightbox.imageId,
    resetShareStatusSoon,
    shareStatus,
  ]);

  const showEdgeHint = useCallback((which: "first" | "last") => {
    setEdgeHint(which);
    setTimeout(() => setEdgeHint(null), 1200);
  }, []);

  const switchToGalleryItem = useCallback(
    (target: DesktopGalleryItem, direction: 1 | -1) => {
      const seq = switchSeqRef.current + 1;
      switchSeqRef.current = seq;
      preloadAbortRef.current?.abort();
      const preloadAbort = new AbortController();
      preloadAbortRef.current = preloadAbort;
      setPendingImageId(target.image.id);
      setEdgeHint(null);

      void (async () => {
        try {
          await preloadImage(
            target.image.preview_url ?? target.image.thumb_url ?? target.image.data_url,
            preloadAbort.signal,
          );
        } catch {
          if (preloadAbort.signal.aborted) return;
          try {
            await preloadImage(target.image.data_url, preloadAbort.signal);
          } catch {
            if (preloadAbort.signal.aborted) return;
            // Let the lightbox switch and surface the existing image error UI.
          }
        }
        if (switchSeqRef.current !== seq) return;
        if (preloadAbortRef.current === preloadAbort) {
          preloadAbortRef.current = null;
        }
        setSlideDir(direction);
        setPendingImageId(null);
        openLightbox(
          target.image.id,
          target.image.data_url,
          target.prompt,
          target.image.preview_url ?? target.image.thumb_url,
        );
      })();
    },
    [openLightbox],
  );

  const gotoDelta = useCallback(
    (delta: 1 | -1) => {
      if (gallery.length === 0) return;
      const idx = currentIdx;
      if (idx < 0) return;
      // 到边界时不循环：给轻抖 + 提示
      if (delta === 1 && idx === gallery.length - 1) {
        showEdgeHint("last");
        return;
      }
      if (delta === -1 && idx === 0) {
        showEdgeHint("first");
        return;
      }
      const n = idx + delta;
      const target = gallery[n];
      if (!target?.image) return;
      switchToGalleryItem(target, delta);
    },
    [gallery, currentIdx, showEdgeHint, switchToGalleryItem],
  );

  const handleWheel = useCallback(
    (e: React.WheelEvent<HTMLDivElement>) => {
      if (!e.ctrlKey && !e.metaKey && activeZoom <= 1) return;
      e.preventDefault();
      const direction = e.deltaY > 0 ? -1 : 1;
      const nextZoom = clampZoom(activeZoom + direction * ZOOM_STEP);
      if (nextZoom === activeZoom) return;
      const wrapRect = imageWrapRef.current?.getBoundingClientRect();
      if (wrapRect) {
        const cx = e.clientX - (wrapRect.left + wrapRect.width / 2);
        const cy = e.clientY - (wrapRect.top + wrapRect.height / 2);
        const ratio = nextZoom / activeZoom;
        const nextPan = nextZoom <= 1
          ? RESET_PAN_OFFSET
          : { x: cx * (1 - ratio) + activePanOffset.x * ratio, y: cy * (1 - ratio) + activePanOffset.y * ratio };
        const vm = nextZoom > 1 && activeViewMode === "fit" ? "actual" : nextZoom <= 1 ? "fit" : activeViewMode;
        updateImageState((state) => ({
          ...state,
          zoom: nextZoom,
          viewMode: vm,
          panOffset: clampPanForCurrentView(nextPan, nextZoom, vm),
        }));
      } else {
        setZoom((z) => z + direction * ZOOM_STEP);
      }
    },
    [activeZoom, activeViewMode, activePanOffset, setZoom, clampPanForCurrentView, updateImageState],
  );

  useEffect(() => {
    touchActionsRef.current = {
      clampPanForCurrentView,
      gotoDelta,
      handleClose,
      updateImageState,
    };
  }, [clampPanForCurrentView, gotoDelta, handleClose, updateImageState]);

  function handleImagePointerDown(e: React.PointerEvent<HTMLImageElement>) {
    const canPan = activeZoom > 1 || activeViewMode !== "fit";
    if (!canPan || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    setMousePan({
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      startOffset: panOffsetRef.current,
    });
    e.currentTarget.setPointerCapture(e.pointerId);
  }

  function handleImagePointerMove(e: React.PointerEvent<HTMLImageElement>) {
    const pan = mousePan;
    if (!pan || pan.pointerId !== e.pointerId) return;
    e.preventDefault();
    const dx = e.clientX - pan.startX;
    const dy = e.clientY - pan.startY;
    const nextOffset = clampPanForCurrentView(
      {
        x: pan.startOffset.x + dx,
        y: pan.startOffset.y + dy,
      },
      activeZoom,
      activeViewMode,
    );
    updateImageState((state) => ({
      ...state,
      panOffset: nextOffset,
    }));
  }

  function handleImagePointerEnd(e: React.PointerEvent<HTMLImageElement>) {
    const pan = mousePan;
    if (!pan || pan.pointerId !== e.pointerId) return;
    setMousePan(null);
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* noop */
    }
  }

  useEffect(() => {
    if (!lightbox.open) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        handleClose();
        return;
      }
      // Tab 焦点循环：把 Tab 限制在 dialog 内部可聚焦元素之间，防止 Tab 出 dialog
      // 让用户聚焦到背景被 inert 不掉的元素（chat 输入框等）。
      if (e.key === "Tab") {
        const root = containerRef.current;
        if (!root) return;
        const focusables = Array.from(
          root.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        ).filter((el) => !el.hasAttribute("data-focus-skip"));
        if (focusables.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (active === first || !root.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else if (active === last) {
          e.preventDefault();
          first.focus();
        }
        return;
      }
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;

      if (e.key === "d" || e.key === "D") {
        e.preventDefault();
        handleDownload();
      } else if (e.key === "e" || e.key === "E") {
        e.preventDefault();
        handleIterate();
      } else if (e.key === "i" || e.key === "I") {
        e.preventDefault();
        setDetailsOpen((open) => !open);
      } else if (e.key === "0") {
        e.preventDefault();
        resetView();
      } else if (e.key === "1" || e.key === "f" || e.key === "F") {
        e.preventDefault();
        setViewMode("fit");
      } else if (e.key === "2" || e.key === "a" || e.key === "A") {
        e.preventDefault();
        setViewMode("actual");
      } else if (e.key === "3") {
        e.preventDefault();
        setViewMode("fill");
      } else if (e.key === "+" || e.key === "=") {
        e.preventDefault();
        setZoom((zoom) => zoom + ZOOM_STEP);
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        setZoom((zoom) => zoom - ZOOM_STEP);
      } else if (e.key === "j" || e.key === "J" || e.key === "ArrowRight") {
        e.preventDefault();
        gotoDelta(1);
      } else if (e.key === "k" || e.key === "K" || e.key === "ArrowLeft") {
        e.preventDefault();
        gotoDelta(-1);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    lightbox.open,
    handleClose,
    handleDownload,
    handleIterate,
    resetView,
    setViewMode,
    setZoom,
    gotoDelta,
  ]);

  // 打开时记住打开前的焦点 + 聚焦关闭按钮（screen reader 第一焦点要在 dialog 内
  // 的可操作元素，而不是容器本身）；关闭时还原焦点到打开者
  useEffect(() => {
    if (!lightbox.open) return;
    previouslyFocusedRef.current = document.activeElement as HTMLElement | null;
    let raf = 0;
    raf = requestAnimationFrame(() => {
      const target = closeButtonRef.current ?? containerRef.current;
      target?.focus({ preventScroll: true });
    });
    return () => {
      cancelAnimationFrame(raf);
      const prev = previouslyFocusedRef.current;
      if (prev && typeof prev.focus === "function") {
        try {
          prev.focus({ preventScroll: true });
        } catch {
          /* noop */
        }
      }
      previouslyFocusedRef.current = null;
    };
  }, [lightbox.open]);

  useEffect(() => {
    if (lightbox.open) return;
    let canceled = false;
    queueMicrotask(() => {
      if (canceled) return;
      setDetailsOpen(false);
      setPromptCopied(false);
      setMousePan(null);
    });
    return () => {
      canceled = true;
    };
  }, [lightbox.open]);

  useEffect(() => {
    let canceled = false;
    queueMicrotask(() => {
      if (canceled) return;
      setPromptCopied(false);
      setDownloadStatus("idle");
      setShareStatus("idle");
      setPendingImageId(null);
      setMousePan(null);
      setMainImageLoaded(false);
    });
    return () => {
      canceled = true;
    };
  }, [imageStateKey]);

  useEffect(() => {
    if (!lightbox.open) return;
    const handleResize = () => {
      updateImageState((state) => ({
        ...state,
        panOffset: clampPanForCurrentView(
          state.panOffset,
          state.zoom,
          state.viewMode,
        ),
      }));
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [clampPanForCurrentView, lightbox.open, updateImageState]);

  // body scroll lock：打开期间禁止背景滚动，关闭时还原
  useEffect(() => {
    if (!lightbox.open) return;
    const { body, documentElement } = document;
    const prevBodyOverflow = body.style.overflow;
    const prevBodyOverscroll = body.style.overscrollBehavior;
    const prevDocOverscroll = documentElement.style.overscrollBehavior;
    document.body.style.overflow = "hidden";
    body.style.overscrollBehavior = "contain";
    documentElement.style.overscrollBehavior = "contain";
    return () => {
      body.style.overflow = prevBodyOverflow;
      body.style.overscrollBehavior = prevBodyOverscroll;
      documentElement.style.overscrollBehavior = prevDocOverscroll;
    };
  }, [lightbox.open]);

  // 触摸手势：
  // - 1 指（zoom=1）：水平 swipe 翻页 / 下拉关闭
  // - 1 指（zoom>1）：平移已放大的图
  // - 2 指：pinch-zoom（捏合缩放）
  useEffect(() => {
    if (!lightbox.open) return;
    const el = containerRef.current;
    if (!el) return;
    const gesture = {
      startX: 0,
      startY: 0,
      mode: "idle" as "idle" | "swipe" | "pan" | "pinch",
      pinchStartDist: 0,
      pinchStartZoom: 1,
      panStartOffset: { x: 0, y: 0 },
    };

    const dist = (a: Touch, b: Touch) =>
      Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);

    const onStart = (e: TouchEvent) => {
      if (e.touches.length === 2) {
        gesture.mode = "pinch";
        gesture.pinchStartDist = dist(e.touches[0], e.touches[1]);
        gesture.pinchStartZoom = zoomRef.current;
      } else if (e.touches.length === 1) {
        gesture.startX = e.touches[0].clientX;
        gesture.startY = e.touches[0].clientY;
        if (zoomRef.current > 1 || viewModeRef.current !== "fit") {
          gesture.mode = "pan";
          gesture.panStartOffset = { ...panOffsetRef.current };
        } else {
          gesture.mode = "swipe";
        }
      }
    };
    const onMove = (e: TouchEvent) => {
      if (e.cancelable && gesture.mode !== "idle") e.preventDefault();
      const {
        clampPanForCurrentView: currentClampPanForCurrentView,
        updateImageState: currentUpdateImageState,
      } = touchActionsRef.current;
      if (gesture.mode === "pinch" && e.touches.length === 2) {
        const d = dist(e.touches[0], e.touches[1]);
        if (gesture.pinchStartDist > 0) {
          const next = Math.min(
            MAX_ZOOM,
            Math.max(1, gesture.pinchStartZoom * (d / gesture.pinchStartDist)),
          );
          currentUpdateImageState((state) => {
            const viewMode =
              next > 1 && state.viewMode === "fit" ? "actual" : state.viewMode;
            return {
              ...state,
              viewMode,
              zoom: next,
              panOffset: currentClampPanForCurrentView(
                next === 1 && viewMode === "fit"
                  ? RESET_PAN_OFFSET
                  : state.panOffset,
                next,
                viewMode,
              ),
            };
          });
        }
      } else if (gesture.mode === "pan" && e.touches.length === 1) {
        const dx = e.touches[0].clientX - gesture.startX;
        const dy = e.touches[0].clientY - gesture.startY;
        const nextOffset = currentClampPanForCurrentView(
          {
            x: gesture.panStartOffset.x + dx,
            y: gesture.panStartOffset.y + dy,
          },
          zoomRef.current,
          viewModeRef.current,
        );
        currentUpdateImageState((state) => ({
          ...state,
          panOffset: nextOffset,
        }));
      }
    };
    const onEnd = (e: TouchEvent) => {
      if (gesture.mode === "swipe" && e.changedTouches.length > 0) {
        const dx = e.changedTouches[0].clientX - gesture.startX;
        const dy = e.changedTouches[0].clientY - gesture.startY;
        const absDx = Math.abs(dx),
          absDy = Math.abs(dy);
        if (absDx > 60 && absDx > absDy * 1.5) {
          touchActionsRef.current.gotoDelta(dx < 0 ? 1 : -1);
        } else if (dy > 80 && absDy > absDx * 1.5) {
          touchActionsRef.current.handleClose();
        }
      }
      if (e.touches.length === 0) gesture.mode = "idle";
    };
    // touchmove 需要 passive:false 才能 preventDefault（pinch/pan 需要）
    el.addEventListener("touchstart", onStart, { passive: true });
    el.addEventListener("touchmove", onMove, { passive: false });
    el.addEventListener("touchend", onEnd, { passive: true });
    el.addEventListener("touchcancel", onEnd, { passive: true });
    return () => {
      el.removeEventListener("touchstart", onStart);
      el.removeEventListener("touchmove", onMove);
      el.removeEventListener("touchend", onEnd);
      el.removeEventListener("touchcancel", onEnd);
    };
  }, [lightbox.open]);

  const hasPrev = currentIdx > 0;
  const hasNext = currentIdx >= 0 && currentIdx < gallery.length - 1;
  const thumbGalleryItems = useMemo(() => {
    if (gallery.length <= DESKTOP_THUMB_WINDOW_SIZE || currentIdx < 0) {
      return gallery.map((entry, index) => ({ entry, index }));
    }
    const radius = Math.floor(DESKTOP_THUMB_WINDOW_SIZE / 2);
    let start = Math.max(0, currentIdx - radius);
    const end = Math.min(gallery.length, start + DESKTOP_THUMB_WINDOW_SIZE);
    start = Math.max(0, end - DESKTOP_THUMB_WINDOW_SIZE);
    return gallery
      .slice(start, end)
      .map((entry, offset) => ({ entry, index: start + offset }));
  }, [currentIdx, gallery]);
  const formattedDate = formatImageDate(currentImageMeta?.created_at);
  const posterSrc =
    currentImageMeta?.thumb_url ??
    currentImageMeta?.preview_url ??
    null;
  const activeViewModeLabel = labelForViewMode(activeViewMode);
  const downloadTitle =
    downloadStatus === "downloading"
      ? "正在下载..."
      : downloadStatus === "success"
        ? "已开始下载"
        : downloadStatus === "error"
          ? "下载失败，已尝试打开原图"
          : "下载原图（D）";
  const downloadText =
    downloadStatus === "downloading"
      ? "下载中"
      : downloadStatus === "success"
        ? "已下载"
        : downloadStatus === "error"
          ? "失败"
          : "下载";
  const DownloadIcon =
    downloadStatus === "downloading"
      ? Loader2
      : downloadStatus === "success"
        ? Check
        : downloadStatus === "error"
          ? AlertCircle
          : Download;
  const shareTitle =
    shareStatus === "creating"
      ? "正在生成分享链接..."
      : shareStatus === "success"
        ? "分享链接已复制"
        : shareStatus === "error"
          ? "分享失败"
          : "生成公开分享链接";
  const shareText =
    shareStatus === "creating"
      ? "分享中"
      : shareStatus === "success"
        ? "已复制"
        : shareStatus === "error"
          ? "失败"
          : "分享";
  const ShareIcon =
    shareStatus === "creating"
      ? Loader2
      : shareStatus === "success"
        ? Check
        : shareStatus === "error"
          ? AlertCircle
          : Share2;
  const isSwitchingImage = pendingImageId !== null;

  return (
    <AnimatePresence>
      {lightbox.open && lightbox.imageSrc && displaySrc && (
        <motion.div
          key="desktop-lightbox"
          ref={containerRef}
          tabIndex={-1}
          role="dialog"
          aria-modal="true"
          aria-labelledby={dialogTitleId}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.12, ease: "linear" }}
          className="fixed inset-0 h-[100dvh] w-screen flex items-center justify-center overflow-hidden overscroll-contain outline-none z-[var(--z-lightbox)]"
          style={{ touchAction: "none", overscrollBehavior: "contain" }}
          onWheel={handleWheel}
          onMouseDown={(e) => {
            // 只在 pointerdown 落在容器本身（即 backdrop 或 pointer-events-none 区域）时记录
            // mousedown 与 mouseup 都必须在非交互区域，避免拖动选择后释放到背景误关
            (e.currentTarget as HTMLDivElement).dataset.downTarget =
              e.target === e.currentTarget ? "backdrop" : "content";
          }}
          onMouseUp={(e) => {
            const wasBackdrop =
              (e.currentTarget as HTMLDivElement).dataset.downTarget === "backdrop";
            (e.currentTarget as HTMLDivElement).dataset.downTarget = "";
            if (wasBackdrop && e.target === e.currentTarget) {
              handleClose();
            }
          }}
        >
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="absolute inset-0 bg-black/90 pointer-events-none"
            transition={{ duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
            aria-hidden
          />

          {/* dialog accessible name：屏幕阅读器朗读 prompt 文本作为窗口标题 */}
          <span id={dialogTitleId} className="sr-only">
            {lightbox.imageAlt
              ? `图片预览：${lightbox.imageAlt}`
              : "图片预览"}
          </span>

          {/* 隐藏下载触发器 */}
          <a ref={downloadAnchorRef} className="hidden" aria-hidden="true" />

          {/* 顶栏：左侧观看控制 + 中间索引 + 右侧动作 */}
          <motion.div
            initial={{ y: -12, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: -12, opacity: 0 }}
            transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1], delay: 0.05 }}
            style={{
              paddingTop: "max(1.25rem, env(safe-area-inset-top))",
              paddingLeft: "max(1.25rem, env(safe-area-inset-left))",
              paddingRight: "max(1.25rem, env(safe-area-inset-right))",
            }}
            className={cn(
              "absolute top-0 left-0 right-0 pb-3 md:pb-4",
              "grid grid-cols-[1fr_auto_1fr] items-start gap-3 pointer-events-none",
            )}
          >
            <div className="flex min-w-0 items-center gap-2 pointer-events-auto">
              <div
                className={cn(
                  "flex min-h-11 items-center gap-1 rounded-full",
                  "border border-white/10 bg-black/35 p-1 backdrop-blur-xl",
                  "shadow-[0_16px_40px_rgba(0,0,0,0.24)]",
                )}
              >
                <ToolIconButton
                  onClick={() => setZoom((zoom) => zoom - ZOOM_STEP)}
                  title="缩小（-）"
                  icon={ZoomOut}
                  disabled={activeZoom <= MIN_ZOOM}
                />
                <button
                  type="button"
                  onClick={resetView}
                  title="重置为适应窗口（0）"
                  aria-label="重置为适应窗口（0）"
                  className={cn(
                    "h-9 min-w-16 rounded-full px-3 text-xs font-mono tabular-nums",
                    "text-white/82 hover:bg-white/10 hover:text-white",
                    "transition-colors duration-150 cursor-pointer",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
                  )}
                >
                  {formatZoom(activeZoom)}
                </button>
                <ToolIconButton
                  onClick={() => setZoom((zoom) => zoom + ZOOM_STEP)}
                  title="放大（+）"
                  icon={ZoomIn}
                  disabled={activeZoom >= MAX_ZOOM}
                />
              </div>
            </div>

            {gallery.length > 0 && currentIdx >= 0 && (
              <div
                className={cn(
                  "pointer-events-auto place-self-start px-3.5 py-2 rounded-full",
                  "bg-black/35 border border-white/10 text-white/82",
                  "text-xs font-mono tabular-nums backdrop-blur-xl",
                  "shadow-[0_16px_40px_rgba(0,0,0,0.22)]",
                )}
              >
                {currentIdx + 1} / {gallery.length}
              </div>
            )}

            <div className="flex flex-wrap justify-end gap-2 pointer-events-auto">
              <div
                className={cn(
                  "flex min-h-11 items-center gap-1 rounded-full",
                  "border border-white/10 bg-black/35 p-1 backdrop-blur-xl",
                  "shadow-[0_16px_40px_rgba(0,0,0,0.24)]",
                )}
              >
                <TopButton
                  onClick={handleIterate}
                  title="迭代（E）"
                  icon={Edit2}
                  disabled={!imageActionsAvailable}
                >
                  迭代
                </TopButton>
                <TopButton
                  onClick={handleUpscale}
                  title="放大到4K"
                  icon={ArrowUpRight}
                  disabled={!imageActionsAvailable}
                >
                  放大
                </TopButton>
                <TopButton
                  onClick={handleReroll}
                  title="重新生成"
                  icon={RefreshCw}
                  disabled={!imageActionsAvailable}
                >
                  重画
                </TopButton>
                <TopButton
                  onClick={handleDownload}
                  title={downloadTitle}
                  icon={DownloadIcon}
                  disabled={downloadStatus === "downloading"}
                  iconClassName={
                    downloadStatus === "downloading" ? "animate-spin" : undefined
                  }
                >
                  {downloadText}
                </TopButton>
                <TopButton
                  onClick={handleShare}
                  title={shareTitle}
                  icon={ShareIcon}
                  disabled={!lightbox.imageId || shareStatus === "creating"}
                  iconClassName={
                    shareStatus === "creating" ? "animate-spin" : undefined
                  }
                >
                  {shareText}
                </TopButton>
              </div>

              <ToolIconButton
                onClick={() => setDetailsOpen((open) => !open)}
                title="图片信息（I）"
                icon={Info}
                active={detailsOpen}
              />
              <button
                ref={closeButtonRef}
                type="button"
                onClick={handleClose}
                aria-label="关闭（Esc）"
                title="关闭（Esc）"
                className={cn(
                  "pointer-events-auto inline-flex items-center justify-center",
                  "w-11 h-11 rounded-full border border-white/15",
                  "bg-black/35 text-white backdrop-blur-xl hover:bg-white/15 hover:border-white/25",
                  "active:scale-[0.94] transition-all duration-150 cursor-pointer",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
                )}
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </motion.div>

          {/* 左右翻页 chevron */}
          {gallery.length > 1 && (
            <>
              <SideChevron
                side="left"
                disabled={!hasPrev || isSwitchingImage}
                onClick={() => gotoDelta(-1)}
              />
              <SideChevron
                side="right"
                disabled={!hasNext || isSwitchingImage}
                onClick={() => gotoDelta(1)}
              />
            </>
          )}

          {/* 图片本体 */}
          <motion.div
            ref={imageWrapRef}
            className={cn(
              "relative z-10 w-full h-full px-4 sm:px-6 md:px-10 py-20",
              "flex items-center justify-center pointer-events-none",
              "transition-[padding] duration-300 ease-[var(--ease-shutter)]",
              detailsOpen && "md:pr-[23rem] lg:pr-[27rem]",
            )}
          >
            {activeLoadError ? (
              <div className="pointer-events-auto rounded-2xl border border-white/10 bg-black/50 backdrop-blur px-8 py-10 text-center max-w-md">
                <p className="text-base text-white/90">图片加载失败</p>
                <p className="text-xs text-white/50 mt-2">
                  数据可能已过期或网络异常，可关闭后重试。
                </p>
              </div>
            ) : (
              <>
                {posterSrc && posterSrc !== displaySrc && !mainImageLoaded && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={posterSrc}
                    alt=""
                    aria-hidden
                    draggable={false}
                    className={cn(
                      "pointer-events-none absolute max-h-[calc(100%-8rem)] max-w-[calc(100%-4rem)]",
                      "select-none rounded-md object-contain opacity-45 blur-md saturate-110",
                    )}
                  />
                )}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  ref={imageRef}
                  key={`${lightbox.imageId}:${displaySrc}`}
                  src={displaySrc}
                  alt={lightbox.imageAlt || ""}
                  loading="eager"
                  decoding="async"
                  fetchPriority="high"
                  onLoad={() => {
                    setMainImageLoaded(true);
                    updateImageState((state) => ({
                      ...state,
                      loadError: false,
                      panOffset: clampPanForCurrentView(
                        state.panOffset,
                        state.zoom,
                        state.viewMode,
                      ),
                    }));
                  }}
                  onError={() => {
                    if (displaySrc !== lightbox.imageSrc && lightbox.imageSrc) {
                      updateImageState((state) => ({ ...state, displayFailed: true }));
                      return;
                    }
                    updateImageState((state) => ({ ...state, loadError: true }));
                  }}
                  style={{
                    transform: `translate3d(${activePanOffset.x}px, ${activePanOffset.y}px, 0) scale(${activeZoom})`,
                    willChange:
                      isPanning || activeZoom > 1 || activeViewMode !== "fit"
                        ? "transform"
                        : "auto",
                    backfaceVisibility: "hidden",
                    cursor:
                      activeZoom > 1 || activeViewMode !== "fit"
                        ? isPanning
                          ? "grabbing"
                          : "grab"
                        : "zoom-in",
                    transition: isPanning ? "none" : "transform 0.2s ease-out",
                    touchAction: "none",
                    overscrollBehavior: "contain",
                  }}
                  onPointerDown={handleImagePointerDown}
                  onPointerMove={handleImagePointerMove}
                  onPointerUp={handleImagePointerEnd}
                  onPointerCancel={handleImagePointerEnd}
                  onDoubleClick={(e) => {
                    e.stopPropagation();
                    if (activeZoom > 1 || activeViewMode !== "fit") {
                      updateImageState((state) => ({
                        ...state,
                        viewMode: "fit",
                        zoom: 1,
                        panOffset: RESET_PAN_OFFSET,
                      }));
                    } else {
                      const rect = e.currentTarget.getBoundingClientRect();
                      const dx = e.clientX - (rect.left + rect.width / 2);
                      const dy = e.clientY - (rect.top + rect.height / 2);
                      const targetZoom = 2;
                      updateImageState((state) => ({
                        ...state,
                        viewMode: "actual",
                        zoom: targetZoom,
                        panOffset: clampPanForCurrentView(
                          { x: -dx * (targetZoom - 1), y: -dy * (targetZoom - 1) },
                          targetZoom,
                          "actual",
                        ),
                      }));
                    }
                  }}
                  className={cn(
                    "rounded-md shadow-2xl",
                    activeViewMode === "fill"
                      ? "h-full w-full max-w-none max-h-none object-cover"
                      : activeViewMode === "actual"
                        ? "max-w-none max-h-none object-contain"
                        : "max-w-full max-h-full object-contain",
                    "pointer-events-auto select-none transform-gpu",
                    edgeHint && "animate-[lb-shake_0.35s_ease-in-out]",
                  )}
                  draggable={false}
                />
              </>
            )}
          </motion.div>

          <AnimatePresence>
            {detailsOpen && (
              <motion.aside
                key="desktop-lightbox-details"
                initial={{ opacity: 0, x: 24 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 24 }}
                transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
                style={{
                  top: "max(5.5rem, calc(env(safe-area-inset-top) + 5rem))",
                  right: "max(1.25rem, env(safe-area-inset-right))",
                  bottom: "max(6.5rem, calc(env(safe-area-inset-bottom) + 5.5rem))",
                }}
                className={cn(
                  "absolute z-30 flex w-[min(22rem,calc(100vw-2.5rem))] flex-col overflow-hidden",
                  "rounded-2xl border border-white/12 bg-black/48 text-white",
                  "backdrop-blur-2xl shadow-[0_28px_80px_rgba(0,0,0,0.45)]",
                  "pointer-events-auto",
                )}
              >
                <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
                  <div>
                    <p className="text-sm font-medium text-white/90">图片信息</p>
                    <p className="mt-0.5 text-[11px] text-white/45">
                      {sourceLabel} · {activeViewModeLabel} · {formatZoom(activeZoom)}
                    </p>
                  </div>
                  <ToolIconButton
                    onClick={() => setDetailsOpen(false)}
                    title="收起信息"
                    icon={X}
                    className="h-9 w-9"
                  />
                </div>

                <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4 scrollbar-thin">
                  {lightbox.imageAlt && (
                    <section>
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <h3 className="text-[11px] font-mono uppercase tracking-wide text-white/45">
                          prompt
                        </h3>
                        <button
                          type="button"
                          onClick={handleCopyPrompt}
                          className={cn(
                            "inline-flex h-8 items-center gap-1.5 rounded-full px-2.5",
                            "border border-white/10 bg-white/5 text-[11px] text-white/72",
                            "hover:border-white/25 hover:bg-white/10 hover:text-white",
                            "transition-colors duration-150 cursor-pointer",
                            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
                          )}
                        >
                          {promptCopied ? (
                            <Check className="h-3.5 w-3.5" />
                          ) : (
                            <Copy className="h-3.5 w-3.5" />
                          )}
                          {promptCopied ? "已复制" : "复制"}
                        </button>
                      </div>
                      <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-white/84">
                        {lightbox.imageAlt}
                      </p>
                    </section>
                  )}

                  <section className="space-y-2">
                    <h3 className="text-[11px] font-mono uppercase tracking-wide text-white/45">
                      元数据
                    </h3>
                    <div className="rounded-xl border border-white/10 bg-white/[0.04] p-3">
                      <DetailRow label="编号" value={lightbox.imageId ?? "-"} />
                      <DetailRow
                        label="尺寸"
                        value={
                          currentImageMeta?.size_actual ??
                          (currentImageMeta?.width && currentImageMeta.height
                            ? `${currentImageMeta.width} x ${currentImageMeta.height}`
                            : "-")
                        }
                      />
                      <DetailRow label="MIME" value={currentImageMeta?.mime ?? "-"} />
                      <DetailRow label="渲染" value={currentImageMeta?.quality ?? "-"} />
                      <DetailRow
                        label="模式"
                        value={currentImageMeta?.fast === true ? "快速" : currentImageMeta?.fast === false ? "标准" : "-"}
                      />
                      <DetailRow label="时间" value={formattedDate ?? "-"} />
                    </div>
                  </section>

                  <section className="space-y-2">
                    <h3 className="text-[11px] font-mono uppercase tracking-wide text-white/45">
                      快捷键
                    </h3>
                    <div className="grid grid-cols-2 gap-2 text-[11px] text-white/58">
                      <Shortcut label="上一张" value="K / ←" />
                      <Shortcut label="下一张" value="J / →" />
                      <Shortcut label="缩放" value="+ / -" />
                      <Shortcut label="适应" value="0" />
                      <Shortcut label="模式" value="1 / 2 / 3" />
                      <Shortcut label="下载" value="D" />
                      <Shortcut label="信息" value="I" />
                    </div>
                  </section>
                </div>

                <div className="grid grid-cols-2 gap-2 border-t border-white/10 p-3">
                  <button
                    type="button"
                    onClick={handleOpenOriginal}
                    className={cn(
                      "inline-flex h-10 items-center justify-center gap-2 rounded-xl",
                      "border border-white/10 bg-white/5 text-sm text-white/80",
                      "hover:border-white/25 hover:bg-white/10 hover:text-white",
                      "transition-colors duration-150 cursor-pointer",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
                    )}
                  >
                    <ExternalLink className="h-4 w-4" />
                    打开
                  </button>
                  <button
                    type="button"
                    onClick={handleDownload}
                    disabled={downloadStatus === "downloading"}
                    className={cn(
                      "inline-flex h-10 items-center justify-center gap-2 rounded-xl",
                      "border border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/14",
                      "text-sm text-[var(--amber-100)]",
                      "hover:bg-[var(--color-lumen-amber)]/22",
                      "disabled:cursor-wait disabled:opacity-70",
                      "transition-colors duration-150 cursor-pointer",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
                    )}
                  >
                    <DownloadIcon
                      className={cn(
                        "h-4 w-4",
                        downloadStatus === "downloading" && "animate-spin",
                      )}
                    />
                    {downloadText}
                  </button>
                </div>
              </motion.aside>
            )}
          </AnimatePresence>

          {/* 底部信息 + 缩略图条 */}
          <motion.div
            initial={{ y: 12, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 12, opacity: 0 }}
            transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1], delay: 0.05 }}
            style={{
              paddingBottom: "max(1.25rem, env(safe-area-inset-bottom))",
              paddingLeft: "max(1.25rem, env(safe-area-inset-left))",
              paddingRight: "max(1.25rem, env(safe-area-inset-right))",
            }}
            className={cn(
              "absolute bottom-0 left-0 right-0 pt-3 md:pt-4",
              "flex flex-col items-center gap-3 pointer-events-none",
            )}
          >
            {/* 边界提示 */}
            <AnimatePresence>
              {isSwitchingImage && (
                <motion.div
                  key="switching-image"
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: 6 }}
                  className={cn(
                    "pointer-events-auto px-3 py-1 rounded-full text-[11px]",
                    "bg-black/60 border border-white/15 text-white/80 backdrop-blur-md",
                  )}
                  role="status"
                >
                  正在载入下一张
                </motion.div>
              )}
              {edgeHint && (
                <motion.div
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: 6 }}
                  className={cn(
                    "pointer-events-auto px-3 py-1 rounded-full text-[11px]",
                    "bg-black/60 border border-white/15 text-white/80 backdrop-blur-md",
                  )}
                  role="status"
                >
                  {edgeHint === "first" ? "已是第一张" : "已是最后一张"}
                </motion.div>
              )}
            </AnimatePresence>

            {/* 缩略图条（多图时才显示） */}
            {gallery.length > 1 && (
              <div
                className={cn(
                  "pointer-events-auto flex items-center gap-1.5 px-2 py-1.5",
                  "max-w-[min(720px,90vw)] overflow-x-auto",
                  "bg-black/50 border border-white/10 rounded-xl backdrop-blur-md",
                )}
              >
                {thumbGalleryItems.map(({ entry: g, index: idx }) => {
                  const img = g.image!;
                  const isActive = img.id === lightbox.imageId;
                  return (
                    <button
                      key={img.id}
                      type="button"
                      onClick={() => {
                        setSlideDir(idx > currentIdx ? 1 : -1);
                        switchToGalleryItem(g, idx > currentIdx ? 1 : -1);
                      }}
                      aria-label={`第 ${idx + 1} 张`}
                      aria-current={isActive}
                      className={cn(
                        "relative shrink-0 w-12 h-12 rounded-lg overflow-hidden",
                        "border transition-all duration-150",
                        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
                        isActive
                          ? "border-[var(--color-lumen-amber)] ring-1 ring-[var(--color-lumen-amber)]/60"
                          : "border-white/10 hover:border-white/40 opacity-70 hover:opacity-100",
                      )}
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={img.thumb_url ?? img.preview_url ?? img.data_url}
                        alt=""
                        loading="lazy"
                        decoding="async"
                        fetchPriority="low"
                        className="w-full h-full object-cover"
                        draggable={false}
                      />
                    </button>
                  );
                })}
              </div>
            )}

          </motion.div>

          {/* 内嵌抖动 keyframes（唯一命名，避免依赖 styled-jsx）。 */}
          <style>{`
            @keyframes lb-shake {
              0%, 100% { transform: translateX(0); }
              25% { transform: translateX(-6px); }
              50% { transform: translateX(6px); }
              75% { transform: translateX(-3px); }
            }
          `}</style>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

// ——————————————————— 子原语 ———————————————————
function ToolIconButton({
  onClick,
  title,
  icon: Icon,
  disabled = false,
  active = false,
  className,
}: {
  onClick: () => void;
  title: string;
  icon: LucideIcon;
  disabled?: boolean;
  active?: boolean;
  className?: string;
}) {
  const button = (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={title}
      title={title}
      className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-full",
        "border border-transparent text-white/68",
        "hover:border-white/15 hover:bg-white/10 hover:text-white",
        "disabled:cursor-not-allowed disabled:opacity-35 disabled:hover:border-transparent disabled:hover:bg-transparent",
        "active:scale-[0.94] transition-all duration-150 cursor-pointer",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
        active &&
          "border-[var(--color-lumen-amber)]/45 bg-[var(--color-lumen-amber)]/16 text-[var(--amber-100)]",
        className,
      )}
    >
      <Icon className="h-4 w-4" aria-hidden />
    </button>
  );

  return (
    <Tooltip content={title} side="bottom" enabled={!disabled}>
      {button}
    </Tooltip>
  );
}

function TopButton({
  onClick,
  title,
  icon: Icon,
  disabled = false,
  iconClassName,
  children,
}: {
  onClick: () => void;
  title: string;
  icon: LucideIcon;
  disabled?: boolean;
  iconClassName?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className={cn(
        "inline-flex h-9 items-center gap-1.5 rounded-full px-3 text-sm",
        "border border-transparent text-white/82",
        "hover:border-white/15 hover:bg-white/10 hover:text-white",
        "disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:border-transparent disabled:hover:bg-transparent",
        "active:scale-[0.97] transition-all duration-150 cursor-pointer",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
      )}
    >
      <Icon className={cn("w-4 h-4", iconClassName)} aria-hidden />
      <span>{children}</span>
    </button>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[4.5rem_1fr] gap-2 py-1.5 text-xs">
      <span className="text-white/42">{label}</span>
      <span className="min-w-0 break-words font-mono text-white/76">{value}</span>
    </div>
  );
}

function Shortcut({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-lg border border-white/8 bg-white/[0.035] px-2 py-1.5">
      <span>{label}</span>
      <span className="font-mono text-white/82">{value}</span>
    </div>
  );
}

function SideChevron({
  side,
  disabled,
  onClick,
}: {
  side: "left" | "right";
  disabled: boolean;
  onClick: () => void;
}) {
  const Icon = side === "left" ? ChevronLeft : ChevronRight;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={side === "left" ? "上一张（K）" : "下一张（J）"}
      title={side === "left" ? "上一张（K）" : "下一张（J）"}
      className={cn(
        "absolute top-1/2 -translate-y-1/2 z-20",
        side === "left" ? "left-2 sm:left-3 md:left-6" : "right-2 sm:right-3 md:right-6",
        "inline-flex items-center justify-center w-11 h-11 rounded-full",
        "bg-white/6 border border-white/10 text-white/70 backdrop-blur-md",
        "hover:bg-white/15 hover:text-white hover:border-white/25",
        "active:scale-[0.94] transition-all duration-150",
        "disabled:opacity-30 disabled:cursor-not-allowed",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-lumen-amber)]/70",
      )}
    >
      <Icon className="w-5 h-5" />
    </button>
  );
}
