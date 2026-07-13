"use client";

import {
  Background,
  BackgroundVariant,
  MiniMap,
  ReactFlow,
  type Connection,
  type Edge,
  type NodeChange,
  type OnConnectStartParams,
  type OnSelectionChangeParams,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Cable, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useMediaQuery } from "@/hooks/useMediaQuery";
import {
  blurActiveCanvasEditor,
  canvasNodeZIndex,
  splitCanvasNodePositionChanges,
  updateCanvasTransientPositions,
} from "@/lib/canvas/interaction";
import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import {
  activeOutputsByNode,
  deliveryOutputsForNode,
  latestExecutionsByNode,
} from "@/lib/canvas/runtime";
import { validateCanvasConnection } from "@/lib/canvas/graph";
import type {
  CanvasDataType,
  CanvasDocument,
  CanvasGraph,
  CanvasNodeDefinition,
  CanvasNodeExecution,
  CanvasNodeType,
  CanvasOutput,
  ConnectionDraft,
} from "@/lib/canvas/types";
import { toast } from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import { useCanvasStore } from "./CanvasStoreProvider";
import {
  canvasNodeTypes,
  type CanvasFlowNode,
} from "./nodes/CanvasNodes";
import styles from "./canvas.module.css";

export interface CanvasViewportApi {
  fitView: () => void;
  getViewportCenter: () => { x: number; y: number };
}

