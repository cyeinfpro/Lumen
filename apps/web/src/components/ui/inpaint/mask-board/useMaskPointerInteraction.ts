import type Konva from "konva";
import {
  type Dispatch,
  type MutableRefObject,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  type SetStateAction,
  type WheelEvent as ReactWheelEvent,
  useCallback,
  useEffect,
  useRef,
} from "react";

import { MASK_BRUSH_STEP } from "../maskBoardKeyboard";
import type { Stroke, Tool } from "../types";
import {
  clampBrush,
  clampViewTransform,
  defaultViewTransform,
  distance,
  effectiveBrushRadius,
  imagePointFromStagePoint,
  isPointInsideDisplay,
  isPrimaryDrawingPointer,
  MAX_VIEW_SCALE,
  midpoint,
  MIN_VIEW_SCALE,
  PINCH_MIN_DISTANCE,
  pointFromPointerEvent,
  stagePointFromImagePoint,
  STROKE_MIN_DELTA_SQ,
} from "./geometry";
import type {
  DisplayDimensions,
  MaskCursor,
  PinchGesture,
  StagePoint,
  ViewTransform,
} from "./types";

interface UseMaskPointerInteractionOptions {
  stageRef: RefObject<Konva.Stage | null>;
  disabled: boolean;
  imageSrc: string;
  displayKey: string;
  displayDims: DisplayDimensions;
  tool: Tool;
  brushSize: number;
  setBrushSize: Dispatch<SetStateAction<number>>;
  setStrokes: Dispatch<SetStateAction<Stroke[]>>;
  setCursor: Dispatch<SetStateAction<MaskCursor | null>>;
  view: ViewTransform;
  setView: Dispatch<SetStateAction<ViewTransform>>;
}

function shouldStopForTouchGesture({
  native,
  stagePoint,
  activeTouchPoints,
  suppressTouchDraw,
  cancelTouchStroke,
  updatePinchGesture,
}: {
  native: PointerEvent;
  stagePoint: StagePoint | null;
  activeTouchPoints: Map<number, StagePoint>;
  suppressTouchDraw: MutableRefObject<boolean>;
  cancelTouchStroke: () => void;
  updatePinchGesture: () => void;
}): boolean {
  if (native.pointerType !== "touch" || !stagePoint) return false;
  native.preventDefault();
  activeTouchPoints.set(native.pointerId, stagePoint);
  if (activeTouchPoints.size >= 2) {
    suppressTouchDraw.current = true;
    cancelTouchStroke();
    updatePinchGesture();
    return true;
  }
  return suppressTouchDraw.current;
}

