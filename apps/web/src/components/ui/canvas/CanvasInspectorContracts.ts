import type { CanvasDocument } from "@/lib/canvas/types";

export type CanvasSelectionAlignment =
  | "left"
  | "horizontal-center"
  | "right"
  | "top"
  | "vertical-center"
  | "bottom";

export type CanvasSelectionDistribution = "horizontal" | "vertical";

export interface CanvasInspectorProps {
  document: CanvasDocument;
  onRunNode: (nodeId: string) => void;
  runningNodeId?: string | null;
  onDuplicateSelection?: () => void;
  onAlignSelection?: (alignment: CanvasSelectionAlignment) => void;
  onDistributeSelection?: (
    distribution: CanvasSelectionDistribution,
  ) => void;
  onAutoLayoutSelection?: () => void;
  onFitSelection?: () => void;
}