export function CanvasViewport({
  document,
  onRunNode,
  onReady,
  onOpenInspector,
}: {
  document: CanvasDocument;
  onRunNode: (nodeId: string) => void;
  onReady?: (api: CanvasViewportApi) => void;
  onOpenInspector?: () => void;
}) {
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
  const setConnectionDraft = useCanvasStore((state) => state.setConnectionDraft);
  const beginInteraction = useCanvasStore((state) => state.beginInteraction);
  const endInteraction = useCanvasStore((state) => state.endInteraction);
  const isCompact = useMediaQuery("(max-width: 1199px)") !== false;
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const interactionActiveRef = useRef(false);
  const instanceRef =
    useRef<ReactFlowInstance<CanvasFlowNode, Edge> | null>(null);
  const editorFocusRequestRef = useRef(0);
  const [instance, setInstance] =
    useState<ReactFlowInstance<CanvasFlowNode, Edge> | null>(null);
  const [targetPickerOpen, setTargetPickerOpen] = useState(false);
  const [transientPositions, setTransientPositions] = useState<
    Record<string, { x: number; y: number }>
  >({});
  const [measuredDimensions, setMeasuredDimensions] = useState<
    Record<string, { width: number; height: number }>
  >({});

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
          duration: 220,
        });
      };
      zoomWhenReady();
    },
    [beginNodeEdit, selectNode],
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
  const compatibleTargets = useMemo(
    () => listCompatibleTargets(graph, connectionDraft),
    [connectionDraft, graph],
  );
  const startClickConnection = useCallback(
    (nodeId: string, handleId: string, dataType: CanvasDataType) => {
      if (!isCompact || toolMode !== "connect") return;
      blurActiveCanvasEditor();
      setTargetPickerOpen(false);
      const sameSource =
        connectionDraft?.sourceNodeId === nodeId &&
        connectionDraft.sourceHandle === handleId;
      setConnectionDraft(
        sameSource
          ? null
          : {
              sourceNodeId: nodeId,
              sourceHandle: handleId,
              dataType,
            },
      );
    },
    [connectionDraft, isCompact, setConnectionDraft, toolMode],
  );

  const projectedNodes = useMemo<CanvasFlowNode[]>(
    () => {
      const connectable = !isCompact || toolMode === "connect";
      const editingEnabled =
        toolMode === "select" && connectionDraft === null;
      return graph.nodes.map((node) => {
        const dimensions = canvasFlowNodeDimensions(node);
        return {
          id: node.id,
          type: node.type,
          position: node.position,
          selected: selectedNodeIds.includes(node.id),
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
            compatibleInputHandles: compatibleInputHandlesForNode(
              graph,
              node,
              connectionDraft,
            ),
            onRun: onRunNode,
            onUpdateConfig: updateNodeConfig,
            onUpdateTitle: updateNodeTitle,
            onEditFocus: focusNodeEditor,
            onEditBlur: finishNodeEditor,
            onConfigEditStart: beginNodeConfigEdit,
            onConfigEditEnd: endNodeConfigEdit,
            onStartConnection:
              isCompact && toolMode === "connect"
                ? startClickConnection
                : undefined,
            editingEnabled,
          },
        };
      });
    },
    [
      activeOutputs,
      beginNodeConfigEdit,
      connectionDraft,
      document.recent_executions,
      endNodeConfigEdit,
      executions,
      finishNodeEditor,
      focusNodeEditor,
      graph,
      isCompact,
      measuredDimensions,
      onRunNode,
      selectedNodeIds,
      startClickConnection,
      toolMode,
      updateNodeConfig,
      updateNodeTitle,
    ],
  );

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
        type: "smoothstep",
      })),
    [graph.edges, selectedEdgeId],
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
        ): change is Extract<NodeChange<CanvasFlowNode>, { type: "position" }> =>
          change.type === "position" && Boolean(change.position),
      );
      const { transient, settled } =
        splitCanvasNodePositionChanges(positionChanges);
      if (transient.length > 0 || settled.length > 0) {
        setTransientPositions((current) => {
          return updateCanvasTransientPositions(
            current,
            transient,
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
      return validateCanvasConnection(graph, {
        sourceNodeId: connection.source,
        sourceHandle: connection.sourceHandle,
        targetNodeId: connection.target,
        targetHandle: connection.targetHandle,
      }).valid;
    },
    [graph],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
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
      setConnectionDraft(null);
    },
    [addEdge, setConnectionDraft],
  );

  const onConnectStart = useCallback(
    (_event: MouseEvent | TouchEvent, params: OnConnectStartParams) => {
      if (!params.nodeId || !params.handleId || params.handleType !== "source") return;
      blurActiveCanvasEditor();
      const node = graph.nodes.find((item) => item.id === params.nodeId);
      const port = node
        ? CANVAS_NODE_SPECS[node.type].outputs.find(
            (candidate) => candidate.id === params.handleId,
          )
        : null;
      if (!port) return;
      setTargetPickerOpen(false);
      setConnectionDraft({
        sourceNodeId: params.nodeId,
        sourceHandle: params.handleId,
        dataType: port.dataType,
      });
    },
    [graph.nodes, setConnectionDraft],
  );

  const handleSelectionChange = useCallback(
    ({
      nodes,
      edges,
    }: OnSelectionChangeParams<CanvasFlowNode, Edge>) => {
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
          selectedNodeIds.includes(node.id)
            ? selectedNodeIds.filter((nodeId) => nodeId !== node.id)
            : [...selectedNodeIds, node.id],
        );
        return;
      }
      selectNodes([node.id]);
    },
    [selectNodes, selectedNodeIds],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      const type = event.dataTransfer.getData(
        "application/lumen-canvas-node",
      ) as CanvasNodeType;
      if (!type || !(type in CANVAS_NODE_SPECS) || !instance) return;
      const position = instance.screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });
      addNode(type, position);
    },
    [addNode, instance],
  );

  const handleInit = useCallback(
    (next: ReactFlowInstance<CanvasFlowNode, Edge>) => {
      instanceRef.current = next;
      setInstance(next);
      onReady?.({
        fitView: () =>
          void next.fitView({ padding: 0.18, duration: 240 }),
        getViewportCenter: () => {
          const bounds = viewportRef.current?.getBoundingClientRect();
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
        setTransientPositions((current) =>
          updateCanvasTransientPositions(
            current,
            [],
            positions.map((item) => item.nodeId),
          ),
        );
        moveNodes(positions);
      }
      if (!interactionActiveRef.current) return;
      interactionActiveRef.current = false;
      endInteraction();
    },
    [endInteraction, moveNodes],
  );

  useEffect(
    () => () => {
      editorFocusRequestRef.current += 1;
      instanceRef.current = null;
      if (interactionActiveRef.current) endInteraction();
    },
    [endInteraction],
  );

  useEffect(() => {
    if (toolMode !== "select" || connectionDraft) {
      editorFocusRequestRef.current += 1;
    }
  }, [connectionDraft, toolMode]);

  return (
    <div
      ref={viewportRef}
      className={styles.viewport}
      onDrop={handleDrop}
      onPointerCancel={() => {
        setConnectionDraft(null);
        finishInteraction();
      }}
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
        onEdgeClick={(_event, edge) => {
          selectEdge(edge.id);
          if (isCompact) onOpenInspector?.();
        }}
        onPaneClick={() => {
          selectNode(null);
          selectEdge(null);
          setConnectionDraft(null);
        }}
        onNodeDragStart={startInteraction}
        onNodeDragStop={(_event, _node, nodes) => finishInteraction(nodes)}
        onSelectionDragStart={startInteraction}
        onSelectionDragStop={(_event, nodes) => finishInteraction(nodes)}
        onBeforeDelete={async ({ nodes, edges }) => {
          removeElements(
            nodes.map((node) => node.id),
            edges.map((edge) => edge.id),
          );
          return false;
        }}
        onConnect={onConnect}
        onConnectStart={onConnectStart}
        onConnectEnd={() => {
          if (!isCompact || toolMode !== "connect") {
            window.setTimeout(() => setConnectionDraft(null), 0);
          }
        }}
        isValidConnection={isValidConnection}
        minZoom={0.15}
        maxZoom={2.4}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        elevateNodesOnSelect={false}
        deleteKeyCode={["Backspace", "Delete"]}
        panOnDrag={isCompact ? toolMode === "hand" : [1, 2]}
        nodesDraggable={toolMode === "select"}
        nodesConnectable={!isCompact || toolMode === "connect"}
        connectOnClick={false}
        selectionOnDrag={!isCompact && toolMode === "select"}
        selectionKeyCode="Shift"
        multiSelectionKeyCode="Shift"
        zoomOnPinch
        zoomOnScroll={!isCompact}
        zoomOnDoubleClick={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="var(--border)" />
        {graph.nodes.length >= 30 ? (
          <MiniMap
            pannable
            zoomable
            nodeColor="var(--fg-2)"
            maskColor="color-mix(in srgb, var(--bg-0) 78%, transparent)"
          />
        ) : null}
      </ReactFlow>
      {isCompact && connectionDraft ? (
        <MobileConnectTargets
          open={targetPickerOpen}
          targets={compatibleTargets}
          onOpen={() => setTargetPickerOpen(true)}
          onClose={() => setTargetPickerOpen(false)}
          onCancel={() => {
            setTargetPickerOpen(false);
            setConnectionDraft(null);
          }}
          onSelect={(target) => {
            instance?.setCenter(target.x, target.y, {
              zoom: 1,
              duration: 240,
            });
            const result = addEdge({
              sourceNodeId: connectionDraft.sourceNodeId,
              sourceHandle: connectionDraft.sourceHandle,
              targetNodeId: target.nodeId,
              targetHandle: target.handleId,
            });
            if (!result.ok) {
              toast.error(result.reason);
              return;
            }
            setTargetPickerOpen(false);
          }}
        />
      ) : null}
    </div>
  );
}

interface CompatibleTarget {
  key: string;
  nodeId: string;
  nodeTitle: string;
  nodeType: string;
  handleId: string;
  handleLabel: string;
  x: number;
  y: number;
}

function canvasFlowNodeDimensions(node: CanvasNodeDefinition) {
  const width = node.size?.width ?? CANVAS_NODE_SPECS[node.type].width;
  const height = node.size?.height ?? (node.type === "frame" ? 220 : 180);
  return {
    width,
    height,
    styleHeight: node.type === "frame" ? height : undefined,
  };
}

function canvasNodeDeliveryOutputs(
  graph: CanvasGraph,
  node: CanvasNodeDefinition,
  activeOutputs: Map<string, CanvasOutput>,
  recentExecutions: CanvasNodeExecution[],
): CanvasOutput[] {
  if (node.type !== "delivery") return [];
  return deliveryOutputsForNode(
    graph,
    node.id,
    activeOutputs,
    recentExecutions,
  );
}

function compatibleInputHandlesForNode(
  graph: CanvasGraph,
  node: CanvasNodeDefinition,
  draft: ConnectionDraft | null,
): string[] {
  if (!draft) return [];
  return CANVAS_NODE_SPECS[node.type].inputs
    .filter((port) =>
      validateCanvasConnection(graph, {
        sourceNodeId: draft.sourceNodeId,
        sourceHandle: draft.sourceHandle,
        targetNodeId: node.id,
        targetHandle: port.id,
      }).valid,
    )
    .map((port) => port.id);
}

function listCompatibleTargets(
  graph: CanvasGraph,
  draft: ConnectionDraft | null,
): CompatibleTarget[] {
  if (!draft) return [];
  return graph.nodes.flatMap((node) =>
    CANVAS_NODE_SPECS[node.type].inputs.flatMap((port) => {
      const valid = validateCanvasConnection(graph, {
        sourceNodeId: draft.sourceNodeId,
        sourceHandle: draft.sourceHandle,
        targetNodeId: node.id,
        targetHandle: port.id,
      }).valid;
      return valid
        ? [
            {
              key: `${node.id}:${port.id}`,
              nodeId: node.id,
              nodeTitle: node.title,
              nodeType: CANVAS_NODE_SPECS[node.type].label,
              handleId: port.id,
              handleLabel: port.label,
              x: node.position.x,
              y: node.position.y,
            },
          ]
        : [];
    }),
  );
}

function MobileConnectTargets({
  open,
  targets,
  onOpen,
  onClose,
  onCancel,
  onSelect,
}: {
  open: boolean;
  targets: CompatibleTarget[];
  onOpen: () => void;
  onClose: () => void;
  onCancel: () => void;
  onSelect: (target: CompatibleTarget) => void;
}) {
  return (
    <>
      <div className="absolute inset-x-3 top-3 z-20 flex items-center justify-center gap-2">
        <button
          type="button"
          onClick={onOpen}
          className="inline-flex min-h-11 items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]/96 px-3 type-body-sm text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-xl"
        >
          <Cable className="h-4 w-4 text-[var(--accent)]" />
          兼容目标 {targets.length}
        </button>
        <button
          type="button"
          aria-label="取消连接"
          title="取消连接"
          onClick={onCancel}
          className="inline-flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]/96 text-[var(--fg-1)] shadow-[var(--shadow-2)] backdrop-blur-xl"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <BottomSheet
        open={open}
        onClose={onClose}
        ariaLabel="兼容连接目标"
        snapPoints={["62%"]}
        className="mobile-dialog-sheet"
      >
        <div className="mobile-dialog-scroll h-full overflow-y-auto p-4">
          <p className="type-page-kicker">连接目标</p>
          <h2 className="type-card-title mt-1">选择兼容端口</h2>
          <div className="mt-4 grid gap-2">
            {targets.length > 0 ? (
              targets.map((target) => (
                <button
                  key={target.key}
                  type="button"
                  onClick={() => onSelect(target)}
                  className="flex min-h-12 w-full items-center justify-between gap-3 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-left transition-colors active:bg-[var(--bg-3)]"
                >
                  <span className="min-w-0">
                    <span className="block truncate type-body-sm font-medium text-[var(--fg-0)]">
                      {target.nodeTitle}
                    </span>
                    <span className="block truncate type-caption text-[var(--fg-2)]">
                      {target.nodeType}
                    </span>
                  </span>
                  <span className="shrink-0 type-caption text-[var(--accent)]">
                    {target.handleLabel}
                  </span>
                </button>
              ))
            ) : (
              <p className="py-8 text-center type-body-sm text-[var(--fg-2)]">
                当前没有可连接的目标端口。
              </p>
            )}
          </div>
        </div>
      </BottomSheet>
    </>
  );
}
