"use client";

import {
  Background,
  BackgroundVariant,
  MiniMap,
  ReactFlow,
  type Connection,
  type Edge,
  type NodeChange,
  type OnConnectEnd,
  type OnConnectStartParams,
  type OnSelectionChangeParams,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useReducedMotion } from "framer-motion";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useMediaQuery } from "@/hooks/useMediaQuery";
import {
  blurActiveCanvasEditor,
  canvasNodeZIndex,
  splitCanvasNodePositionChanges,
  updateCanvasTransientPositions,
} from "@/lib/canvas/interaction";
import {
  validateCanvasConnections,
  type CanvasConnectionInput,
} from "@/lib/canvas/graph";
import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import {
  activeOutputsByNode,
  latestExecutionsByNode,
} from "@/lib/canvas/runtime";
import type {
  CanvasDataType,
  CanvasDocument,
  CanvasEdgeDefinition,
  CanvasGraph,
  CanvasNodeType,
  CanvasPosition,
  ConnectionDraft,
} from "@/lib/canvas/types";
import { toast } from "@/components/ui/primitives";
import { useCanvasStore } from "./CanvasStoreProvider";
import {
  CANVAS_ARIA_LABEL_CONFIG,
  CANVAS_MAX_ZOOM,
  canvasClickConnectionEnabled,
  canvasEdgeAriaLabel,
  canvasFlowNodeDimensions,
  canvasGridGap,
  canvasNodeDeliveryOutputs,
  canvasNodesConnectable,
  canvasPanOnDrag,
  fitCanvasViewport,
  flowViewportBounds,
  focusCanvasNode,
  omitCanvasNodeMeasurements,
  pointerClientPosition,
  shouldShowMiniMap,
  viewportAnimationDuration,
  type CanvasNodeGeometry,
  type CanvasViewportPreferences,
  type ConnectionCompatibility,
} from "./CanvasViewportModel";
import {
  CanvasEmptyState,
  MobileConnectTargets,
} from "./CanvasViewportOverlays";
import { CanvasViewportControls } from "./CanvasViewportControls";
import { canvasNodeTypes, type CanvasFlowNode } from "./nodes/CanvasNodes";
import styles from "./canvas.module.css";