export function useMaskPointerInteraction({
  stageRef,
  disabled,
  imageSrc,
  displayKey,
  displayDims,
  tool,
  brushSize,
  setBrushSize,
  setStrokes,
  setCursor,
  view,
  setView,
}: UseMaskPointerInteractionOptions) {
  const viewRef = useRef(view);
  const drawingRef = useRef(false);
  const touchStrokeStartedRef = useRef(false);
  const activeTouchPointsRef = useRef<Map<number, StagePoint>>(new Map());
  const pinchGestureRef = useRef<PinchGesture | null>(null);
  const suppressTouchDrawRef = useRef(false);

  useEffect(() => {
    viewRef.current = view;
  }, [view]);

  useEffect(() => {
    drawingRef.current = false;
    activeTouchPointsRef.current.clear();
    pinchGestureRef.current = null;
    suppressTouchDrawRef.current = false;
    touchStrokeStartedRef.current = false;
  }, [displayKey, imageSrc]);

  const cancelTouchStrokeForGesture = useCallback(() => {
    drawingRef.current = false;
    setCursor(null);
    if (!touchStrokeStartedRef.current) return;
    setStrokes((current) => {
      const last = current[current.length - 1];
      if (!last || last.points.length > 2) return current;
      return current.slice(0, -1);
    });
    touchStrokeStartedRef.current = false;
  }, [setCursor, setStrokes]);

  const updatePinchGesture = useCallback(() => {
    const points = Array.from(activeTouchPointsRef.current.values());
    if (points.length < 2) return;
    const first = points[0];
    const second = points[1];
    const nextDistance = distance(first, second);
    if (nextDistance < PINCH_MIN_DISTANCE) return;
    const nextCenter = midpoint(first, second);
    let gesture = pinchGestureRef.current;
    if (!gesture) {
      const startView = viewRef.current;
      gesture = {
        startDistance: nextDistance,
        startLocalCenter: {
          x: (nextCenter.x - startView.x) / startView.scale,
          y: (nextCenter.y - startView.y) / startView.scale,
        },
        startView,
      };
      pinchGestureRef.current = gesture;
    }
    const nextScale = Math.max(
      MIN_VIEW_SCALE,
      Math.min(
        MAX_VIEW_SCALE,
        gesture.startView.scale * (nextDistance / gesture.startDistance),
      ),
    );
    setView(
      clampViewTransform(
        {
          scale: nextScale,
          x: nextCenter.x - gesture.startLocalCenter.x * nextScale,
          y: nextCenter.y - gesture.startLocalCenter.y * nextScale,
        },
        displayDims.width,
        displayDims.height,
      ),
    );
  }, [displayDims.height, displayDims.width, setView]);

  const finishTouchPointer = useCallback((pointerId: number) => {
    activeTouchPointsRef.current.delete(pointerId);
    if (activeTouchPointsRef.current.size < 2) {
      pinchGestureRef.current = null;
    }
    if (activeTouchPointsRef.current.size === 0) {
      suppressTouchDrawRef.current = false;
      touchStrokeStartedRef.current = false;
    }
  }, []);

  const handlePointerDown = useCallback(
    (event: Konva.KonvaEventObject<PointerEvent>) => {
      if (disabled) return;
      const stage = event.target.getStage();
      const native = event.evt as PointerEvent;
      const stagePoint = pointFromPointerEvent(native, stageRef.current);
      if (
        shouldStopForTouchGesture({
          native,
          stagePoint,
          activeTouchPoints: activeTouchPointsRef.current,
          suppressTouchDraw: suppressTouchDrawRef,
          cancelTouchStroke: cancelTouchStrokeForGesture,
          updatePinchGesture,
        })
      ) {
        return;
      }
      const pointer = stagePoint ?? stage?.getPointerPosition();
      if (!pointer) return;
      const position = imagePointFromStagePoint(
        pointer,
        viewRef.current,
        displayDims,
      );
      if (!isPointInsideDisplay(position, displayDims)) return;
      if (!isPrimaryDrawingPointer(native)) return;
      drawingRef.current = true;
      touchStrokeStartedRef.current = native.pointerType === "touch";
      const radius = effectiveBrushRadius(native, brushSize);
      setStrokes((current) => [
        ...current,
        { tool, radius, points: [position.x, position.y] },
      ]);
    },
    [
      brushSize,
      cancelTouchStrokeForGesture,
      disabled,
      displayDims,
      setStrokes,
      stageRef,
      tool,
      updatePinchGesture,
    ],
  );

  const handlePointerMove = useCallback(
    (event: Konva.KonvaEventObject<PointerEvent>) => {
      if (disabled) return;
      const stage = event.target.getStage();
      const native = event.evt as PointerEvent;
      const stagePoint = pointFromPointerEvent(native, stageRef.current);
      if (native.pointerType === "touch" && stagePoint) {
        native.preventDefault();
        activeTouchPointsRef.current.set(native.pointerId, stagePoint);
        if (
          activeTouchPointsRef.current.size >= 2 ||
          pinchGestureRef.current
        ) {
          suppressTouchDrawRef.current = true;
          cancelTouchStrokeForGesture();
          updatePinchGesture();
          return;
        }
        if (suppressTouchDrawRef.current) return;
      }
      const pointer = stagePoint ?? stage?.getPointerPosition();
      if (!pointer) return;
      const position = imagePointFromStagePoint(
        pointer,
        viewRef.current,
        displayDims,
      );
      const cursorPoint = stagePointFromImagePoint(position, viewRef.current);
      const pointerType =
        (native.pointerType as "mouse" | "pen" | "touch" | undefined) ??
        "mouse";
      setCursor({ ...cursorPoint, pointerType });
      if (!drawingRef.current) return;
      setStrokes((current) => appendStrokePoint(current, position));
    },
    [
      cancelTouchStrokeForGesture,
      disabled,
      displayDims,
      setCursor,
      setStrokes,
      stageRef,
      updatePinchGesture,
    ],
  );

  const handlePointerUp = useCallback(
    (event: Konva.KonvaEventObject<PointerEvent>) => {
      const native = event.evt as PointerEvent;
      if (native.pointerType === "touch") {
        finishTouchPointer(native.pointerId);
      }
      drawingRef.current = false;
      touchStrokeStartedRef.current = false;
    },
    [finishTouchPointer],
  );

  const handlePointerLeave = useCallback(
    (event: Konva.KonvaEventObject<PointerEvent>) => {
      const native = event.evt as PointerEvent;
      if (native.pointerType === "touch") {
        finishTouchPointer(native.pointerId);
        return;
      }
      drawingRef.current = false;
      setCursor(null);
    },
    [finishTouchPointer, setCursor],
  );

  const handleWheel = useCallback(
    (event: ReactWheelEvent<HTMLDivElement>) => {
      if (disabled || event.deltaY === 0) return;
      event.preventDefault();
      event.stopPropagation();
      const direction = event.deltaY < 0 ? 1 : -1;
      setBrushSize((value) =>
        clampBrush(value + direction * MASK_BRUSH_STEP),
      );
    },
    [disabled, setBrushSize],
  );

  const fitView = useCallback(() => {
    activeTouchPointsRef.current.clear();
    pinchGestureRef.current = null;
    suppressTouchDrawRef.current = false;
    touchStrokeStartedRef.current = false;
    drawingRef.current = false;
    setView(defaultViewTransform());
  }, [setView]);

  const onContainerPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if ((event.target as HTMLElement).closest("[data-mask-canvas-stage]")) {
        event.preventDefault();
      }
    },
    [],
  );

  return {
    handlePointerDown,
    handlePointerMove,
    handlePointerUp,
    handlePointerLeave,
    handleWheel,
    fitView,
    onContainerPointerDown,
  };
}

function appendStrokePoint(
  strokes: Stroke[],
  point: StagePoint,
): Stroke[] {
  if (strokes.length === 0) return strokes;
  const last = strokes[strokes.length - 1];
  const lastX = last.points[last.points.length - 2];
  const lastY = last.points[last.points.length - 1];
  const deltaX = point.x - lastX;
  const deltaY = point.y - lastY;
  if (deltaX * deltaX + deltaY * deltaY < STROKE_MIN_DELTA_SQ) {
    return strokes;
  }
  const next: Stroke = {
    ...last,
    points: [...last.points, point.x, point.y],
  };
  return [...strokes.slice(0, -1), next];
}
