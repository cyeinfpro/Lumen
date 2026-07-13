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
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Cable, X } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { useIsMobile } from "@/hooks/useMediaQuery";
import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import {
  activeOutputsByNode,
  deliveryOutputsForNode,
  latestExecutionsByNode,
} from "@/lib/canvas/runtime";
import { validateCanvasConnection } from "@/lib/canvas/graph";
import type {
  CanvasDocument,
  CanvasGraph,
  CanvasNodeType,
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
}

export function CanvasViewport({
  document,
  onRunNode,
  onReady,
}: {
  document: CanvasDocument;
  onRunNode: (nodeId: string) => void;
  onReady?: (api: CanvasViewportApi) => void;
}) {
  const graph = useCanvasStore((state) => state.graph);
  const selectedNodeId = useCanvasStore((state) => state.selectedNodeId);
  const selectedEdgeId = useCanvasStore((state) => state.selectedEdgeId);
  const toolMode = useCanvasStore((state) => state.toolMode);
  const connectionDraft = useCanvasStore((state) => state.connectionDraft);
  const selectNode = useCanvasStore((state) => state.selectNode);
  const selectEdge = useCanvasStore((state) => state.selectEdge);
  const moveNode = useCanvasStore((state) => state.moveNode);
  const removeNodes = useCanvasStore((state) => state.removeNodes);
  const addEdge = useCanvasStore((state) => state.addEdge);
  const removeEdges = useCanvasStore((state) => state.removeEdges);
  const addNode = useCanvasStore((state) => state.addNode);
  const setConnectionDraft = useCanvasStore((state) => state.setConnectionDraft);
  const isMobile = useIsMobile() === true;
  const [instance, setInstance] =
    useState<ReactFlowInstance<CanvasFlowNode, Edge> | null>(null);
  const [targetPickerOpen, setTargetPickerOpen] = useState(false);
  const [transientPositions, setTransientPositions] = useState<
    Record<string, { x: number; y: number }>
  >({});

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

  const projectedNodes = useMemo<CanvasFlowNode[]>(
    () =>
      graph.nodes.map((node) => ({
        id: node.id,
        type: node.type,
        position: node.position,
        selected: node.id === selectedNodeId,
        draggable: toolMode === "select",
        connectable: !isMobile || toolMode === "connect",
        zIndex: node.type === "frame" ? -1 : 1,
        style: {
          width: node.size?.width,
          height: node.type === "frame" ? node.size?.height : undefined,
        },
        data: {
          definition: node,
          execution: executions.get(node.id) ?? null,
          activeOutput: activeOutputs.get(node.id) ?? null,
          deliveryOutputs:
            node.type === "delivery"
              ? deliveryOutputsForNode(
                  graph,
                  node.id,
                  activeOutputs,
                  document.recent_executions,
                )
              : [],
          connectionType: connectionDraft?.dataType ?? null,
          compatibleInputHandles: connectionDraft
            ? CANVAS_NODE_SPECS[node.type].inputs
                .filter((port) =>
                  validateCanvasConnection(graph, {
                    sourceNodeId: connectionDraft.sourceNodeId,
                    sourceHandle: connectionDraft.sourceHandle,
                    targetNodeId: node.id,
                    targetHandle: port.id,
                  }).valid,
                )
                .map((port) => port.id)
            : [],
          onRun: onRunNode,
        },
      })),
    [
      activeOutputs,
      connectionDraft,
      document.recent_executions,
      executions,
      graph,
      isMobile,
      onRunNode,
      selectedNodeId,
      toolMode,
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
      const removed = changes
        .filter((change) => change.type === "remove")
        .map((change) => change.id);
      if (removed.length > 0) removeNodes(removed);
      for (const change of changes) {
        if (change.type === "select") {
          if (change.selected) selectNode(change.id);
          else if (selectedNodeId === change.id) selectNode(null);
        }
      }
      const positions = changes.filter(
        (
          change,
        ): change is Extract<NodeChange<CanvasFlowNode>, { type: "position" }> =>
          change.type === "position" && Boolean(change.position),
      );
      if (positions.length > 0) {
        setTransientPositions((current) => {
          const next = { ...current };
          for (const change of positions) {
            if (change.position) next[change.id] = change.position;
          }
          return next;
        });
      }
    },
    [removeNodes, selectNode, selectedNodeId],
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
      setInstance(next);
      onReady?.({ fitView: () => void next.fitView({ padding: 0.18, duration: 240 }) });
    },
    [onReady],
  );

  return (
    <div
      className={styles.viewport}
      onDrop={handleDrop}
      onDragOver={(event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
    >
      <ReactFlow<CanvasFlowNode, Edge>
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={canvasNodeTypes}
        onInit={handleInit}
        onNodesChange={onNodesChange}
        onEdgesChange={(changes) => {
          const removed = changes
            .filter((change) => change.type === "remove")
            .map((change) => change.id);
          if (removed.length > 0) removeEdges(removed);
          for (const change of changes) {
            if (change.type === "select" && change.selected) selectEdge(change.id);
          }
        }}
        onNodeClick={(_event, node) => selectNode(node.id)}
        onEdgeClick={(_event, edge) => selectEdge(edge.id)}
        onPaneClick={() => {
          selectNode(null);
          selectEdge(null);
          setConnectionDraft(null);
        }}
        onNodeDragStop={(_event, node) => {
          setTransientPositions((current) => {
            const next = { ...current };
            delete next[node.id];
            return next;
          });
          moveNode(node.id, node.position);
        }}
        onConnect={onConnect}
        onConnectStart={onConnectStart}
        onConnectEnd={() => {
          if (!isMobile || toolMode !== "connect") {
            window.setTimeout(() => setConnectionDraft(null), 0);
          }
        }}
        isValidConnection={isValidConnection}
        minZoom={0.15}
        maxZoom={2.4}
        fitView
        fitViewOptions={{ padding: 0.18 }}
        deleteKeyCode={["Backspace", "Delete"]}
        panOnDrag={isMobile ? toolMode === "hand" : [1, 2]}
        nodesDraggable={toolMode === "select"}
        nodesConnectable={!isMobile || toolMode === "connect"}
        connectOnClick={isMobile && toolMode === "connect"}
        selectionOnDrag={!isMobile && toolMode === "select"}
        selectionKeyCode="Shift"
        multiSelectionKeyCode="Shift"
        zoomOnPinch
        zoomOnScroll={!isMobile}
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
      {isMobile && connectionDraft ? (
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
      <div className="absolute inset-x-3 top-3 z-20 flex items-center justify-center gap-2 md:hidden">
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
