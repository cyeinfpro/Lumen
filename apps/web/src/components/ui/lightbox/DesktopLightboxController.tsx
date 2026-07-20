"use client";

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { copyTextToClipboard } from "@/lib/clipboard";
import { useCreateShareMutation } from "@/lib/queries";
import { useChatStore } from "@/store/useChatStore";
import { useInpaintStore } from "@/store/useInpaintStore";
import { useUiStore } from "@/store/useUiStore";

import {
  DesktopLightboxView,
  type DesktopLightboxViewProps,
} from "./DesktopLightboxView";
import { useDesktopLightboxKeyboard } from "./desktopLightboxKeyboard";
import {
  downloadDesktopImage,
  shareDesktopImage,
} from "./desktopLightboxMediaActions";
import {
  CLICK_MAX_DURATION_MS,
  CLICK_TAP_SLOP,
  CLICK_ZOOM,
  EMPTY_DESKTOP_GALLERY,
  EMPTY_GENERATIONS,
  RESET_PAN_OFFSET,
  ZOOM_STEP,
  buildCurrentLightboxItem,
  clampPanOffset,
  clampZoom,
  createImageState,
  currentGalleryIndex,
  desktopActionPresentation,
  desktopThumbnailItems,
  findCurrentImageMeta,
  labelForViewMode,
  preloadImage,
  resolveImagePresentation,
  resolvePanBoundsInput,
  toDesktopGalleryItem,
  type DesktopGalleryItem,
  type DesktopImageMeta,
  type DownloadStatus,
  type ImagePointerState,
  type ImageTransientState,
  type MousePanState,
  type PanOffset,
  type ShareStatus,
  type TouchActions,
  type ViewMode,
} from "./desktopLightboxModel";
import { useDesktopLightboxTouch } from "./desktopLightboxTouch";
import {
  CLOSE_EVENT,
  OPEN_EVENT,
  type LightboxItem,
  type OpenLightboxDetail,
} from "./types";

async function writeClipboardText(text: string): Promise<void> {
  await copyTextToClipboard(text);
}

function posterSource(meta: DesktopImageMeta | null): string | null {
  if (!meta) return null;
  return meta.thumb_url ?? meta.preview_url ?? null;
}

function releaseImagePointer(
  element: HTMLImageElement,
  pointerId: number,
): void {
  try {
    element.releasePointerCapture(pointerId);
  } catch {
    // The pointer may already be released by the browser.
  }
}

