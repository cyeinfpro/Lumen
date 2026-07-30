import type { Node } from "@xyflow/react";

import type {
  CanvasDataType,
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasOutput,
  CanvasPosition,
  CanvasSize,
} from "@/lib/canvas/types";

export interface CanvasFlowNodeData extends Record<string, unknown> {
  definition: CanvasNodeDefinition;
  execution?: CanvasNodeExecution | null;
  activeOutput?: CanvasOutput | null;
  deliveryOutputs?: CanvasOutput[];
  resolvedText?: string;
  inputCounts?: Record<string, number>;
  runDisabledReason?: string | null;
  connectionType?: CanvasDataType | null;
  compatibleInputHandles?: string[];
  onRun?: (nodeId: string) => void;
  onUpdateConfig?: (nodeId: string, config: Record<string, unknown>) => void;
  onUpdateTitle?: (nodeId: string, title: string) => void;
  onEditFocus?: (nodeId: string) => void;
  onEditBlur?: (nodeId: string) => void;
  onConfigEditStart?: (nodeId: string) => void;
  onConfigEditEnd?: (nodeId: string) => void;
  onStartConnection?: (
    nodeId: string,
    handleId: string,
    dataType: CanvasDataType,
  ) => void;
  onCompleteConnection?: (nodeId: string, handleId: string) => void;
  onResizeStart?: (nodeId: string) => void;
  onResizeEnd?: (
    nodeId: string,
    geometry: { position: CanvasPosition; size: CanvasSize },
  ) => void;
  editingEnabled?: boolean;
}

export type CanvasFlowNode = Node<
  CanvasFlowNodeData,
  CanvasNodeDefinition["type"]
>;