export interface CanvasViewportApi {
  fitView: () => void;
  fitSelection: (nodeIds?: readonly string[]) => void;
  focusNode: (nodeId: string) => void;
  zoomIn: () => void;
  zoomOut: () => void;
  resetZoom: () => void;
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

const MINIMAP_NODE_THRESHOLD = 24;
const DESKTOP_MIN_ZOOM = 0.15;
const COMPACT_MIN_ZOOM = 0.08;

export function CanvasViewport({
  document,
  onRunNode,
  onReady,
  onOpenInspector,
  onOpenQuickAdd,
  onOpenContextMenu,
}: CanvasViewportProps) {
  const graph = useCanvasStore((state) => state.graph);
  const selectedNodeIds = useCanvasStore((state) => state.selectedNodeIds);
  const selectedEdgeId = useCanvasStore((state) => state.selectedEdgeId);
  const toolMode = useCanvasStore((state) => state.toolMode);
  const connectionDraft = useCanvasStore((state) => state.connectionDraft);
  const selectNode = useCanvasStore((state) => state.selectNode);
  const selectNodes = useCanvasStore((state) => state.selectNodes);
  const selectEdge = useCanvasStore((state) => state.selectEdge);
  const updateNodeConfig = useCanvasStore((state) => state.updateNodeConfig);
  const updateNodeTitle = useCanvasStore((state) => state.updateNodeTitle);
  const beginNodeEdit = useCanvasStore((state) => state.beginNodeEdit);
  const endNodeEdit = useCanvasStore((state) => state.endNodeEdit);
  const beginNodeConfigEdit = useCanvasStore(
    (state) => state.beginNodeConfigEdit,
  );
  const endNodeConfigEdit = useCanvasStore((state) => state.endNodeConfigEdit);
  const moveNodes = useCanvasStore((state) => state.moveNodes);
  const removeElements = useCanvasStore((state) => state.removeElements);
  const addEdge = useCanvasStore((state) => state.addEdge);
  const addNode = useCanvasStore((state) => state.addNode);
  const setConnectionDraft = useCanvasStore(
    (state) => state.setConnectionDraft,
  );
  const beginInteraction = useCanvasStore((state) => state.beginInteraction);
  const endInteraction = useCanvasStore((state) => state.endInteraction);
  const resizeNode = useCanvasStore((state) => state.resizeNode);
  const updateDocumentSettings = useCanvasStore(
    (state) => state.updateDocumentSettings,
  );
  const isCompact = useMediaQuery("(max-width: 1199px)") !== false;
  const reducedMotion = Boolean(useReducedMotion());
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const interactionActiveRef = useRef(false);
  const cancelledConnectionRef = useRef(false);
  const resizingNodeIdsRef = useRef(new Set<string>());
  const connectionDraftRef = useRef(connectionDraft);
  const instanceRef = useRef<ReactFlowInstance<CanvasFlowNode, Edge> | null>(
    null,
  );
  const editorFocusRequestRef = useRef(0);
  const connectionDropPositionRef = useRef<{ x: number; y: number } | null>(
    null,
  );
  const suppressPaneClickRef = useRef(false);
  const suppressPaneClickTimerRef = useRef<number | null>(null);
  const [instance, setInstance] = useState<ReactFlowInstance<
    CanvasFlowNode,
    Edge
  > | null>(null);
  const [targetPickerOpen, setTargetPickerOpen] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [miniMapVisible, setMiniMapVisible] = useState(
    () =>
      graph.nodes.length + Math.ceil(graph.edges.length / 2) >=
      MINIMAP_NODE_THRESHOLD,
  );
  const [transientPositions, setTransientPositions] = useState<
    Record<string, { x: number; y: number }>
  >({});
  const [measuredDimensions, setMeasuredDimensions] = useState<
    Record<string, { width: number; height: number }>
  >({});
  const selectedNodeIdSet = useMemo(
    () => new Set(selectedNodeIds),
    [selectedNodeIds],
  );
  const snapToGrid = graph.settings.snap_to_grid;
  const snapGrid = useMemo<[number, number]>(() => {
    const gridSize = Math.max(1, Math.round(graph.settings.grid_size));
    return [gridSize, gridSize];
  }, [graph.settings.grid_size]);
  const minimumZoom = canvasMinimumZoom(isCompact);
  const showMiniMap = shouldShowMiniMap(
    isCompact,
    miniMapVisible,
    graph.nodes.length,
  );
  const viewportPreferencesRef = useRef<CanvasViewportPreferences>({
    isCompact,
    reducedMotion,
    selectedNodeIds,
  });

  useEffect(() => {
    viewportPreferencesRef.current = {
      isCompact,
      reducedMotion,
      selectedNodeIds,
    };
  }, [isCompact, reducedMotion, selectedNodeIds]);

  useEffect(() => {
    connectionDraftRef.current = connectionDraft;
  }, [connectionDraft]);

  const updateConnectionDraft = useCallback(
    (draft: ConnectionDraft | null) => {
      connectionDraftRef.current = draft;
      setConnectionDraft(draft);
    },
    [setConnectionDraft],
  );

  const clearTransientNodeState = useCallback(
    (nodeIds?: readonly string[]) => {
      if (!nodeIds) {
        setTransientPositions({});
        setMeasuredDimensions({});
        return;
      }
      setTransientPositions((current) =>
        updateCanvasTransientPositions(current, [], nodeIds),
      );
      setMeasuredDimensions((current) =>
        omitCanvasNodeMeasurements(current, nodeIds),
      );
    },
    [],
  );

  const startInteraction = useCallback(() => {
    if (interactionActiveRef.current) return;
    interactionActiveRef.current = true;
    beginInteraction();
  }, [beginInteraction]);

  const finishInteraction = useCallback(
    (nodes: CanvasFlowNode[] = []) => {
      if (nodes.length > 0) {
        const positions = nodes.map((node) => ({
          nodeId: node.id,
          position: node.position,
        }));
        clearTransientNodeState(positions.map((item) => item.nodeId));
        moveNodes(positions);
      }
      if (!interactionActiveRef.current) return;
      interactionActiveRef.current = false;
      endInteraction();
    },
    [clearTransientNodeState, endInteraction, moveNodes],
  );

  const startFrameResize = useCallback(
    (nodeId: string) => {
      resizingNodeIdsRef.current.add(nodeId);
      startInteraction();
    },
    [startInteraction],
  );

  const commitFrameResize = useCallback(
    (nodeId: string, geometry: CanvasNodeGeometry) => {
      if (!cancelledConnectionRef.current) {
        resizeNode(nodeId, geometry.size, geometry.position);
      }
      resizingNodeIdsRef.current.delete(nodeId);
      clearTransientNodeState([nodeId]);
      finishInteraction();
    },
    [clearTransientNodeState, finishInteraction, resizeNode],
  );

  const focusNodeEditor = useCallback(
    (nodeId: string) => {
      selectNode(nodeId);
      beginNodeEdit(nodeId);
      const requestId = editorFocusRequestRef.current + 1;
      editorFocusRequestRef.current = requestId;
      let remainingFrames = 90;
      let settlingFrames = 8;
      const zoomWhenReady = () => {
        if (editorFocusRequestRef.current !== requestId) return;
        const current = instanceRef.current;
        const internalNode = current?.getInternalNode(nodeId);
        if (
          !current ||
          !internalNode?.measured.width ||
          !internalNode.measured.height
        ) {
          remainingFrames -= 1;
          if (remainingFrames > 0) {
            window.requestAnimationFrame(zoomWhenReady);
          }
          return;
        }
        if (settlingFrames > 0) {
          settlingFrames -= 1;
          window.requestAnimationFrame(zoomWhenReady);
          return;
        }
        if (current.getZoom() >= 0.75) return;
        const node = current.getNode(nodeId);
        if (!node) return;
        void current.fitView({
          nodes: [node],
          padding: 0.42,
          minZoom: 0.9,
          maxZoom: 1.08,
          duration: viewportAnimationDuration(reducedMotion),
        });
      };
      zoomWhenReady();
    },
    [beginNodeEdit, reducedMotion, selectNode],
  );
  const finishNodeEditor = useCallback(
    (nodeId: string) => {
      editorFocusRequestRef.current += 1;
      endNodeEdit(nodeId);
    },
    [endNodeEdit],
  );

  const executions = useMemo(
    () => latestExecutionsByNode(document.recent_executions),
    [document.recent_executions],
  );
  const activeOutputs = useMemo(
    () =>
      activeOutputsByNode({
        graph,
        selections: document.selections,
        recent_executions: document.recent_executions,
      }),
    [document.recent_executions, document.selections, graph],
  );
  const connectionCompatibility = useMemo(
    () => buildConnectionCompatibility(graph, connectionDraft),
    [connectionDraft, graph],
  );
  const startClickConnection = useCallback(
    (nodeId: string, handleId: string, dataType: CanvasDataType) => {
      if (isCompact && toolMode !== "connect") return;
      if (!isCompact && toolMode !== "select") return;
      blurActiveCanvasEditor();
      setTargetPickerOpen(false);
      const sameSource =
        connectionDraftRef.current?.sourceNodeId === nodeId &&
        connectionDraftRef.current.sourceHandle === handleId;
      updateConnectionDraft(
        sameSource
          ? null
          : {
              sourceNodeId: nodeId,
              sourceHandle: handleId,
              dataType,
            },
      );
    },
    [isCompact, toolMode, updateConnectionDraft],
  );
  const completeClickConnection = useCallback(
    (targetNodeId: string, targetHandle: string) => {
      const draft = connectionDraftRef.current;
      if (!draft) return;
      const result = addEdge({
        sourceNodeId: draft.sourceNodeId,
        sourceHandle: draft.sourceHandle,
        targetNodeId,
        targetHandle,
      });
      if (!result.ok) toast.error(result.reason);
      updateConnectionDraft(null);
    },
    [addEdge, updateConnectionDraft],
  );

  const projectedNodes = useMemo<CanvasFlowNode[]>(() => {
    const connectable = !isCompact || toolMode === "connect";
    const clickConnectionEnabled = canvasClickConnectionEnabled(
      isCompact,
      toolMode,
    );
    const editingEnabled = toolMode === "select" && connectionDraft === null;
    return graph.nodes.map((node) => {
      const dimensions = canvasFlowNodeDimensions(node);
      return {
        id: node.id,
        type: node.type,
        position: node.position,
        selected: selectedNodeIdSet.has(node.id),
        ariaLabel: `${CANVAS_NODE_SPECS[node.type].label}节点：${node.title}`,
        draggable: toolMode === "select",
        dragHandle: ".canvas-node-drag-handle",
        connectable,
        zIndex: canvasNodeZIndex(node.type),
        initialWidth: dimensions.width,
        initialHeight: dimensions.height,
        measured: measuredDimensions[node.id],
        style: {
          width: dimensions.width,
          height: dimensions.styleHeight,
        },
        data: {
          definition: node,
          execution: executions.get(node.id) ?? null,
          activeOutput: activeOutputs.get(node.id) ?? null,
          deliveryOutputs: canvasNodeDeliveryOutputs(
            graph,
            node,
            activeOutputs,
            document.recent_executions,
          ),
          connectionType: connectionDraft?.dataType ?? null,
          compatibleInputHandles:
            connectionCompatibility.handlesByNode.get(node.id) ?? [],
          onRun: onRunNode,
          onUpdateConfig: updateNodeConfig,
          onUpdateTitle: updateNodeTitle,
          onResizeStart: startFrameResize,
          onResizeEnd: commitFrameResize,
          onEditFocus: focusNodeEditor,
          onEditBlur: finishNodeEditor,
          onConfigEditStart: beginNodeConfigEdit,
          onConfigEditEnd: endNodeConfigEdit,
          onStartConnection: clickConnectionEnabled
            ? startClickConnection
            : undefined,
          onCompleteConnection: clickConnectionEnabled
            ? completeClickConnection
            : undefined,
          editingEnabled,
        },
      };
    });
  }, [
    activeOutputs,
    beginNodeConfigEdit,
    completeClickConnection,
    commitFrameResize,
    connectionDraft,
    connectionCompatibility.handlesByNode,
    document.recent_executions,
    endNodeConfigEdit,
    executions,
    finishNodeEditor,
    focusNodeEditor,
    graph,
    isCompact,
    measuredDimensions,
    onRunNode,
    selectedNodeIdSet,
    startFrameResize,
    startClickConnection,
    toolMode,
    updateNodeConfig,
    updateNodeTitle,
  ]);

  const flowNodes = useMemo(
    () =>
      projectedNodes.map((node) => {
        const transient = transientPositions[node.id];
        return transient
          ? { ...node, position: transient, dragging: true }
          : node;
      }),
    [projectedNodes, transientPositions],
  );

  const graphNodesById = useMemo(
    () => new Map(graph.nodes.map((node) => [node.id, node])),
    [graph.nodes],
  );
  const flowEdges = useMemo<Edge[]>(
    () =>
      graph.edges.map((edge) => ({
        id: edge.id,
        source: edge.source_node_id,
        sourceHandle: edge.source_handle,
        target: edge.target_node_id,
        targetHandle: edge.target_handle,
        selected: edge.id === selectedEdgeId,
        label: edge.role || undefined,
        ariaLabel: canvasEdgeAriaLabel(graphNodesById, edge),
        type: "smoothstep",
      })),
    [graph.edges, graphNodesById, selectedEdgeId],
  );

  const onNodesChange = useCallback(
    (changes: NodeChange<CanvasFlowNode>[]) => {
      const dimensionChanges = changes.filter(
        (
          change,
        ): change is Extract<
          NodeChange<CanvasFlowNode>,
          { type: "dimensions" }
        > => change.type === "dimensions" && Boolean(change.dimensions),
      );
      if (dimensionChanges.length > 0) {
        setMeasuredDimensions((current) => {
          let next = current;
          for (const change of dimensionChanges) {
            if (!change.dimensions) continue;
            if (change.resizing === false) {
              next = omitCanvasNodeMeasurements(next, [change.id]);
              continue;
            }
            const previous = next[change.id];
            if (
              previous?.width === change.dimensions.width &&
              previous.height === change.dimensions.height
            ) {
              continue;
            }
            if (next === current) next = { ...current };
            next[change.id] = { ...change.dimensions };
          }
          return next;
        });
      }
      const positionChanges = changes.filter(
        (
          change,
        ): change is Extract<
          NodeChange<CanvasFlowNode>,
          { type: "position" }
        > => change.type === "position" && Boolean(change.position),
      );
      const resizePositionChanges = positionChanges.filter((change) =>
        resizingNodeIdsRef.current.has(change.id),
      );
      const { transient, settled } =
        splitCanvasNodePositionChanges(
          positionChanges.filter(
            (change) => !resizingNodeIdsRef.current.has(change.id),
          ),
        );
      const resizeTransient = resizePositionChanges.flatMap((change) =>
        change.position
          ? [{ nodeId: change.id, position: change.position }]
          : [],
      );
      if (
        transient.length > 0 ||
        resizeTransient.length > 0 ||
        settled.length > 0
      ) {
        setTransientPositions((current) => {
          return updateCanvasTransientPositions(
            current,
            [...transient, ...resizeTransient],
            settled.map((item) => item.nodeId),
          );
        });
      }
      if (settled.length > 0) moveNodes(settled);
    },
    [moveNodes],
  );

  const isValidConnection = useCallback(
    (connection: Connection | Edge) => {
      if (
        !connection.source ||
        !connection.target ||
        !connection.sourceHandle ||
        !connection.targetHandle
      ) {
        return false;
      }
      return canvasConnectionIsValid(graph, {
        sourceNodeId: connection.source,
        sourceHandle: connection.sourceHandle,
        targetNodeId: connection.target,
        targetHandle: connection.targetHandle,
      });
    },
    [graph],
  );

  const addNodeWithFeedback = useCallback(
    (type: CanvasNodeType, position: CanvasPosition) => {
      const nodeId = addNode(type, position);
      if (!nodeId) {
        toast.error("画布已达到节点或存储大小上限");
      }
      return nodeId;
    },
    [addNode],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (cancelledConnectionRef.current) return;
      if (
        !connection.source ||
        !connection.target ||
        !connection.sourceHandle ||
        !connection.targetHandle
      ) {
        return;
      }
      const result = addEdge({
        sourceNodeId: connection.source,
        sourceHandle: connection.sourceHandle,
        targetNodeId: connection.target,
        targetHandle: connection.targetHandle,
      });
      if (!result.ok) toast.error(result.reason);
      updateConnectionDraft(null);
    },
    [addEdge, updateConnectionDraft],
  );

