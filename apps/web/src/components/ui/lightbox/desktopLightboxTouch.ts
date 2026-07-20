import { useEffect, type RefObject } from "react";

import {
  MAX_ZOOM,
  RESET_PAN_OFFSET,
  type PanOffset,
  type TouchActions,
  type ViewMode,
} from "./desktopLightboxModel";

type CurrentRef<T> = { current: T };
type TouchMode = "idle" | "swipe" | "pan" | "pinch";

type DesktopTouchGesture = {
  startX: number;
  startY: number;
  mode: TouchMode;
  pinchStartDist: number;
  pinchStartZoom: number;
  panStartOffset: PanOffset;
};

type DesktopLightboxTouchOptions = {
  open: boolean;
  containerRef: RefObject<HTMLDivElement | null>;
  zoomRef: CurrentRef<number>;
  viewModeRef: CurrentRef<ViewMode>;
  panOffsetRef: CurrentRef<PanOffset>;
  actionsRef: CurrentRef<TouchActions>;
};

function createTouchGesture(): DesktopTouchGesture {
  return {
    startX: 0,
    startY: 0,
    mode: "idle",
    pinchStartDist: 0,
    pinchStartZoom: 1,
    panStartOffset: RESET_PAN_OFFSET,
  };
}

function touchDistance(first: Touch, second: Touch): number {
  return Math.hypot(
    first.clientX - second.clientX,
    first.clientY - second.clientY,
  );
}

function beginTouchGesture(
  event: TouchEvent,
  gesture: DesktopTouchGesture,
  zoom: number,
  viewMode: ViewMode,
  panOffset: PanOffset,
): void {
  if (event.touches.length === 2) {
    gesture.mode = "pinch";
    gesture.pinchStartDist = touchDistance(
      event.touches[0],
      event.touches[1],
    );
    gesture.pinchStartZoom = zoom;
    return;
  }
  if (event.touches.length !== 1) return;
  gesture.startX = event.touches[0].clientX;
  gesture.startY = event.touches[0].clientY;
  if (zoom > 1 || viewMode !== "fit") {
    gesture.mode = "pan";
    gesture.panStartOffset = { ...panOffset };
    return;
  }
  gesture.mode = "swipe";
}

function movePinch(
  event: TouchEvent,
  gesture: DesktopTouchGesture,
  actions: TouchActions,
): boolean {
  if (gesture.mode !== "pinch" || event.touches.length !== 2) {
    return false;
  }
  const distance = touchDistance(event.touches[0], event.touches[1]);
  if (gesture.pinchStartDist <= 0) return true;
  const zoom = Math.min(
    MAX_ZOOM,
    Math.max(
      1,
      gesture.pinchStartZoom * (distance / gesture.pinchStartDist),
    ),
  );
  actions.updateImageState((state) => {
    const viewMode =
      zoom > 1 && state.viewMode === "fit" ? "actual" : state.viewMode;
    const panOffset =
      zoom === 1 && viewMode === "fit"
        ? RESET_PAN_OFFSET
        : state.panOffset;
    return {
      ...state,
      viewMode,
      zoom,
      panOffset: actions.clampPanForCurrentView(
        panOffset,
        zoom,
        viewMode,
      ),
    };
  });
  return true;
}

function movePan(
  event: TouchEvent,
  gesture: DesktopTouchGesture,
  actions: TouchActions,
  zoom: number,
  viewMode: ViewMode,
): boolean {
  if (gesture.mode !== "pan" || event.touches.length !== 1) return false;
  const dx = event.touches[0].clientX - gesture.startX;
  const dy = event.touches[0].clientY - gesture.startY;
  const panOffset = actions.clampPanForCurrentView(
    {
      x: gesture.panStartOffset.x + dx,
      y: gesture.panStartOffset.y + dy,
    },
    zoom,
    viewMode,
  );
  actions.updateImageState((state) => ({ ...state, panOffset }));
  return true;
}

function moveTouchGesture(
  event: TouchEvent,
  gesture: DesktopTouchGesture,
  actions: TouchActions,
  zoom: number,
  viewMode: ViewMode,
): void {
  if (event.cancelable && gesture.mode !== "idle") {
    event.preventDefault();
  }
  if (movePinch(event, gesture, actions)) return;
  movePan(event, gesture, actions, zoom, viewMode);
}

function finishSwipe(
  touch: Touch,
  gesture: DesktopTouchGesture,
  actions: TouchActions,
): void {
  const dx = touch.clientX - gesture.startX;
  const dy = touch.clientY - gesture.startY;
  const absDx = Math.abs(dx);
  const absDy = Math.abs(dy);
  if (absDx > 60 && absDx > absDy * 1.5) {
    actions.gotoDelta(dx < 0 ? 1 : -1);
    return;
  }
  if (dy > 80 && absDy > absDx * 1.5) actions.handleClose();
}

function finishTouchGesture(
  event: TouchEvent,
  gesture: DesktopTouchGesture,
  actions: TouchActions,
): void {
  if (
    gesture.mode === "swipe" &&
    event.changedTouches.length > 0
  ) {
    finishSwipe(event.changedTouches[0], gesture, actions);
  }
  if (event.touches.length === 0) gesture.mode = "idle";
}

export function useDesktopLightboxTouch({
  open,
  containerRef,
  zoomRef,
  viewModeRef,
  panOffsetRef,
  actionsRef,
}: DesktopLightboxTouchOptions): void {
  useEffect(() => {
    if (!open) return;
    const element = containerRef.current;
    if (!element) return;
    const gesture = createTouchGesture();
    const onStart = (event: TouchEvent) => {
      beginTouchGesture(
        event,
        gesture,
        zoomRef.current,
        viewModeRef.current,
        panOffsetRef.current,
      );
    };
    const onMove = (event: TouchEvent) => {
      moveTouchGesture(
        event,
        gesture,
        actionsRef.current,
        zoomRef.current,
        viewModeRef.current,
      );
    };
    const onEnd = (event: TouchEvent) => {
      finishTouchGesture(event, gesture, actionsRef.current);
    };

    element.addEventListener("touchstart", onStart, { passive: true });
    element.addEventListener("touchmove", onMove, { passive: false });
    element.addEventListener("touchend", onEnd, { passive: true });
    element.addEventListener("touchcancel", onEnd, { passive: true });
    return () => {
      element.removeEventListener("touchstart", onStart);
      element.removeEventListener("touchmove", onMove);
      element.removeEventListener("touchend", onEnd);
      element.removeEventListener("touchcancel", onEnd);
    };
  }, [
    actionsRef,
    containerRef,
    open,
    panOffsetRef,
    viewModeRef,
    zoomRef,
  ]);
}
