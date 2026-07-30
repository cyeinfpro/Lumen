"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { useChatStore } from "@/store/useChatStore";
import { useInpaintStore } from "@/store/useInpaintStore";
import { useUiStore } from "@/store/useUiStore";

import {
  DesktopLightboxView,
  type DesktopLightboxViewProps,
} from "./DesktopLightboxView";
import { useDesktopLightboxKeyboard } from "./desktopLightboxKeyboard";
import {
  EMPTY_DESKTOP_GALLERY,
  EMPTY_GENERATIONS,
  ZOOM_STEP,
  buildCurrentLightboxItem,
  createImageState,
  currentGalleryIndex,
  desktopThumbnailItems,
  findCurrentImageMeta,
  labelForViewMode,
  preloadImage,
  resolveImagePresentation,
  toDesktopGalleryItem,
  type DesktopGalleryItem,
  type DesktopImageMeta,
  type ImageTransientState,
} from "./desktopLightboxModel";
import { useDesktopLightboxMediaActions } from "./useDesktopLightboxMediaActions";
import { useDesktopLightboxDialog } from "./useDesktopLightboxDialog";
import { useDesktopLightboxViewport } from "./useDesktopLightboxViewport";
import {
  CLOSE_EVENT,
  OPEN_EVENT,
  type LightboxItem,
  type OpenLightboxDetail,
} from "./types";

function posterSource(meta: DesktopImageMeta | null): string | null {
  if (!meta) return null;
  return meta.thumb_url ?? meta.preview_url ?? null;
}

export function DesktopLightbox() {
  const lightbox = useUiStore((state) => state.lightbox);
  const openLightbox = useUiStore((state) => state.openLightbox);
  const openLightboxFromItems = useUiStore(
    (state) => state.openLightboxFromItems,
  );
  const closeLightbox = useUiStore((state) => state.closeLightbox);
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
  const {
    detailsOpen,
    toggleDetails,
    hideDetails,
    hideActiveImageLayer,
    downloadAnchorRef,
    containerRef,
    imageWrapRef,
    imageRef,
    dialogTitleId,
    containerElementId,
    downloadAnchorElementId,
    imageWrapElementId,
    imageElementId,
    closeButtonElementId,
  } = useDesktopLightboxDialog({
    open: lightbox.open,
    displaySrc,
  });

  const [edgeHint, setEdgeHint] = useState<
    "first" | "last" | null
  >(null);
  const [, setSlideDir] = useState<1 | -1>(1);
  const [pendingImageId, setPendingImageId] = useState<string | null>(
    null,
  );
  const switchSeqRef = useRef(0);
  const preloadAbortRef = useRef<AbortController | null>(null);

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

  const handleClose = useCallback(() => {
    hideActiveImageLayer();
    switchSeqRef.current += 1;
    preloadAbortRef.current?.abort();
    preloadAbortRef.current = null;
    setEventGallery(null);
    setEventItems(null);
    hideDetails();
    setPendingImageId(null);
    closeLightbox();
  }, [closeLightbox, hideActiveImageLayer, hideDetails]);

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
  const {
    downloadStatus,
    downloadTitle,
    downloadText,
    shareStatus,
    shareTitle,
    shareText,
    handleDownload,
    handleShare,
    handleOpenOriginal,
  } = useDesktopLightboxMediaActions({
    open: lightbox.open,
    imageId: lightbox.imageId,
    imageSrc: lightbox.imageSrc,
    imageStateKey,
    activeImageStateKeyRef,
    currentImageMeta,
    downloadAnchorRef,
  });

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

  const {
    isPanning,
    mainImageLoaded,
    setZoom,
    resetView,
    setViewMode,
    handleWheel,
    handleImageLoad,
    handleImageError,
    handleImagePointerDown,
    handleImagePointerMove,
    handleImagePointerEnd,
    handleImagePointerCancel,
  } = useDesktopLightboxViewport({
    open: lightbox.open,
    imageStateKey,
    imageSrc: lightbox.imageSrc,
    displaySrc,
    activeImageStateKeyRef,
    currentImageMeta,
    activeViewMode,
    activeZoom,
    activePanOffset,
    containerRef,
    imageWrapRef,
    imageRef,
    updateImageState,
    gotoDelta,
    handleClose,
  });

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
    switchSeqRef.current += 1;
    preloadAbortRef.current?.abort();
    preloadAbortRef.current = null;
    let canceled = false;
    queueMicrotask(() => {
      if (canceled) return;
      setPendingImageId(null);
      setEdgeHint(null);
    });
    return () => {
      canceled = true;
    };
  }, [imageStateKey]);

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
    isPanning,
    mainImageLoaded,
    detailsOpen,
    imageActionsAvailable,
    downloadStatus,
    downloadTitle,
    downloadText,
    shareStatus,
    shareTitle,
    shareText,
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
