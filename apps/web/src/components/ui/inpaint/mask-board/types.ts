import type Konva from "konva";

export interface MaskExport {
  blob: Blob;
  preview_data_url: string;
  width: number;
  height: number;
  /** 涂抹覆盖比例（0..1，alpha=0 像素占比；大图采样估算） */
  coverage: number;
}

export interface MaskBoardHandle {
  exportMask: () => Promise<MaskExport | null>;
  hasStrokes: () => boolean;
  clear: () => void;
}

export interface StagePoint {
  x: number;
  y: number;
}

export interface ViewTransform {
  x: number;
  y: number;
  scale: number;
}

export interface PinchGesture {
  startDistance: number;
  startLocalCenter: StagePoint;
  startView: ViewTransform;
}

export interface MaskCursor {
  x: number;
  y: number;
  pointerType: "mouse" | "pen" | "touch" | null;
}

export interface DisplayDimensions {
  width: number;
  height: number;
  scale: number;
}

export type MaskStagePointerHandler = (
  event: Konva.KonvaEventObject<PointerEvent>,
) => void;

export interface MaskBoardStats {
  coverage: number;
  strokeCount: number;
}

export interface ContainerDimensions {
  w: number;
  h: number;
}
