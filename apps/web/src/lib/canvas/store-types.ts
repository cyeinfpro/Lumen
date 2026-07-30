import type { StoreApi } from "zustand/vanilla";

import type { CanvasConnectionInput } from "#canvas-graph";
import type { CanvasNodeCreateOverrides } from "#canvas-registry";
import type {
  CanvasDocumentSettings,
  CanvasEdgeDetailsUpdate,
  CanvasGraph,
  CanvasHistoryEntry,
  CanvasNodeAppearanceUpdate,
  CanvasNodeType,
  CanvasOperation,
  CanvasPosition,
  CanvasSaveState,
  CanvasSize,
  CanvasToolMode,
  ConnectionDraft,
} from "#canvas-types";
import type { CanvasSubgraph, InsertSubgraphOptions } from "./clipboard";

export interface CanvasNodeMove {
  nodeId: string;
  position: CanvasPosition;
}

export interface CanvasEditorState {
  graph: CanvasGraph;
  revision: number;
  selectedNodeId: string | null;
  selectedNodeIds: string[];
  selectedEdgeId: string | null;
  editingNodeId: string | null;
  toolMode: CanvasToolMode;
  connectionDraft: ConnectionDraft | null;
  activeInteractionCount: number;
  pendingOperations: CanvasOperation[];
  pendingOperationGroupSizes: number[];
  inFlightOperationCount: number;
  retryPrefixOperationCount: number;
  history: CanvasHistoryEntry[];
  future: CanvasHistoryEntry[];
  saveState: CanvasSaveState;
  saveMessage: string | null;
  hydrate: (graph: CanvasGraph, revision: number) => void;
  addNode: (
    type: CanvasNodeType,
    position: CanvasPosition,
    overrides?: CanvasNodeCreateOverrides,
  ) => string;
  updateNodeConfig: (nodeId: string, config: Record<string, unknown>) => void;
  beginNodeConfigEdit: (nodeId: string) => void;
  endNodeConfigEdit: (nodeId: string) => void;
  updateNodeAppearance: (
    nodeId: string,
    appearance: CanvasNodeAppearanceUpdate,
  ) => void;
  updateNodeTitle: (nodeId: string, title: string) => void;
  resizeNode: (
    nodeId: string,
    size: CanvasSize,
    position?: CanvasPosition,
  ) => void;
  moveNode: (nodeId: string, position: CanvasPosition) => void;
  moveNodes: (items: CanvasNodeMove[]) => void;
  duplicateNodes: (nodeIds: string[], offset?: CanvasPosition) => string[];
  insertSubgraph: (
    subgraph: CanvasSubgraph,
    options?: InsertSubgraphOptions,
  ) => string[];
  removeElements: (nodeIds: string[], edgeIds: string[]) => void;
  removeNodes: (nodeIds: string[]) => void;
  addEdge: (
    input: CanvasConnectionInput,
  ) => { ok: true } | { ok: false; reason: string };
  updateEdgeDetails: (
    edgeId: string,
    details: CanvasEdgeDetailsUpdate,
  ) => void;
  updateEdgeBinding: (
    edgeId: string,
    bindingMode: "follow_active" | "pinned",
    pinnedExecutionId?: string | null,
    pinnedOutputIndex?: number | null,
  ) => void;
  updateDocumentSettings: (
    settings: Partial<CanvasDocumentSettings>,
  ) => void;
  removeEdges: (edgeIds: string[]) => void;
  selectNode: (nodeId: string | null) => void;
  selectNodes: (nodeIds: string[]) => void;
  selectEdge: (edgeId: string | null) => void;
  beginNodeEdit: (nodeId: string) => void;
  endNodeEdit: (nodeId: string) => void;
  setToolMode: (mode: CanvasToolMode) => void;
  setConnectionDraft: (draft: ConnectionDraft | null) => void;
  beginInteraction: () => void;
  endInteraction: () => void;
  undo: () => void;
  redo: () => void;
  markSaving: (count?: number) => void;
  acknowledgeOperations: (count: number, revision: number) => boolean;
  markSaveError: (message: string, retryable?: boolean) => void;
  markConflict: (message: string) => void;
  replaceFromRemote: (graph: CanvasGraph, revision: number) => void;
}

export type CanvasEditorStore = StoreApi<CanvasEditorState>;
