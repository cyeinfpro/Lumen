"use client";

import {
  type Connection,
  type Edge,
  type NodeChange,
  type OnConnectStartParams,
  type ReactFlowInstance,
  type ReactFlowProps,
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
import {
  CANVAS_NODE_SPECS,
  findCanvasNodeCatalogItem,
  findMatchingCanvasNodeCatalogItem,
  isCanvasNodeType,
  type CanvasNodeCreateOverrides,
} from "@/lib/canvas/registry";
import type {
  CanvasDataType,
  CanvasGraph,
  CanvasNodeDefinition,
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
  canvasFlowNodeDimensions,
  canvasNodeDeliveryOutputs,
  canvasNodesConnectable,
  canvasPanOnDrag,
  omitCanvasNodeMeasurements,
  shouldShowMiniMap,
  viewportAnimationDuration,
  type CanvasNodeGeometry,
  type CanvasViewportPreferences,
  type ConnectionCompatibility,
} from "./CanvasViewportModel";
import {
  buildCanvasConnectionCompatibility,
  createCanvasConnectionCandidate,
} from "./CanvasViewportConnections";
import { CanvasViewportSurface } from "./CanvasViewportSurface";
import type { CanvasFlowNode } from "./nodes/CanvasNodes";
import { useCanvasViewportActions } from "./useCanvasViewportActions";
import { useCanvasViewportDomainInteraction } from "./useCanvasViewportDomainInteraction";
import {
  useCanvasViewportProjection,
  type CanvasNodeProjectionContext,
} from "./useCanvasViewportProjection";
import type { CanvasViewportApi, CanvasViewportProps } from "./CanvasViewportTypes";

export type {
  CanvasViewportActionRequest,
  CanvasViewportApi,
  CanvasViewportMotionOptions,
  CanvasViewportProps,
} from "./CanvasViewportTypes";

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
  const beginNodeConfigEdit = useCanvasStore((state) => state.beginNodeConfigEdit);
  const endNodeConfigEdit = useCanvasStore((state) => state.endNodeConfigEdit);
  const moveNodes = useCanvasStore((state) => state.moveNodes);
  const removeElements = useCanvasStore((state) => state.removeElements);
  const addEdge = useCanvasStore((state) => state.addEdge);
  const addNode = useCanvasStore((state) => state.addNode);
  const setConnectionDraft = useCanvasStore((state) => state.setConnectionDraft);
  const beginInteraction = useCanvasStore((state) => state.beginInteraction);
  const endInteraction = useCanvasStore((state) => state.endInteraction);
  const resizeNode = useCanvasStore((state) => state.resizeNode);
  const updateDocumentSettings = useCanvasStore((state) => state.updateDocumentSettings);
  const isMobile = useMediaQuery("(max-width: 767px)") !== false;
  const isTablet =
    useMediaQuery("(min-width: 768px) and (max-width: 1199px)") === true;
  const reducedMotion = Boolean(useReducedMotion());
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const interactionActiveRef = useRef(false);
  const cancelledConnectionRef = useRef(false);
  const cancelledResizeRef = useRef(false);
  const resizingNodeIdsRef = useRef(new Set<string>());
  const connectionDraftRef = useRef(connectionDraft);
  const instanceRef =
    useRef<ReactFlowInstance<CanvasFlowNode, Edge> | null>(null);
  const editorFocusRequestRef = useRef(0);
  const [instance, setInstance] =
    useState<ReactFlowInstance<CanvasFlowNode, Edge> | null>(null);
  const [targetPickerOpen, setTargetPickerOpen] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [miniMapVisible, setMiniMapVisible] = useState(
    () =>
      graph.nodes.length + Math.ceil(graph.edges.length / 2) >=
      MINIMAP_NODE_THRESHOLD,
  );
  const selectedNodeIdSet = useMemo(() => new Set(selectedNodeIds), [selectedNodeIds]);
  const snapToGrid = graph.settings.snap_to_grid;
  const snapGrid = useMemo<[number, number]>(() => {
    const gridSize = Math.max(1, Math.round(graph.settings.grid_size));
    return [gridSize, gridSize];
  }, [graph.settings.grid_size]);
  const minimumZoom = isMobile ? COMPACT_MIN_ZOOM : DESKTOP_MIN_ZOOM;
  const showMiniMap = shouldShowMiniMap(
    isMobile,
    miniMapVisible,
    graph.nodes.length,
  );
  const viewportPreferencesRef = useRef<CanvasViewportPreferences>({
    isMobile,
    reducedMotion,
    selectedNodeIds,
  });

  useEffect(() => {
    viewportPreferencesRef.current = {
      isMobile,
      reducedMotion,
      selectedNodeIds,
    };
  }, [isMobile, reducedMotion, selectedNodeIds]);

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

  const {
    cancelDomainInteraction,
    clearTransientNodeState,
    finishInteraction,
    markInteractionCancelled,
    measuredDimensions,
    setMeasuredDimensions,
    setTransientPositions,
    startInteraction,
    transientPositions,
  } = useCanvasViewportDomainInteraction({
    interactionActiveRef,
    cancelledConnectionRef,
    cancelledResizeRef,
    connectionDraft,
    editorFocusRequestRef,
    instanceRef,
    resizingNodeIdsRef,
    beginInteraction,
    endInteraction,
    moveNodes,
    setTargetPickerOpen,
    toolMode,
    updateConnectionDraft,
  });

  const startFrameResize = useCallback(
    (nodeId: string) => {
      if (!resizingNodeIdsRef.current.has(nodeId)) {
        cancelledResizeRef.current = false;
      }
      resizingNodeIdsRef.current.add(nodeId);
      startInteraction();
    },
    [startInteraction],
  );

  const commitFrameResize = useCallback(
    (nodeId: string, geometry: CanvasNodeGeometry) => {
      const cancelled = cancelledResizeRef.current;
      cancelledResizeRef.current = false;
      if (!cancelled) {
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

  const connectionCompatibility = useMemo(
    () => buildConnectionCompatibility(graph, connectionDraft),
    [connectionDraft, graph],
  );
  const startClickConnection = useCallback(
    (nodeId: string, handleId: string, dataType: CanvasDataType) => {
      if (isMobile && toolMode !== "connect") return;
      if (!isMobile && toolMode !== "select") return;
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
    [isMobile, toolMode, updateConnectionDraft],
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

  const projectNode = useCallback(
    (
      node: CanvasNodeDefinition,
      projection: CanvasNodeProjectionContext,
    ): CanvasFlowNode => {
      const dimensions = canvasFlowNodeDimensions(node);
      const preset = findMatchingCanvasNodeCatalogItem(node);
      const clickConnectionEnabled = canvasClickConnectionEnabled(
        isMobile,
        toolMode,
      );
      return {
        id: node.id,
        type: node.type,
        position: node.position,
        selected: selectedNodeIdSet.has(node.id),
        ariaLabel: `${preset?.label ?? CANVAS_NODE_SPECS[node.type].label}节点：${node.title}`,
        draggable: toolMode === "select",
        dragHandle: ".canvas-node-drag-handle",
        connectable: !isMobile || toolMode === "connect",
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
          execution: projection.execution,
          activeOutput: projection.activeOutput,
          deliveryOutputs: canvasNodeDeliveryOutputs(
            graph,
            node,
            projection.activeOutputs,
            document.recent_executions,
          ),
          resolvedText: projection.resolvedText,
          inputCounts: projection.inputCounts,
          runDisabledReason: projection.runDisabledReason,
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
          editingEnabled:
            toolMode === "select" && connectionDraft === null,
        },
      };
    },
    [
      beginNodeConfigEdit,
      commitFrameResize,
      completeClickConnection,
      connectionCompatibility.handlesByNode,
      connectionDraft,
      document.recent_executions,
      endNodeConfigEdit,
      finishNodeEditor,
      focusNodeEditor,
      graph,
      isMobile,
      measuredDimensions,
      onRunNode,
      selectedNodeIdSet,
      startClickConnection,
      startFrameResize,
      toolMode,
      updateNodeConfig,
      updateNodeTitle,
    ],
  );
  const { flowEdges, flowNodes } = useCanvasViewportProjection({
    document,
    graph,
    selectedEdgeId,
    transientPositions,
    projectNode,
  });

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
    [moveNodes, setMeasuredDimensions, setTransientPositions],
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
    (
      type: CanvasNodeType,
      position: CanvasPosition,
      overrides?: CanvasNodeCreateOverrides,
    ) => {
      const nodeId = addNode(type, position, overrides);
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

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      const catalogId = event.dataTransfer.getData(
        "application/lumen-canvas-node",
      );
      const catalogItem = findCanvasNodeCatalogItem(catalogId);
      const type = catalogItem?.type ?? (isCanvasNodeType(catalogId) ? catalogId : null);
      if (!type || !instance) return;
      const catalogOverrides = catalogItem
        ? {
            ...catalogItem.overrides,
            ui: {
              ...(catalogItem.overrides?.ui ?? {}),
              preset_id: catalogItem.id,
            },
          }
        : undefined;
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
      addNodeWithFeedback(
        type,
        position,
        catalogOverrides,
      );
    },
    [addNodeWithFeedback, instance, snapGrid, snapToGrid],
  );

  const addPromptNode = useCallback(
    (position: CanvasPosition) => {
      addNodeWithFeedback("prompt", position);
    },
    [addNodeWithFeedback],
  );
  const handleReady = useCallback(
    (api: CanvasViewportApi) => {
      const { getViewportCenter } = api;
      onReady?.({ ...api, getViewportCenter });
    },
    [onReady],
  );
  const {
    handleConnectEnd,
    handleEdgeContextMenu,
    handleEmptyQuickAdd,
    handleInit,
    handleNodeClick,
    handleNodeContextMenu,
    handlePaneClick,
    handlePaneContextMenu,
    handleSelectionChange,
  } = useCanvasViewportActions({
    addPromptNode,
    cancelledConnectionRef,
    connectionDraft,
    connectionDraftRef,
    finishInteraction,
    instanceRef,
    isMobile,
    onOpenContextMenu,
    onOpenQuickAdd,
    onReady: handleReady,
    selectEdge,
    selectNode,
    selectNodes,
    selectedNodeIds,
    selectedNodeIdSet,
    setInstance,
    setMiniMapVisible,
    snapGrid,
    snapToGrid,
    toolMode,
    updateConnectionDraft,
    viewportPreferencesRef,
    viewportRef,
  });

  const handleTouchCancel = useCallback(
    (event: React.TouchEvent<HTMLDivElement>) => {
      markInteractionCancelled();
      event.currentTarget.ownerDocument.dispatchEvent(
        new Event("touchend", { bubbles: true, cancelable: true }),
      );
      cancelDomainInteraction();
    },
    [cancelDomainInteraction, markInteractionCancelled],
  );

  const flowProps = {
    nodes: flowNodes,
    edges: flowEdges,
    onInit: handleInit,
    onNodesChange,
    onSelectionChange: handleSelectionChange,
    onNodeClick: handleNodeClick,
    onNodeContextMenu: handleNodeContextMenu,
    onEdgeClick: (_event, edge) => {
      selectEdge(edge.id);
      if (isMobile) onOpenInspector?.();
    },
    onEdgeContextMenu: handleEdgeContextMenu,
    onPaneClick: handlePaneClick,
    onPaneContextMenu: handlePaneContextMenu,
    onNodeDragStart: startInteraction,
    onNodeDragStop: (_event, _node, nodes) => finishInteraction(nodes),
    onSelectionDragStart: startInteraction,
    onSelectionDragStop: (_event, nodes) => finishInteraction(nodes),
    onBeforeDelete: async ({ nodes, edges }) => {
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
    },
    onConnect,
    onConnectStart,
    onConnectEnd: handleConnectEnd,
    onMove: (_event, viewport) => {
      setZoom((current) =>
        Math.abs(current - viewport.zoom) < 0.001
          ? current
          : viewport.zoom,
      );
    },
    isValidConnection,
    minZoom: minimumZoom,
    maxZoom: CANVAS_MAX_ZOOM,
    snapToGrid,
    snapGrid,
    deleteKeyCode: ["Backspace", "Delete"],
    panOnDrag: canvasPanOnDrag(isMobile, toolMode),
    panActivationKeyCode: "Space",
    nodesDraggable: toolMode === "select",
    nodesConnectable: canvasNodesConnectable(isMobile, toolMode),
    selectionOnDrag: !isMobile && toolMode === "select",
    selectionKeyCode: "Shift",
    multiSelectionKeyCode: "Shift",
    zoomOnPinch: true,
    zoomOnScroll: !isMobile,
    zoomOnDoubleClick: false,
    proOptions: { hideAttribution: true },
  } satisfies ReactFlowProps<CanvasFlowNode, Edge>;

  return (
    <CanvasViewportSurface
      aria-label="无限画布编辑区"
      ariaLabelConfig={CANVAS_ARIA_LABEL_CONFIG}
      connectOnClick={false}
      elevateNodesOnSelect={false}
      flowProps={flowProps}
      onlyRenderVisibleElements
      viewportRef={viewportRef}
      onDrop={handleDrop}
      onPointerCancelCapture={markInteractionCancelled}
      onTouchCancelCapture={markInteractionCancelled}
      onPointerCancel={(event) => {
        if (event.pointerType === "touch") return;
        cancelDomainInteraction();
      }}
      onTouchCancel={handleTouchCancel}
      snapToGrid={snapToGrid}
      snapGrid={snapGrid}
      showMiniMap={showMiniMap}
      isMobile={isMobile}
      instance={instance}
      zoom={zoom}
      minimumZoom={minimumZoom}
      maximumZoom={CANVAS_MAX_ZOOM}
      reducedMotion={reducedMotion}
      isTablet={isTablet}
      viewportPreferencesRef={viewportPreferencesRef}
      onGridVisibleChange={(visible) =>
        updateDocumentSettings({ snap_to_grid: visible })
      }
      miniMapVisible={miniMapVisible}
      onMiniMapVisibleChange={setMiniMapVisible}
      nodeCount={graph.nodes.length}
      onEmptyQuickAdd={handleEmptyQuickAdd}
      connectionDraft={connectionDraft}
      targetPickerOpen={targetPickerOpen}
      targets={connectionCompatibility.targets}
      onTargetPickerOpenChange={setTargetPickerOpen}
      connectionDraftRef={connectionDraftRef}
      addEdge={addEdge}
      updateConnectionDraft={updateConnectionDraft}
    />
  );
}

function buildConnectionCompatibility(
  graph: CanvasGraph,
  draft: ConnectionDraft | null,
): ConnectionCompatibility {
  return buildCanvasConnectionCompatibility(graph, draft, {
    isValid: (input, targetDataType) =>
      canvasConnectionIsValid(graph, input, targetDataType),
    targetPosition: (node) => {
      const dimensions = canvasFlowNodeDimensions(node);
      return {
        x: node.position.x + dimensions.width / 2,
        y: node.position.y + dimensions.height / 2,
      };
    },
  });
}

function canvasConnectionIsValid(
  graph: CanvasGraph,
  input: CanvasConnectionInput,
  targetDataType?: CanvasDataType,
): boolean {
  const candidate = createCanvasConnectionCandidate(
    graph,
    input,
    targetDataType,
  );
  return candidate
    ? validateCanvasConnections(graph, [candidate]).valid
    : false;
}