  const onConnectStart = useCallback(
    (_event: MouseEvent | TouchEvent, params: OnConnectStartParams) => {
      if (!params.nodeId || !params.handleId || params.handleType !== "source")
        return;
      cancelledConnectionRef.current = false;
      blurActiveCanvasEditor();
      connectionDropPositionRef.current = null;
      const node = graph.nodes.find((item) => item.id === params.nodeId);
      const port = node
        ? CANVAS_NODE_SPECS[node.type].outputs.find(
            (candidate) => candidate.id === params.handleId,
          )
        : null;
      if (!port) return;
      startInteraction();
      setTargetPickerOpen(false);
      updateConnectionDraft({
        sourceNodeId: params.nodeId,
        sourceHandle: params.handleId,
        dataType: port.dataType,
      });
    },
    [graph.nodes, startInteraction, updateConnectionDraft],
  );

  const handleSelectionChange = useCallback(
    ({ nodes, edges }: OnSelectionChangeParams<CanvasFlowNode, Edge>) => {
      if (edges.length > 0) {
        selectEdge(edges.at(-1)?.id ?? null);
        return;
      }
      selectNodes(nodes.map((node) => node.id));
    },
    [selectEdge, selectNodes],
  );

  const handleNodeClick = useCallback(
    (event: React.MouseEvent, node: CanvasFlowNode) => {
      if (event.shiftKey) {
        selectNodes(
          selectedNodeIdSet.has(node.id)
            ? selectedNodeIds.filter((nodeId) => nodeId !== node.id)
            : [...selectedNodeIds, node.id],
        );
        return;
      }
      selectNodes([node.id]);
    },
    [selectNodes, selectedNodeIdSet, selectedNodeIds],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      const type = event.dataTransfer.getData(
        "application/lumen-canvas-node",
      ) as CanvasNodeType;
      if (!type || !(type in CANVAS_NODE_SPECS) || !instance) return;
      const position = instance.screenToFlowPosition(
        {
          x: event.clientX,
          y: event.clientY,
        },
        {
          snapToGrid,
          snapGrid,
        },
      );
      addNodeWithFeedback(type, position);
    },
    [addNodeWithFeedback, instance, snapGrid, snapToGrid],
  );

  const createActionRequest = useCallback(
    (
      clientPosition: { x: number; y: number },
      trigger: CanvasViewportActionRequest["trigger"],
      draft: ConnectionDraft | null = connectionDraftRef.current,
      target: Pick<CanvasViewportActionRequest, "nodeId" | "edgeId"> = {},
    ): CanvasViewportActionRequest | null => {
      const current = instanceRef.current;
      if (!current) return null;
      return {
        position: current.screenToFlowPosition(clientPosition, {
          snapToGrid,
          snapGrid,
        }),
        clientPosition,
        trigger,
        connectionDraft: draft,
        ...target,
      };
    },
    [snapGrid, snapToGrid],
  );

  const openQuickAdd = useCallback(
    (request: CanvasViewportActionRequest, fallbackToPrompt = false) => {
      if (onOpenQuickAdd) {
        onOpenQuickAdd(request);
        return;
      }
      if (fallbackToPrompt) {
        addNodeWithFeedback("prompt", request.position);
      }
    },
    [addNodeWithFeedback, onOpenQuickAdd],
  );

  const handlePaneClick = useCallback(
    (event: React.MouseEvent | MouseEvent) => {
      selectNode(null);
      selectEdge(null);
      const suppressDraftReset = suppressPaneClickRef.current;
      suppressPaneClickRef.current = false;
      if (!suppressDraftReset) updateConnectionDraft(null);
      if (event.detail !== 2) return;
      const request = createActionRequest(
        { x: event.clientX, y: event.clientY },
        "pane-double-click",
      );
      if (request) openQuickAdd(request, true);
    },
    [
      createActionRequest,
      openQuickAdd,
      selectEdge,
      selectNode,
      updateConnectionDraft,
    ],
  );

  const handlePaneContextMenu = useCallback(
    (event: React.MouseEvent | MouseEvent) => {
      if (!onOpenContextMenu) return;
      event.preventDefault();
      const request = createActionRequest(
        { x: event.clientX, y: event.clientY },
        "pane-context-menu",
      );
      if (request) onOpenContextMenu(request);
    },
    [createActionRequest, onOpenContextMenu],
  );

  const handleNodeContextMenu = useCallback(
    (event: React.MouseEvent, node: CanvasFlowNode) => {
      if (!onOpenContextMenu) return;
      event.preventDefault();
      selectNodes([node.id]);
      const request = createActionRequest(
        { x: event.clientX, y: event.clientY },
        "node-context-menu",
        connectionDraftRef.current,
        { nodeId: node.id },
      );
      if (request) onOpenContextMenu(request);
    },
    [createActionRequest, onOpenContextMenu, selectNodes],
  );

  const handleEdgeContextMenu = useCallback(
    (event: React.MouseEvent, edge: Edge) => {
      if (!onOpenContextMenu) return;
      event.preventDefault();
      selectEdge(edge.id);
      const request = createActionRequest(
        { x: event.clientX, y: event.clientY },
        "edge-context-menu",
        connectionDraftRef.current,
        { edgeId: edge.id },
      );
      if (request) onOpenContextMenu(request);
    },
    [createActionRequest, onOpenContextMenu, selectEdge],
  );

  const handleConnectEnd = useCallback<OnConnectEnd>(
    (event, connectionState) => {
      try {
        if (cancelledConnectionRef.current) {
          cancelledConnectionRef.current = false;
          connectionDropPositionRef.current = null;
          updateConnectionDraft(null);
          return;
        }
        if (connectionState.isValid) {
          connectionDropPositionRef.current = null;
          return;
        }
        const draft = connectionDraftRef.current;
        const clientPosition = pointerClientPosition(event);
        if (draft && connectionState.toNode === null && clientPosition) {
          const request = createActionRequest(
            clientPosition,
            "connection-drop",
            draft,
          );
          if (request) {
            connectionDropPositionRef.current = request.position;
            suppressPaneClickRef.current = true;
            if (suppressPaneClickTimerRef.current !== null) {
              window.clearTimeout(suppressPaneClickTimerRef.current);
            }
            suppressPaneClickTimerRef.current = window.setTimeout(() => {
              suppressPaneClickRef.current = false;
              suppressPaneClickTimerRef.current = null;
            }, 0);
            onOpenQuickAdd?.(request);
            return;
          }
        }
        if (!isCompact || toolMode !== "connect") {
          window.setTimeout(() => updateConnectionDraft(null), 0);
        }
      } finally {
        finishInteraction();
      }
    },
    [
      createActionRequest,
      finishInteraction,
      isCompact,
      onOpenQuickAdd,
      toolMode,
      updateConnectionDraft,
    ],
  );

  const handleEmptyQuickAdd = useCallback(() => {
    const bounds = flowViewportBounds(viewportRef.current);
    if (!bounds) return;
    const request = createActionRequest(
      {
        x: bounds.left + bounds.width / 2,
        y: bounds.top + bounds.height / 2,
      },
      "empty-state",
      null,
    );
    if (request) openQuickAdd(request, true);
  }, [createActionRequest, openQuickAdd]);

  const handleInit = useCallback(
    (next: ReactFlowInstance<CanvasFlowNode, Edge>) => {
      instanceRef.current = next;
      setInstance(next);
      onReady?.({
        fitView: () => fitCanvasViewport(next, viewportPreferencesRef.current),
        fitSelection: (nodeIds) => {
          const ids = nodeIds ?? viewportPreferencesRef.current.selectedNodeIds;
          const nodes = ids
            .map((nodeId) => next.getNode(nodeId))
            .filter((node): node is CanvasFlowNode => Boolean(node));
          if (nodes.length === 0) return;
          fitCanvasViewport(
            next,
            viewportPreferencesRef.current,
            nodes,
            0.26,
            1.2,
          );
        },
        focusNode: (nodeId) =>
          focusCanvasNode(next, nodeId, viewportPreferencesRef.current),
        zoomIn: () => {
          void next.zoomIn({
            duration: viewportAnimationDuration(
              viewportPreferencesRef.current.reducedMotion,
            ),
          });
        },
        zoomOut: () => {
          void next.zoomOut({
            duration: viewportAnimationDuration(
              viewportPreferencesRef.current.reducedMotion,
            ),
          });
        },
        resetZoom: () => {
          void next.zoomTo(1, {
            duration: viewportAnimationDuration(
              viewportPreferencesRef.current.reducedMotion,
            ),
          });
        },
        toggleMiniMap: () => setMiniMapVisible((current) => !current),
        getZoom: () => next.getZoom(),
        getViewportCenter: () => {
          const bounds = flowViewportBounds(viewportRef.current);
          if (!bounds) return { x: 0, y: 0 };
          return next.screenToFlowPosition({
            x: bounds.left + bounds.width / 2,
            y: bounds.top + bounds.height / 2,
          });
        },
      });
    },
    [onReady],
  );

  const cancelDomainInteraction = useCallback(() => {
    updateConnectionDraft(null);
    setTargetPickerOpen(false);
    resizingNodeIdsRef.current.clear();
    clearTransientNodeState();
    finishInteraction();
  }, [clearTransientNodeState, finishInteraction, updateConnectionDraft]);

  const handleTouchCancel = useCallback(
    (event: React.TouchEvent<HTMLDivElement>) => {
      cancelledConnectionRef.current = true;
      event.currentTarget.ownerDocument.dispatchEvent(
        new Event("touchend", { bubbles: true, cancelable: true }),
      );
      cancelDomainInteraction();
    },
    [cancelDomainInteraction],
  );

  useEffect(
    () => () => {
      editorFocusRequestRef.current += 1;
      instanceRef.current = null;
      if (suppressPaneClickTimerRef.current !== null) {
        window.clearTimeout(suppressPaneClickTimerRef.current);
      }
      resizingNodeIdsRef.current.clear();
      if (interactionActiveRef.current) endInteraction();
    },
    [endInteraction],
  );

  useEffect(() => {
    if (toolMode !== "select" || connectionDraft) {
      editorFocusRequestRef.current += 1;
    }
    if (!connectionDraft) connectionDropPositionRef.current = null;
  }, [connectionDraft, toolMode]);

  return (
    <div
      ref={viewportRef}
      className={styles.viewport}
      onDrop={handleDrop}
      onPointerCancelCapture={() => {
        cancelledConnectionRef.current = true;
      }}
      onTouchCancelCapture={() => {
        cancelledConnectionRef.current = true;
      }}
      onPointerCancel={(event) => {
        if (event.pointerType === "touch") return;
        cancelDomainInteraction();
      }}
      onTouchCancel={handleTouchCancel}
      onDragOver={(event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
    >
      <ReactFlow<CanvasFlowNode, Edge>
        aria-label="无限画布编辑区"
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={canvasNodeTypes}
        onInit={handleInit}
        onNodesChange={onNodesChange}
        onSelectionChange={handleSelectionChange}
        onNodeClick={handleNodeClick}
        onNodeContextMenu={handleNodeContextMenu}
        onEdgeClick={(_event, edge) => {
          selectEdge(edge.id);
          if (isCompact) onOpenInspector?.();
        }}
        onEdgeContextMenu={handleEdgeContextMenu}
        onPaneClick={handlePaneClick}
        onPaneContextMenu={handlePaneContextMenu}
        onNodeDragStart={startInteraction}
        onNodeDragStop={(_event, _node, nodes) => finishInteraction(nodes)}
        onSelectionDragStart={startInteraction}
        onSelectionDragStop={(_event, nodes) => finishInteraction(nodes)}
        onBeforeDelete={async ({ nodes, edges }) => {
          if (
            interactionActiveRef.current ||
            resizingNodeIdsRef.current.size > 0 ||
            connectionDraftRef.current
          ) {
            return false;
          }
          removeElements(
            nodes.map((node) => node.id),
            edges.map((edge) => edge.id),
          );
          return false;
        }}
        onConnect={onConnect}
        onConnectStart={onConnectStart}
        onConnectEnd={handleConnectEnd}
        onMove={(_event, viewport) => {
          setZoom((current) =>
            Math.abs(current - viewport.zoom) < 0.001
              ? current
              : viewport.zoom,
          );
        }}
        isValidConnection={isValidConnection}
        minZoom={minimumZoom}
        maxZoom={CANVAS_MAX_ZOOM}
        snapToGrid={snapToGrid}
        snapGrid={snapGrid}
        onlyRenderVisibleElements
        elevateNodesOnSelect={false}
        deleteKeyCode={["Backspace", "Delete"]}
        panOnDrag={canvasPanOnDrag(isCompact, toolMode)}
        panActivationKeyCode="Space"
        nodesDraggable={toolMode === "select"}
        nodesConnectable={canvasNodesConnectable(isCompact, toolMode)}
        connectOnClick={false}
        selectionOnDrag={!isCompact && toolMode === "select"}
        selectionKeyCode="Shift"
        multiSelectionKeyCode="Shift"
        zoomOnPinch
        zoomOnScroll={!isCompact}
        zoomOnDoubleClick={false}
        ariaLabelConfig={CANVAS_ARIA_LABEL_CONFIG}
        proOptions={{ hideAttribution: true }}
      >
        {snapToGrid ? (
          <Background
            variant={BackgroundVariant.Dots}
            gap={canvasGridGap(snapGrid[0])}
            size={1}
            color="var(--border)"
          />
        ) : null}
        {showMiniMap ? (
          <MiniMap
            className={styles.miniMap}
            pannable
            zoomable
            nodeColor="var(--fg-2)"
            maskColor="color-mix(in srgb, var(--bg-0) 78%, transparent)"
          />
        ) : null}
      </ReactFlow>
      {!isCompact && instance ? (
        <CanvasViewportControls
          zoom={zoom}
          minZoom={minimumZoom}
          maxZoom={CANVAS_MAX_ZOOM}
          onZoomOut={() => {
            void instance.zoomOut({
              duration: viewportAnimationDuration(reducedMotion),
            });
          }}
          onZoomIn={() => {
            void instance.zoomIn({
              duration: viewportAnimationDuration(reducedMotion),
            });
          }}
          onResetZoom={() => {
            void instance.zoomTo(1, {
              duration: viewportAnimationDuration(reducedMotion),
            });
          }}
          onFitView={() =>
            fitCanvasViewport(instance, viewportPreferencesRef.current)
          }
          gridVisible={snapToGrid}
          onGridVisibleChange={(visible) =>
            updateDocumentSettings({ snap_to_grid: visible })
          }
          minimapVisible={miniMapVisible}
          onMinimapVisibleChange={setMiniMapVisible}
          className="absolute bottom-3 left-3 z-[var(--z-tabbar)]"
        />
      ) : null}
      {graph.nodes.length === 0 ? (
        <CanvasEmptyState onCreate={handleEmptyQuickAdd} />
      ) : null}
      {isCompact && connectionDraft ? (
        <MobileConnectTargets
          open={targetPickerOpen}
          targets={connectionCompatibility.targets}
          onOpen={() => setTargetPickerOpen(true)}
          onClose={() => setTargetPickerOpen(false)}
          onCancel={() => {
            setTargetPickerOpen(false);
            updateConnectionDraft(null);
          }}
          onSelect={(target) => {
            void instance?.setCenter(target.x, target.y, {
              zoom: 1,
              duration: viewportAnimationDuration(reducedMotion),
            });
            const draft = connectionDraftRef.current;
            if (!draft) return;
            const result = addEdge({
              sourceNodeId: draft.sourceNodeId,
              sourceHandle: draft.sourceHandle,
              targetNodeId: target.nodeId,
              targetHandle: target.handleId,
            });
            if (!result.ok) {
              toast.error(result.reason);
              return;
            }
            setTargetPickerOpen(false);
            updateConnectionDraft(null);
          }}
        />
      ) : null}
    </div>
  );
}

function canvasMinimumZoom(isCompact: boolean): number {
  return isCompact ? COMPACT_MIN_ZOOM : DESKTOP_MIN_ZOOM;
}

function buildConnectionCompatibility(
  graph: CanvasGraph,
  draft: ConnectionDraft | null,
): ConnectionCompatibility {
  const handlesByNode = new Map<string, string[]>();
  const targets: ConnectionCompatibility["targets"] = [];
  if (!draft) return { handlesByNode, targets };
  const candidateId = canvasConnectionCandidateId(graph);

  for (const node of graph.nodes) {
    const handles: string[] = [];
    const dimensions = canvasFlowNodeDimensions(node);
    for (const port of CANVAS_NODE_SPECS[node.type].inputs) {
      const valid = canvasConnectionIsValid(
        graph,
        {
          sourceNodeId: draft.sourceNodeId,
          sourceHandle: draft.sourceHandle,
          targetNodeId: node.id,
          targetHandle: port.id,
        },
        port.dataType,
        candidateId,
      );
      if (!valid) continue;
      handles.push(port.id);
      targets.push({
        key: `${node.id}:${port.id}`,
        nodeId: node.id,
        nodeTitle: node.title,
        nodeType: CANVAS_NODE_SPECS[node.type].label,
        handleId: port.id,
        handleLabel: port.label,
        x: node.position.x + dimensions.width / 2,
        y: node.position.y + dimensions.height / 2,
      });
    }
    if (handles.length > 0) handlesByNode.set(node.id, handles);
  }
  return { handlesByNode, targets };
}

function canvasConnectionIsValid(
  graph: CanvasGraph,
  input: CanvasConnectionInput,
  targetDataType?: CanvasDataType,
  candidateId = canvasConnectionCandidateId(graph),
): boolean {
  const resolvedTargetDataType =
    targetDataType ?? canvasConnectionTargetDataType(graph, input);
  if (!resolvedTargetDataType) return false;

  const candidate: CanvasEdgeDefinition = {
    id: candidateId,
    source_node_id: input.sourceNodeId,
    source_handle: input.sourceHandle,
    target_node_id: input.targetNodeId,
    target_handle: input.targetHandle,
    data_type: resolvedTargetDataType,
    binding_mode: "follow_active",
  };
  return validateCanvasConnections(graph, [candidate]).valid;
}

function canvasConnectionTargetDataType(
  graph: CanvasGraph,
  input: CanvasConnectionInput,
): CanvasDataType | null {
  const targetNode = graph.nodes.find((node) => node.id === input.targetNodeId);
  const targetPort = targetNode
    ? CANVAS_NODE_SPECS[targetNode.type].inputs.find(
        (port) => port.id === input.targetHandle,
      )
    : null;
  return targetPort?.dataType ?? null;
}

function canvasConnectionCandidateId(graph: CanvasGraph): string {
  let id = "__canvas_connection_candidate__";
  const existingIds = new Set(graph.edges.map((edge) => edge.id));
  while (existingIds.has(id)) id += "_";
  return id;
}
