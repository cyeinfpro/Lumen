import type Konva from "konva";

import type {
  ContainerDimensions,
  DisplayDimensions,
  StagePoint,
  ViewTransform,
} from "./types";

export const MAX_DISPLAY = 768;
export const MIN_BRUSH = 8;
export const MAX_BRUSH = 96;
export const DEFAULT_BRUSH_DESKTOP = 36;
export const DEFAULT_BRUSH_TOUCH = 56;
export const STROKE_MIN_DELTA_SQ = 1.44;
export const MIN_VIEW_SCALE = 1;
export const MAX_VIEW_SCALE = 6;
export const PINCH_MIN_DISTANCE = 8;

const OVERLAY_RED = "rgba(255, 59, 48, 0.5)";
const CURSOR_RED_STROKE = "rgba(255, 59, 48, 0.92)";
const CURSOR_RED_FILL = "rgba(255, 59, 48, 0.16)";
const OVERLAY_CYAN = "rgba(64, 224, 208, 0.55)";
const CURSOR_CYAN_STROKE = "rgba(64, 224, 208, 0.95)";
const CURSOR_CYAN_FILL = "rgba(64, 224, 208, 0.18)";

export function isTouchDevice(): boolean {
  if (typeof window === "undefined") return false;
  return "ontouchstart" in window || (navigator.maxTouchPoints ?? 0) > 0;
}

export function clampBrush(value: number): number {
  return Math.max(MIN_BRUSH, Math.min(MAX_BRUSH, Math.round(value)));
}

export function defaultViewTransform(): ViewTransform {
  return { x: 0, y: 0, scale: 1 };
}

export function clampPoint(
  point: StagePoint,
  width: number,
  height: number,
): StagePoint {
  return {
    x: Math.max(0, Math.min(width, point.x)),
    y: Math.max(0, Math.min(height, point.y)),
  };
}

export function distance(a: StagePoint, b: StagePoint): number {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

export function midpoint(a: StagePoint, b: StagePoint): StagePoint {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

export function clampViewTransform(
  view: ViewTransform,
  width: number,
  height: number,
): ViewTransform {
  if (!width || !height) return defaultViewTransform();
  const scale = Math.max(MIN_VIEW_SCALE, Math.min(MAX_VIEW_SCALE, view.scale));
  const minX = Math.min(0, width - width * scale);
  const minY = Math.min(0, height - height * scale);
  return {
    scale,
    x: Math.max(minX, Math.min(0, view.x)),
    y: Math.max(minY, Math.min(0, view.y)),
  };
}

export function pointFromPointerEvent(
  event: PointerEvent,
  stage: Konva.Stage | null,
): StagePoint | null {
  const rect = stage?.container().getBoundingClientRect();
  if (!rect) return null;
  return {
    x: event.clientX - rect.left,
    y: event.clientY - rect.top,
  };
}

export function imagePointFromStagePoint(
  point: StagePoint,
  view: ViewTransform,
  dimensions: DisplayDimensions,
): StagePoint {
  return clampPoint(
    {
      x: (point.x - view.x) / view.scale,
      y: (point.y - view.y) / view.scale,
    },
    dimensions.width,
    dimensions.height,
  );
}

export function stagePointFromImagePoint(
  point: StagePoint,
  view: ViewTransform,
): StagePoint {
  return {
    x: point.x * view.scale + view.x,
    y: point.y * view.scale + view.y,
  };
}

export function isPointInsideDisplay(
  point: StagePoint,
  dimensions: DisplayDimensions,
): boolean {
  return (
    point.x >= 0 &&
    point.y >= 0 &&
    point.x <= dimensions.width &&
    point.y <= dimensions.height
  );
}

export function isPrimaryDrawingPointer(event: PointerEvent): boolean {
  return event.button === undefined || event.button <= 0;
}

export function effectiveBrushRadius(
  event: PointerEvent,
  brushSize: number,
): number {
  const pressure =
    typeof event.pressure === "number" ? event.pressure : 0;
  const sizeMultiplier =
    event.pointerType === "pen" && pressure > 0
      ? 0.4 + pressure * 0.6
      : 1;
  return Math.max(
    Math.round(MIN_BRUSH / 2),
    Math.round(brushSize * sizeMultiplier),
  );
}

export function maskColorsForLuminance(luminance: number) {
  const isDarkBg = luminance < 0.45;
  return {
    isDarkBg,
    overlayColor: isDarkBg ? OVERLAY_CYAN : OVERLAY_RED,
    cursorStroke: isDarkBg ? CURSOR_CYAN_STROKE : CURSOR_RED_STROKE,
    cursorFill: isDarkBg ? CURSOR_CYAN_FILL : CURSOR_RED_FILL,
  };
}

export function isViewTransformFit(view: ViewTransform): boolean {
  return (
    Math.abs(view.scale - 1) < 0.001 &&
    Math.abs(view.x) < 0.5 &&
    Math.abs(view.y) < 0.5
  );
}

export function displayDimensions(
  image: Pick<HTMLImageElement, "naturalWidth" | "naturalHeight"> | null,
  container: ContainerDimensions | null,
): DisplayDimensions {
  if (!image) return { width: 0, height: 0, scale: 1 };
  const { naturalWidth: width, naturalHeight: height } = image;
  if (!width || !height) return { width: 0, height: 0, scale: 1 };
  const availableWidth = container?.w ?? MAX_DISPLAY;
  const availableHeight = container?.h ?? MAX_DISPLAY;
  const scale = Math.min(
    1,
    availableWidth / width,
    availableHeight / height,
    MAX_DISPLAY / Math.max(width, height),
  );
  return {
    width: Math.round(width * scale),
    height: Math.round(height * scale),
    scale,
  };
}
