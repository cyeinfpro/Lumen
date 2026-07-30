import type { CanvasDocument, ConnectionDraft } from "@/lib/canvas/types";

export interface CanvasViewportMotionOptions {
  instant?: boolean;
}

export interface CanvasViewportApi {
  fitView: (options?: CanvasViewportMotionOptions) => void;
  fitSelection: (
    nodeIds?: readonly string[],
    options?: CanvasViewportMotionOptions,
  ) => void;
  focusNode: (nodeId: string) => void;
  zoomIn: (options?: CanvasViewportMotionOptions) => void;
  zoomOut: (options?: CanvasViewportMotionOptions) => void;
  resetZoom: (options?: CanvasViewportMotionOptions) => void;
  toggleMiniMap: () => void;
  getZoom: () => number;
  getViewportCenter: () => { x: number; y: number };
}

export interface CanvasViewportActionRequest {
  position: { x: number; y: number };
  clientPosition: { x: number; y: number };
  trigger:
    | "empty-state"
    | "pane-double-click"
    | "connection-drop"
    | "pane-context-menu"
    | "node-context-menu"
    | "edge-context-menu";
  connectionDraft: ConnectionDraft | null;
  nodeId?: string;
  edgeId?: string;
}

export interface CanvasViewportProps {
  document: CanvasDocument;
  onRunNode: (nodeId: string) => void;
  onReady?: (api: CanvasViewportApi) => void;
  onOpenInspector?: () => void;
  onOpenQuickAdd?: (request: CanvasViewportActionRequest) => void;
  onOpenContextMenu?: (request: CanvasViewportActionRequest) => void;
}