export function DesktopLightbox() {
  const lightbox = useUiStore((state) => state.lightbox);
  const openLightbox = useUiStore((state) => state.openLightbox);
  const openLightboxFromItems = useUiStore(
    (state) => state.openLightboxFromItems,
  );
  const closeLightbox = useUiStore((state) => state.closeLightbox);
  const createShareMutation = useCreateShareMutation();
  const storeEventItems = lightbox.eventItems;
  const imageActionsAvailable = useChatStore((state) =>
    lightbox.imageId
      ? Boolean(state.imagesById[lightbox.imageId])
      : false,
  );

  const [eventGallery, setEventGallery] = useState<
    DesktopGalleryItem[] | null
  >(null);
  const [eventItems, setEventItems] = useState<LightboxItem[] | null>(
    null,
  );
  const imageStateKey = `${lightbox.imageId ?? ""}\n${lightbox.imageSrc ?? ""}\n${lightbox.imagePreviewSrc ?? ""}`;
  const activeImageStateKeyRef = useRef(imageStateKey);
  useLayoutEffect(() => {
    activeImageStateKeyRef.current = imageStateKey;
  }, [imageStateKey]);

  const [imageState, setImageState] = useState(() =>
    createImageState(imageStateKey),
  );
  const { activeImageState, displaySrc, sourceLabel } =
    resolveImagePresentation(
      imageState,
      imageStateKey,
      lightbox.imageSrc,
      lightbox.imagePreviewSrc,
    );
  const activeLoadError = activeImageState.loadError;
  const activeViewMode = activeImageState.viewMode;
  const activeZoom = activeImageState.zoom;
  const activePanOffset = activeImageState.panOffset;
  const updateImageState = useCallback(
    (
      recipe: (
        state: ImageTransientState,
      ) => ImageTransientState,
    ) => {
      setImageState((previous) => {
        if (activeImageStateKeyRef.current !== imageStateKey) {
          return previous;
        }
        const current =
          previous.key === imageStateKey
            ? previous
            : createImageState(imageStateKey);
        return recipe(current);
      });
    },
    [imageStateKey],
  );

  const [edgeHint, setEdgeHint] = useState<
    "first" | "last" | null
  >(null);
  const [, setSlideDir] = useState<1 | -1>(1);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [downloadStatus, setDownloadStatus] =
    useState<DownloadStatus>("idle");
  const [shareStatus, setShareStatus] =
    useState<ShareStatus>("idle");
  const [pendingImageId, setPendingImageId] = useState<string | null>(
    null,
  );
  const [mousePan, setMousePan] = useState<MousePanState | null>(null);
  const [mainImageLoaded, setMainImageLoaded] = useState(false);

  const imagePointerRef = useRef<ImagePointerState | null>(null);
  const switchSeqRef = useRef(0);
  const downloadSeqRef = useRef(0);
  const shareSeqRef = useRef(0);
  const preloadAbortRef = useRef<AbortController | null>(null);
  const downloadAnchorRef = useRef<HTMLAnchorElement>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const imageWrapRef = useRef<HTMLDivElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  const dialogTitleId = useId();
  const containerElementId = `${dialogTitleId}-container`;
  const downloadAnchorElementId = `${dialogTitleId}-download`;
  const imageWrapElementId = `${dialogTitleId}-image-wrap`;
  const imageElementId = `${dialogTitleId}-image`;
  const closeButtonElementId = `${dialogTitleId}-close`;

  useLayoutEffect(() => {
    if (!lightbox.open) {
      containerRef.current = null;
      downloadAnchorRef.current = null;
      imageWrapRef.current = null;
      imageRef.current = null;
      closeButtonRef.current = null;
      return;
    }
    containerRef.current = document.getElementById(
      containerElementId,
    ) as HTMLDivElement | null;
    downloadAnchorRef.current = document.getElementById(
      downloadAnchorElementId,
    ) as HTMLAnchorElement | null;
    imageWrapRef.current = document.getElementById(
      imageWrapElementId,
    ) as HTMLDivElement | null;
    imageRef.current = document.getElementById(
      imageElementId,
    ) as HTMLImageElement | null;
    closeButtonRef.current = document.getElementById(
      closeButtonElementId,
    ) as HTMLButtonElement | null;
  }, [
    closeButtonElementId,
    containerElementId,
    displaySrc,
    downloadAnchorElementId,
    imageElementId,
    imageWrapElementId,
    lightbox.open,
  ]);

  const generations = useChatStore((state) =>
    lightbox.open ? state.generations : EMPTY_GENERATIONS,
  );
  const chatGallery = useMemo<DesktopGalleryItem[]>(() => {
    if (!lightbox.open) return EMPTY_DESKTOP_GALLERY;
    const items = Object.values(generations).filter(
      (generation) =>
        generation.status === "succeeded" && generation.image,
    );
    items.sort((left, right) => left.started_at - right.started_at);
    return items.map((generation) => ({
      image: generation.image!,
      prompt: generation.prompt,
      started_at: generation.started_at,
    }));
  }, [generations, lightbox.open]);
  const gallery = useMemo(() => {
    if (
      storeEventItems?.some(
        (entry) => entry.id === lightbox.imageId,
      )
    ) {
      return storeEventItems.map(toDesktopGalleryItem);
    }
    if (
      eventGallery?.some(
        (entry) => entry.image.id === lightbox.imageId,
      )
    ) {
      return eventGallery;
    }
    return chatGallery;
  }, [
    chatGallery,
    eventGallery,
    lightbox.imageId,
    storeEventItems,
  ]);

  const hideActiveImageLayer = useCallback(() => {
    const wrap = imageWrapRef.current;
    if (wrap) {
      wrap.style.transition = "opacity 80ms linear";
      wrap.style.opacity = "0";
    }
    const image = imageRef.current;
    if (!image) return;
    image.style.transition = "opacity 80ms linear";
    image.style.opacity = "0";
    image.style.visibility = "hidden";
  }, []);

  const handleClose = useCallback(() => {
    hideActiveImageLayer();
    switchSeqRef.current += 1;
    downloadSeqRef.current += 1;
    shareSeqRef.current += 1;
    preloadAbortRef.current?.abort();
    preloadAbortRef.current = null;
    imagePointerRef.current = null;
    setEventGallery(null);
    setEventItems(null);
    setDetailsOpen(false);
    setPendingImageId(null);
    setMousePan(null);
    closeLightbox();
  }, [closeLightbox, hideActiveImageLayer]);

  useEffect(() => {
    const onOpen = (event: Event) => {
      const detail = (
        event as CustomEvent<OpenLightboxDetail>
      ).detail;
      if (!detail?.items?.length) return;
      const nextGallery = detail.items.map(toDesktopGalleryItem);
      const target =
        nextGallery.find(
          (entry) => entry.image.id === detail.initialId,
        ) ?? nextGallery[0];
      if (!target) return;

      switchSeqRef.current += 1;
      preloadAbortRef.current?.abort();
      preloadAbortRef.current = null;
      setEventGallery(nextGallery);
      setEventItems(detail.items);
      setSlideDir(1);
      setEdgeHint(null);
      setPendingImageId(null);
      if (detail.source === "store") return;
      openLightbox(
        target.image.id,
        target.image.data_url,
        target.prompt,
        target.image.preview_url ?? target.image.thumb_url,
      );
    };
    const onClose = () => handleClose();
    window.addEventListener(OPEN_EVENT, onOpen as EventListener);
    window.addEventListener(CLOSE_EVENT, onClose);
    return () => {
      window.removeEventListener(
        OPEN_EVENT,
        onOpen as EventListener,
      );
      window.removeEventListener(CLOSE_EVENT, onClose);
    };
  }, [handleClose, openLightbox]);

  const currentImageMeta = useMemo(
    () => findCurrentImageMeta(gallery, lightbox.imageId),
    [gallery, lightbox.imageId],
  );
  const currentLightboxItem = useMemo(
    () =>
      buildCurrentLightboxItem(
        lightbox,
        currentImageMeta,
        storeEventItems,
        eventItems,
      ),
    [currentImageMeta, eventItems, lightbox, storeEventItems],
  );
  const currentIndex = useMemo(
    () => currentGalleryIndex(gallery, lightbox.imageId),
    [gallery, lightbox.imageId],
  );

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
  }, [activePanOffset, activeViewMode, activeZoom]);

  const getPanBoundsInput = useCallback(() => {
    return resolvePanBoundsInput(
      imageWrapRef.current?.getBoundingClientRect(),
      imageRef.current,
      currentImageMeta,
    );
  }, [currentImageMeta]);
  const clampPanForCurrentView = useCallback(
    (offset: PanOffset, zoom: number, viewMode: ViewMode) => {
      const { viewport, imageSize } = getPanBoundsInput();
      return clampPanOffset(
        offset,
        zoom,
        viewMode,
        viewport,
        imageSize,
      );
    },
    [getPanBoundsInput],
  );

  const handleDownload = useCallback(() => {
    const src = lightbox.imageSrc;
    if (!src || downloadStatus === "downloading") return;
    const operationKey = imageStateKey;
    const operationSeq = downloadSeqRef.current + 1;
    downloadSeqRef.current = operationSeq;
    const operationIsCurrent = () =>
      activeImageStateKeyRef.current === operationKey &&
      downloadSeqRef.current === operationSeq;
    setDownloadStatus("downloading");

    void (async () => {
      const anchor = downloadAnchorRef.current;
      if (!anchor) {
        if (operationIsCurrent()) setDownloadStatus("idle");
        return;
      }
      const status = await downloadDesktopImage({
        src,
        id: lightbox.imageId,
        mime: currentImageMeta?.mime,
        filename: currentImageMeta?.filename,
        anchor,
        operationIsCurrent,
      });
      if (!status || !operationIsCurrent()) return;
      setDownloadStatus(status);
      const delay = status === "success" ? 1400 : 1800;
      window.setTimeout(() => {
        if (operationIsCurrent()) setDownloadStatus("idle");
      }, delay);
    })();
  }, [
    currentImageMeta,
    downloadStatus,
    imageStateKey,
    lightbox.imageId,
    lightbox.imageSrc,
  ]);

  const handleIterate = useCallback(() => {
    const imageId = lightbox.imageId;
    if (!imageId) return;
    const image = useChatStore.getState().imagesById[imageId];
    if (!image) return;
    handleClose();
    useChatStore.getState().promoteImageToReference(imageId);
  }, [handleClose, lightbox.imageId]);

  const handleUpscale = useCallback(() => {
    const imageId = lightbox.imageId;
    if (!imageId) return;
    handleClose();
    void useChatStore.getState().upscaleImage(imageId);
  }, [handleClose, lightbox.imageId]);

  const handleReroll = useCallback(() => {
    const imageId = lightbox.imageId;
    if (!imageId) return;
    handleClose();
    void useChatStore.getState().rerollImage(imageId);
  }, [handleClose, lightbox.imageId]);

  const handleInpaint = useCallback(() => {
    const imageId = lightbox.imageId;
    if (!imageId) return;
    const image = useChatStore.getState().imagesById[imageId];
    if (!image) return;
    handleClose();
    useInpaintStore.getState().openInpaint({
      imageId: image.id,
      src: image.data_url,
      alt: lightbox.imageAlt ?? "图片",
      width: image.width,
      height: image.height,
    });
  }, [handleClose, lightbox.imageAlt, lightbox.imageId]);

  const setZoom = useCallback(
    (nextValue: number | ((current: number) => number)) => {
      updateImageState((state) => {
        const requested =
          typeof nextValue === "function"
            ? nextValue(state.zoom)
            : nextValue;
        const zoom = clampZoom(requested);
        const viewMode =
          zoom > 1 && state.viewMode === "fit"
            ? "actual"
            : state.viewMode;
        const panOffset =
          zoom <= 1 && viewMode === "fit"
            ? RESET_PAN_OFFSET
            : state.panOffset;
        return {
          ...state,
          zoom,
          viewMode,
          panOffset: clampPanForCurrentView(
            panOffset,
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

  const zoomToPointer = useCallback(
    (clientX: number, clientY: number, nextZoom: number) => {
      const rect = imageRef.current?.getBoundingClientRect();
      updateImageState((state) => {
        const zoom = clampZoom(nextZoom);
        const centerX =
          clientX -
          (rect?.left ?? 0) -
          (rect?.width ?? 0) / 2;
        const centerY =
          clientY -
          (rect?.top ?? 0) -
          (rect?.height ?? 0) / 2;
        const ratio = zoom / state.zoom;
        const panOffset = {
          x:
            centerX * (1 - ratio) +
            state.panOffset.x * ratio,
          y:
            centerY * (1 - ratio) +
            state.panOffset.y * ratio,
        };
        return {
          ...state,
          viewMode: "fit",
          zoom,
          panOffset: clampPanForCurrentView(
            panOffset,
            zoom,
            "fit",
          ),
        };
      });
    },
    [clampPanForCurrentView, updateImageState],
  );

  const handleOpenOriginal = useCallback(() => {
    if (!lightbox.imageSrc) return;
    window.open(
      lightbox.imageSrc,
      "_blank",
      "noopener,noreferrer",
    );
  }, [lightbox.imageSrc]);

  const resetShareStatusSoon = useCallback(
    (operationKey: string, operationSeq: number) => {
      window.setTimeout(() => {
        if (
          activeImageStateKeyRef.current === operationKey &&
          shareSeqRef.current === operationSeq
        ) {
          setShareStatus("idle");
        }
      }, 1600);
    },
    [],
  );

  const handleShare = useCallback(() => {
    const imageId = lightbox.imageId;
    if (!imageId || shareStatus === "creating") return;
    const operationKey = imageStateKey;
    const operationSeq = shareSeqRef.current + 1;
    shareSeqRef.current = operationSeq;
    const operationIsCurrent = () =>
      activeImageStateKeyRef.current === operationKey &&
      shareSeqRef.current === operationSeq;
    setShareStatus("creating");

    void (async () => {
      const status = await shareDesktopImage({
        imageId,
        createShare: createShareMutation.mutateAsync,
        writeClipboard: writeClipboardText,
        operationIsCurrent,
      });
      if (!status || !operationIsCurrent()) return;
      setShareStatus(status);
      if (status !== "idle") {
        resetShareStatusSoon(operationKey, operationSeq);
      }
    })();
  }, [
    createShareMutation.mutateAsync,
    imageStateKey,
    lightbox.imageId,
    resetShareStatusSoon,
    shareStatus,
  ]);

  const showEdgeHint = useCallback(
    (edge: "first" | "last") => {
      setEdgeHint(edge);
      window.setTimeout(() => setEdgeHint(null), 1200);
    },
    [],
  );

  const switchToGalleryItem = useCallback(
    (target: DesktopGalleryItem, direction: 1 | -1) => {
      const sourceImageKey = imageStateKey;
      const sequence = switchSeqRef.current + 1;
      switchSeqRef.current = sequence;
      preloadAbortRef.current?.abort();
      const preloadAbort = new AbortController();
      preloadAbortRef.current = preloadAbort;
      setPendingImageId(target.image.id);
      setEdgeHint(null);

      void (async () => {
        try {
          await preloadImage(
            target.image.preview_url ??
              target.image.thumb_url ??
              target.image.data_url,
            preloadAbort.signal,
          );
        } catch {
          if (preloadAbort.signal.aborted) return;
          if (
            activeImageStateKeyRef.current !== sourceImageKey
          ) {
            return;
          }
          try {
            await preloadImage(
              target.image.data_url,
              preloadAbort.signal,
            );
          } catch {
            if (preloadAbort.signal.aborted) return;
          }
        }
        if (switchSeqRef.current !== sequence) return;
        if (activeImageStateKeyRef.current !== sourceImageKey) return;
        if (preloadAbortRef.current === preloadAbort) {
          preloadAbortRef.current = null;
        }
        setSlideDir(direction);
        setPendingImageId(null);
        const items = storeEventItems ?? eventItems;
        if (items) {
          openLightboxFromItems(
            items,
            target.image.id,
            lightbox.action,
          );
          return;
        }
        openLightbox(
          target.image.id,
          target.image.data_url,
          target.prompt,
          target.image.preview_url ?? target.image.thumb_url,
        );
      })();
    },
    [
      eventItems,
      imageStateKey,
      lightbox.action,
      openLightbox,
      openLightboxFromItems,
      storeEventItems,
    ],
  );

  const gotoDelta = useCallback(
    (delta: 1 | -1) => {
      if (gallery.length === 0 || currentIndex < 0) return;
      if (delta === 1 && currentIndex === gallery.length - 1) {
        showEdgeHint("last");
        return;
      }
      if (delta === -1 && currentIndex === 0) {
        showEdgeHint("first");
        return;
      }
      const target = gallery[currentIndex + delta];
      if (!target) return;
      switchToGalleryItem(target, delta);
    },
    [
      currentIndex,
      gallery,
      showEdgeHint,
      switchToGalleryItem,
    ],
  );

  const handleWheel = useCallback(
    (event: React.WheelEvent<HTMLDivElement>) => {
      if (!event.ctrlKey && !event.metaKey && activeZoom <= 1) {
        return;
      }
      event.preventDefault();
      const direction = event.deltaY > 0 ? -1 : 1;
      const nextZoom = clampZoom(
        activeZoom + direction * ZOOM_STEP,
      );
      if (nextZoom === activeZoom) return;

      const wrapRect =
        imageWrapRef.current?.getBoundingClientRect();
      if (!wrapRect) {
        setZoom((zoom) => zoom + direction * ZOOM_STEP);
        return;
      }
      const centerX =
        event.clientX - (wrapRect.left + wrapRect.width / 2);
      const centerY =
        event.clientY - (wrapRect.top + wrapRect.height / 2);
      const ratio = nextZoom / activeZoom;
      const nextPan =
        nextZoom <= 1
          ? RESET_PAN_OFFSET
          : {
              x:
                centerX * (1 - ratio) +
                activePanOffset.x * ratio,
              y:
                centerY * (1 - ratio) +
                activePanOffset.y * ratio,
            };
      const viewMode =
        nextZoom <= 1
          ? "fit"
          : activeViewMode === "fit"
            ? "actual"
            : activeViewMode;
      updateImageState((state) => ({
        ...state,
        zoom: nextZoom,
        viewMode,
        panOffset: clampPanForCurrentView(
          nextPan,
          nextZoom,
          viewMode,
        ),
      }));
    },
    [
      activePanOffset,
      activeViewMode,
      activeZoom,
      clampPanForCurrentView,
      setZoom,
      updateImageState,
    ],
  );

  useEffect(() => {
    touchActionsRef.current = {
      clampPanForCurrentView,
      gotoDelta,
      handleClose,
      updateImageState,
    };
  }, [
    clampPanForCurrentView,
    gotoDelta,
    handleClose,
    updateImageState,
  ]);

  const handleImagePointerDown = useCallback(
    (event: React.PointerEvent<HTMLImageElement>) => {
      if (event.pointerType === "touch" || event.button !== 0) {
        return;
      }
      const canPan =
        activeZoom > 1 || activeViewMode !== "fit";
      imagePointerRef.current = {
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startOffset: panOffsetRef.current,
        canPan,
        moved: false,
        startTime: performance.now(),
      };
      if (canPan) {
        event.preventDefault();
        event.stopPropagation();
        setMousePan({
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          startOffset: panOffsetRef.current,
        });
      } else {
        setMousePan(null);
      }
      try {
        event.currentTarget.setPointerCapture(event.pointerId);
      } catch {
        // Pointer capture is optional on older browsers.
      }
    },
    [activeViewMode, activeZoom],
  );

  const handleImagePointerMove = useCallback(
    (event: React.PointerEvent<HTMLImageElement>) => {
      const gesture = imagePointerRef.current;
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      const dx = event.clientX - gesture.startX;
      const dy = event.clientY - gesture.startY;
      if (
        !gesture.moved &&
        (Math.abs(dx) > CLICK_TAP_SLOP ||
          Math.abs(dy) > CLICK_TAP_SLOP)
      ) {
        gesture.moved = true;
      }
      if (!gesture.canPan) return;
      event.preventDefault();
      const panOffset = clampPanForCurrentView(
        {
          x: gesture.startOffset.x + dx,
          y: gesture.startOffset.y + dy,
        },
        activeZoom,
        activeViewMode,
      );
      updateImageState((state) => ({ ...state, panOffset }));
    },
    [
      activeViewMode,
      activeZoom,
      clampPanForCurrentView,
      updateImageState,
    ],
  );

  const handleImagePointerEnd = useCallback(
    (event: React.PointerEvent<HTMLImageElement>) => {
      const gesture = imagePointerRef.current;
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      imagePointerRef.current = null;
      setMousePan(null);
      releaseImagePointer(event.currentTarget, event.pointerId);
      if (gesture.moved) return;
      if (
        performance.now() - gesture.startTime >
        CLICK_MAX_DURATION_MS
      ) {
        return;
      }
      if (activeZoom > 1 || activeViewMode !== "fit") {
        resetView();
        return;
      }
      zoomToPointer(
        event.clientX,
        event.clientY,
        CLICK_ZOOM,
      );
    },
    [activeViewMode, activeZoom, resetView, zoomToPointer],
  );

  const handleImagePointerCancel = useCallback(
    (event: React.PointerEvent<HTMLImageElement>) => {
      const gesture = imagePointerRef.current;
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      imagePointerRef.current = null;
      setMousePan(null);
      releaseImagePointer(event.currentTarget, event.pointerId);
    },
    [],
  );

  const toggleDetails = useCallback(() => {
    setDetailsOpen((open) => !open);
  }, []);
  const hideDetails = useCallback(() => {
    setDetailsOpen(false);
  }, []);
  const keyboardActions = useMemo(
    () => ({
      close: handleClose,
      download: handleDownload,
      iterate: handleIterate,
      toggleDetails,
      resetView,
      setViewMode,
      setZoom,
      gotoDelta,
    }),
    [
      gotoDelta,
      handleClose,
      handleDownload,
      handleIterate,
      resetView,
      setViewMode,
      setZoom,
      toggleDetails,
    ],
  );
  useDesktopLightboxKeyboard(
    lightbox.open,
    containerRef,
    keyboardActions,
  );

  useEffect(() => {
    if (!lightbox.open) return;
    previouslyFocusedRef.current =
      document.activeElement as HTMLElement | null;
    const frame = requestAnimationFrame(() => {
      const target =
        closeButtonRef.current ?? containerRef.current;
      target?.focus({ preventScroll: true });
    });
    return () => {
      cancelAnimationFrame(frame);
      const previous = previouslyFocusedRef.current;
      if (previous && typeof previous.focus === "function") {
        try {
          previous.focus({ preventScroll: true });
        } catch {
          // The previous element may have unmounted.
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
      setMousePan(null);
      imagePointerRef.current = null;
    });
    return () => {
      canceled = true;
    };
  }, [lightbox.open]);

  useEffect(() => {
    switchSeqRef.current += 1;
    downloadSeqRef.current += 1;
    shareSeqRef.current += 1;
    preloadAbortRef.current?.abort();
    preloadAbortRef.current = null;
    let canceled = false;
    queueMicrotask(() => {
      if (canceled) return;
      setDownloadStatus("idle");
      setShareStatus("idle");
      setPendingImageId(null);
      setEdgeHint(null);
      setMousePan(null);
      imagePointerRef.current = null;
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
    return () =>
      window.removeEventListener("resize", handleResize);
  }, [
    clampPanForCurrentView,
    lightbox.open,
    updateImageState,
  ]);

  useBodyScrollLock(lightbox.open, {
    bodyOverscrollBehavior: "contain",
    documentOverscrollBehavior: "contain",
  });
  useDesktopLightboxTouch({
    open: lightbox.open,
    containerRef,
    zoomRef,
    viewModeRef,
    panOffsetRef,
    actionsRef: touchActionsRef,
  });

  const handleBackdropMouseDown = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      event.currentTarget.dataset.downTarget =
        event.target === event.currentTarget
          ? "backdrop"
          : "content";
    },
    [],
  );
  const handleBackdropMouseUp = useCallback(
    (event: React.MouseEvent<HTMLDivElement>) => {
      const wasBackdrop =
        event.currentTarget.dataset.downTarget === "backdrop";
      event.currentTarget.dataset.downTarget = "";
      if (wasBackdrop && event.target === event.currentTarget) {
        handleClose();
      }
    },
    [handleClose],
  );

  const handleImageLoad = useCallback(() => {
    if (activeImageStateKeyRef.current !== imageStateKey) return;
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
  }, [
    clampPanForCurrentView,
    imageStateKey,
    updateImageState,
  ]);
  const handleImageError = useCallback(() => {
    if (activeImageStateKeyRef.current !== imageStateKey) return;
    if (displaySrc !== lightbox.imageSrc && lightbox.imageSrc) {
      updateImageState((state) => ({
        ...state,
        displayFailed: true,
      }));
      return;
    }
    updateImageState((state) => ({
      ...state,
      loadError: true,
    }));
  }, [
    displaySrc,
    imageStateKey,
    lightbox.imageSrc,
    updateImageState,
  ]);

  const handleSelectThumbnail = useCallback(
    (entry: DesktopGalleryItem, index: number) => {
      const direction = index > currentIndex ? 1 : -1;
      setSlideDir(direction);
      switchToGalleryItem(entry, direction);
    },
    [currentIndex, switchToGalleryItem],
  );

  const handleInjectedAction = useCallback(() => {
    const action = lightbox.action;
    const imageId = lightbox.imageId;
    if (!action || !imageId) return;
    const current = (lightbox.eventItems ?? []).find(
      (item) => item.id === imageId,
    );
    if (current) action.onClick(current);
  }, [lightbox.action, lightbox.eventItems, lightbox.imageId]);

  const actionPresentation = desktopActionPresentation(
    downloadStatus,
    shareStatus,
  );
  const injectedAction =
    lightbox.action && lightbox.imageId
      ? {
          label: lightbox.action.label,
          pending: Boolean(lightbox.action.pending),
          onClick: handleInjectedAction,
        }
      : null;
  const viewProps: DesktopLightboxViewProps = {
    open: lightbox.open,
    imageId: lightbox.imageId,
    imageSrc: lightbox.imageSrc,
    imageAlt: lightbox.imageAlt,
    displaySrc,
    dialogTitleId,
    containerElementId,
    downloadAnchorElementId,
    imageWrapElementId,
    imageElementId,
    closeButtonElementId,
    galleryLength: gallery.length,
    currentIndex,
    hasPrevious: currentIndex > 0,
    hasNext:
      currentIndex >= 0 &&
      currentIndex < gallery.length - 1,
    thumbnails: desktopThumbnailItems(gallery, currentIndex),
    posterSrc: posterSource(currentImageMeta),
    sourceLabel,
    currentItem: currentLightboxItem,
    activeLoadError,
    activeViewMode,
    activeViewModeLabel: labelForViewMode(activeViewMode),
    activeZoom,
    activePanOffset,
    isPanning: mousePan !== null,
    mainImageLoaded,
    detailsOpen,
    imageActionsAvailable,
    downloadStatus,
    downloadTitle: actionPresentation.downloadTitle,
    downloadText: actionPresentation.downloadText,
    shareStatus,
    shareTitle: actionPresentation.shareTitle,
    shareText: actionPresentation.shareText,
    edgeHint,
    isSwitchingImage: pendingImageId !== null,
    injectedAction,
    onWheel: handleWheel,
    onBackdropMouseDown: handleBackdropMouseDown,
    onBackdropMouseUp: handleBackdropMouseUp,
    onClose: handleClose,
    onZoomOut: () => setZoom((zoom) => zoom - ZOOM_STEP),
    onZoomIn: () => setZoom((zoom) => zoom + ZOOM_STEP),
    onResetView: resetView,
    onToggleDetails: toggleDetails,
    onHideDetails: hideDetails,
    onIterate: handleIterate,
    onInpaint: handleInpaint,
    onUpscale: handleUpscale,
    onReroll: handleReroll,
    onDownload: handleDownload,
    onShare: handleShare,
    onOpenOriginal: handleOpenOriginal,
    onPrevious: () => gotoDelta(-1),
    onNext: () => gotoDelta(1),
    onImageLoad: handleImageLoad,
    onImageError: handleImageError,
    onImagePointerDown: handleImagePointerDown,
    onImagePointerMove: handleImagePointerMove,
    onImagePointerUp: handleImagePointerEnd,
    onImagePointerCancel: handleImagePointerCancel,
    onSelectThumbnail: handleSelectThumbnail,
  };

  return <DesktopLightboxView {...viewProps} />;
}
