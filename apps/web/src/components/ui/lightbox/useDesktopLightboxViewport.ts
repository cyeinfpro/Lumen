"use client";

import {
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  CLICK_MAX_DURATION_MS,
  CLICK_TAP_SLOP,
  CLICK_ZOOM,
  RESET_PAN_OFFSET,
  ZOOM_STEP,
  clampPanOffset,
  clampZoom,
  resolvePanBoundsInput,
  type DesktopImageMeta,
  type ImagePointerState,
  type ImageTransientState,
  type MousePanState,
  type PanOffset,
  type TouchActions,
  type ViewMode,
} from "./desktopLightboxModel";
import { useDesktopLightboxTouch } from "./desktopLightboxTouch";

type UpdateImageState = (
  recipe: (state: ImageTransientState) => ImageTransientState,
) => void;

interface UseDesktopLightboxViewportOptions {
  open: boolean;
  imageStateKey: string;
  imageSrc?: string | null;
  displaySrc?: string | null;
  activeImageStateKeyRef: RefObject<string>;
  currentImageMeta: DesktopImageMeta | null;
  activeViewMode: ViewMode;
  activeZoom: number;
  activePanOffset: PanOffset;
  containerRef: RefObject<HTMLDivElement | null>;
  imageWrapRef: RefObject<HTMLDivElement | null>;
  imageRef: RefObject<HTMLImageElement | null>;
  updateImageState: UpdateImageState;
  gotoDelta: (delta: 1 | -1) => void;
  handleClose: () => void;
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

export function useDesktopLightboxViewport({
  open,
  imageStateKey,
  imageSrc,
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
}: UseDesktopLightboxViewportOptions) {
  const [mousePan, setMousePan] = useState<MousePanState | null>(null);
  const [mainImageLoaded, setMainImageLoaded] = useState(false);
  const imagePointerRef = useRef<ImagePointerState | null>(null);
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
  }, [currentImageMeta, imageRef, imageWrapRef]);

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
    [clampPanForCurrentView, imageRef, updateImageState],
  );

  const handleWheel = useCallback(
    (event: ReactWheelEvent<HTMLDivElement>) => {
      if (!event.ctrlKey && !event.metaKey && activeZoom <= 1) return;
      event.preventDefault();
      const direction = event.deltaY > 0 ? -1 : 1;
      const nextZoom = clampZoom(
        activeZoom + direction * ZOOM_STEP,
      );
      if (nextZoom === activeZoom) return;

      const wrapRect = imageWrapRef.current?.getBoundingClientRect();
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
      imageWrapRef,
      setZoom,
      updateImageState,
    ],
  );

  const handleImagePointerDown = useCallback(
    (event: ReactPointerEvent<HTMLImageElement>) => {
      if (event.pointerType === "touch" || event.button !== 0) return;
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
    (event: ReactPointerEvent<HTMLImageElement>) => {
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
    (event: ReactPointerEvent<HTMLImageElement>) => {
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
    (event: ReactPointerEvent<HTMLImageElement>) => {
      const gesture = imagePointerRef.current;
      if (!gesture || gesture.pointerId !== event.pointerId) return;
      imagePointerRef.current = null;
      setMousePan(null);
      releaseImagePointer(event.currentTarget, event.pointerId);
    },
    [],
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
    activeImageStateKeyRef,
    clampPanForCurrentView,
    imageStateKey,
    updateImageState,
  ]);

  const handleImageError = useCallback(() => {
    if (activeImageStateKeyRef.current !== imageStateKey) return;
    if (displaySrc !== imageSrc && imageSrc) {
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
    activeImageStateKeyRef,
    displaySrc,
    imageSrc,
    imageStateKey,
    updateImageState,
  ]);

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

  useEffect(() => {
    if (open) return;
    let canceled = false;
    queueMicrotask(() => {
      if (canceled) return;
      setMousePan(null);
      imagePointerRef.current = null;
    });
    return () => {
      canceled = true;
    };
  }, [open]);

  useEffect(() => {
    let canceled = false;
    queueMicrotask(() => {
      if (canceled) return;
      setMousePan(null);
      imagePointerRef.current = null;
      setMainImageLoaded(false);
    });
    return () => {
      canceled = true;
    };
  }, [imageStateKey]);

  useEffect(() => {
    if (!open) return;
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
  }, [clampPanForCurrentView, open, updateImageState]);

  useDesktopLightboxTouch({
    open,
    containerRef,
    zoomRef,
    viewModeRef,
    panOffsetRef,
    actionsRef: touchActionsRef,
  });

  return {
    isPanning: mousePan !== null,
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
  };
}
