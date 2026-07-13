import { createStore, type StoreApi } from "zustand/vanilla";

import {
  addCanvasNode,
  cloneCanvasGraph,
  createCanvasEdge,
  validateCanvasConnection,
  type CanvasConnectionInput,
} from "#canvas-graph";
import type {
  CanvasGraph,
  CanvasHistoryEntry,
  CanvasNodeType,
  CanvasOperation,
  CanvasSaveState,
  CanvasToolMode,
  ConnectionDraft,
} from "#canvas-types";

const HISTORY_LIMIT = 100;

export interface CanvasNodeMove {
  nodeId: string;
  position: { x: number; y: number };
}

type MergeableCanvasHistoryEntry = CanvasHistoryEntry & {
  mergeKey?: string;
};

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
  inFlightOperationCount: number;
  history: CanvasHistoryEntry[];
  future: CanvasHistoryEntry[];
  saveState: CanvasSaveState;
  saveMessage: string | null;
  hydrate: (graph: CanvasGraph, revision: number) => void;
  addNode: (type: CanvasNodeType, position: { x: number; y: number }) => string;
  updateNodeConfig: (nodeId: string, config: Record<string, unknown>) => void;
  beginNodeConfigEdit: (nodeId: string) => void;
  endNodeConfigEdit: (nodeId: string) => void;
  updateNodeTitle: (nodeId: string, title: string) => void;
  moveNode: (nodeId: string, position: { x: number; y: number }) => void;
  moveNodes: (items: CanvasNodeMove[]) => void;
  removeElements: (nodeIds: string[], edgeIds: string[]) => void;
  removeNodes: (nodeIds: string[]) => void;
  addEdge: (input: CanvasConnectionInput) => { ok: true } | { ok: false; reason: string };
  updateEdgeBinding: (
    edgeId: string,
    bindingMode: "follow_active" | "pinned",
    pinnedExecutionId?: string | null,
    pinnedOutputIndex?: number | null,
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
  markSaveError: (message: string) => void;
  markConflict: (message: string) => void;
  replaceFromRemote: (graph: CanvasGraph, revision: number) => void;
}

export type CanvasEditorStore = StoreApi<CanvasEditorState>;

export function createCanvasEditorStore(
  graph: CanvasGraph,
  revision: number,
): CanvasEditorStore {
  return createStore<CanvasEditorState>((set, get) => {
    const commit = (
      nextGraph: CanvasGraph,
      label: string,
      operations: CanvasOperation[],
      historyMergeKey?: string,
    ) => {
      const current = get();
      const previous = current.history.at(-1) as
        | MergeableCanvasHistoryEntry
        | undefined;
      const mergesWithPrevious =
        historyMergeKey !== undefined &&
        previous?.mergeKey === historyMergeKey;
      const history = mergesWithPrevious
        ? current.history
        : [
            ...current.history,
            {
              graph: cloneCanvasGraph(current.graph),
              label,
              mergeKey: historyMergeKey,
            },
          ].slice(-HISTORY_LIMIT);
      set({
        graph: nextGraph,
        history,
        future: [],
        pendingOperations: coalescePendingOperations(
          current.pendingOperations,
          operations,
          current.inFlightOperationCount,
        ),
        ...dirtySaveStatus(current),
      });
    };

    const sealNodeConfigEdit = (nodeId: string) => {
      const current = get();
      const previous = current.history.at(-1) as
        | MergeableCanvasHistoryEntry
        | undefined;
      if (previous?.mergeKey !== `update_node_config:${nodeId}`) return;
      const sealed: MergeableCanvasHistoryEntry = {
        ...previous,
        mergeKey: undefined,
      };
      set({
        history: [
          ...current.history.slice(0, -1),
          sealed,
        ],
      });
    };

    const moveNodes = (items: CanvasNodeMove[]) => {
      if (items.length === 0) return;
      const current = get().graph;
      const nodesById = new Map(current.nodes.map((node) => [node.id, node]));
      const positions = new Map<string, { x: number; y: number }>();
      for (const item of items) {
        if (!nodesById.has(item.nodeId)) continue;
        positions.set(item.nodeId, {
          x: item.position.x,
          y: item.position.y,
        });
      }
      const changedItems = [...positions]
        .filter(([nodeId, position]) => {
          const node = nodesById.get(nodeId);
          return (
            node &&
            (node.position.x !== position.x || node.position.y !== position.y)
          );
        })
        .map(([nodeId, position]) => ({ nodeId, position }));
      if (changedItems.length === 0) return;
      const changedPositions = new Map(
        changedItems.map((item) => [item.nodeId, item.position]),
      );
      commit(
        {
          ...current,
          nodes: current.nodes.map((node) => {
            const position = changedPositions.get(node.id);
            return position ? { ...node, position } : node;
          }),
        },
        "移动节点",
        [
          {
            op: "move_nodes",
            operation_schema_version: 1,
            items: changedItems.map(({ nodeId, position }) => ({
              node_id: nodeId,
              x: position.x,
              y: position.y,
            })),
          },
        ],
      );
    };

    const removeElements = (
      nodeIds: string[],
      edgeIds: string[],
      label = "删除元素",
    ) => {
      const current = get();
      const existingNodeIds = uniqueExistingIds(
        nodeIds,
        current.graph.nodes.map((node) => node.id),
      );
      const removedNodeIds = new Set(existingNodeIds);
      const associatedEdgeIds = current.graph.edges
        .filter(
          (edge) =>
            removedNodeIds.has(edge.source_node_id) ||
            removedNodeIds.has(edge.target_node_id),
        )
        .map((edge) => edge.id);
      const associatedEdges = new Set(associatedEdgeIds);
      const extraEdgeIds = uniqueExistingIds(
        edgeIds,
        current.graph.edges.map((edge) => edge.id),
      ).filter((edgeId) => !associatedEdges.has(edgeId));
      if (existingNodeIds.length === 0 && extraEdgeIds.length === 0) return;

      const removedEdgeIds = new Set([...associatedEdgeIds, ...extraEdgeIds]);
      const operations: CanvasOperation[] = [];
      if (existingNodeIds.length > 0) {
        operations.push({
          op: "remove_nodes",
          operation_schema_version: 1,
          node_ids: existingNodeIds,
          edge_ids: associatedEdgeIds,
        });
      }
      if (extraEdgeIds.length > 0) {
        operations.push({
          op: "remove_edges",
          operation_schema_version: 1,
          edge_ids: extraEdgeIds,
        });
      }
      commit(
        normalizeEdgeOrders({
          ...current.graph,
          nodes: current.graph.nodes.filter(
            (node) => !removedNodeIds.has(node.id),
          ),
          edges: current.graph.edges.filter(
            (edge) => !removedEdgeIds.has(edge.id),
          ),
        }),
        label,
        operations,
      );

      const selectedNodeIds = current.selectedNodeIds.filter(
        (nodeId) => !removedNodeIds.has(nodeId),
      );
      set({
        selectedNodeIds,
        selectedNodeId:
          current.selectedNodeId &&
          !removedNodeIds.has(current.selectedNodeId)
            ? current.selectedNodeId
            : (selectedNodeIds[0] ?? null),
        selectedEdgeId:
          current.selectedEdgeId &&
          !removedEdgeIds.has(current.selectedEdgeId)
            ? current.selectedEdgeId
            : null,
        editingNodeId:
          current.editingNodeId &&
          !removedNodeIds.has(current.editingNodeId)
            ? current.editingNodeId
            : null,
      });
    };

    return {
      graph: cloneCanvasGraph(graph),
      revision,
      selectedNodeId: null,
      selectedNodeIds: [],
      selectedEdgeId: null,
      editingNodeId: null,
      toolMode: "select",
      connectionDraft: null,
      activeInteractionCount: 0,
      pendingOperations: [],
      inFlightOperationCount: 0,
      history: [],
      future: [],
      saveState: "idle",
      saveMessage: null,
      hydrate(nextGraph, nextRevision) {
        set({
          graph: cloneCanvasGraph(nextGraph),
          revision: nextRevision,
          selectedNodeId: null,
          selectedNodeIds: [],
          selectedEdgeId: null,
          editingNodeId: null,
          connectionDraft: null,
          activeInteractionCount: 0,
          pendingOperations: [],
          inFlightOperationCount: 0,
          history: [],
          future: [],
          saveState: "saved",
          saveMessage: null,
        });
      },
      addNode(type, position) {
        const result = addCanvasNode(get().graph, type, position);
        commit(result.graph, `添加${result.node.title}`, [
          { op: "add_node", operation_schema_version: 1, node: result.node },
        ]);
        set({
          selectedNodeId: result.node.id,
          selectedNodeIds: [result.node.id],
          selectedEdgeId: null,
        });
        return result.node.id;
      },
      updateNodeConfig(nodeId, config) {
        const current = get().graph;
        const node = current.nodes.find((item) => item.id === nodeId);
        if (!node || canvasConfigEqual(node.config, config)) return;
        const removedEdgeIds =
          node.type === "video_generate"
            ? incompatibleVideoEdgeIds(
                current,
                nodeId,
                String(config.mode ?? "t2v"),
              )
            : [];
        const nextGraph = {
          ...current,
          nodes: current.nodes.map((item) =>
            item.id === nodeId ? { ...item, config: { ...config } } : item,
          ),
          edges: current.edges.filter(
            (edge) => !removedEdgeIds.includes(edge.id),
          ),
        };
        const next =
          removedEdgeIds.length > 0
            ? normalizeEdgeOrders(nextGraph)
            : nextGraph;
        commit(
          next,
          `编辑${node.title}`,
          [
            ...(removedEdgeIds.length > 0
              ? [
                  {
                    op: "remove_edges" as const,
                    operation_schema_version: 1 as const,
                    edge_ids: removedEdgeIds,
                  },
                ]
              : []),
            {
              op: "update_node_config",
              operation_schema_version: 1,
              node_id: nodeId,
              config: { ...config },
            },
          ],
          removedEdgeIds.length === 0
            ? `update_node_config:${nodeId}`
            : undefined,
        );
      },
      beginNodeConfigEdit(nodeId) {
        sealNodeConfigEdit(nodeId);
      },
      endNodeConfigEdit(nodeId) {
        sealNodeConfigEdit(nodeId);
      },
      updateNodeTitle(nodeId, title) {
        const value = title.trim().slice(0, 80);
        if (!value) return;
        const current = get().graph;
        const node = current.nodes.find((item) => item.id === nodeId);
        if (!node || node.title === value) return;
        const next = {
          ...current,
          nodes: current.nodes.map((item) =>
            item.id === nodeId ? { ...item, title: value } : item,
          ),
        };
        commit(next, "重命名节点", [
          {
            op: "update_node_meta",
            operation_schema_version: 1,
            node_id: nodeId,
            title: value,
          },
        ]);
      },
      moveNode(nodeId, position) {
        moveNodes([{ nodeId, position }]);
      },
      moveNodes(items) {
        moveNodes(items);
      },
      removeElements(nodeIds, edgeIds) {
        removeElements(nodeIds, edgeIds);
      },
      removeNodes(nodeIds) {
        removeElements(nodeIds, [], "删除节点");
      },
      addEdge(input) {
        const current = get().graph;
        const validation = validateCanvasConnection(current, input);
        if (!validation.valid) return { ok: false, reason: validation.reason };
        const edge = createCanvasEdge(current, input);
        if (!edge) return { ok: false, reason: "连接无效" };
        commit({ ...current, edges: [...current.edges, edge] }, "连接节点", [
          { op: "add_edge", operation_schema_version: 1, edge },
        ]);
        set({
          selectedEdgeId: edge.id,
          selectedNodeId: null,
          selectedNodeIds: [],
          connectionDraft: null,
        });
        return { ok: true };
      },
      updateEdgeBinding(
        edgeId,
        bindingMode,
        pinnedExecutionId = null,
        pinnedOutputIndex = null,
      ) {
        const current = get().graph;
        const edge = current.edges.find((item) => item.id === edgeId);
        if (!edge) return;
        const nextEdge = {
          ...edge,
          binding_mode: bindingMode,
          pinned_execution_id:
            bindingMode === "pinned" ? pinnedExecutionId : null,
          pinned_output_index:
            bindingMode === "pinned" ? pinnedOutputIndex : null,
        };
        if (
          edge.binding_mode === nextEdge.binding_mode &&
          edge.pinned_execution_id === nextEdge.pinned_execution_id &&
          edge.pinned_output_index === nextEdge.pinned_output_index
        ) {
          return;
        }
        commit(
          {
            ...current,
            edges: current.edges.map((item) =>
              item.id === edgeId ? nextEdge : item,
            ),
          },
          "更新输入绑定",
          [edgeBindingOperation(nextEdge)],
        );
      },
      removeEdges(edgeIds) {
        removeElements([], edgeIds, "删除连接");
      },
      selectNode(nodeId) {
        if (nodeId === null) {
          const current = get();
          if (
            current.selectedNodeId === null &&
            current.selectedNodeIds.length === 0 &&
            current.selectedEdgeId === null
          ) {
            return;
          }
          set({
            selectedNodeId: null,
            selectedNodeIds: [],
            selectedEdgeId: null,
          });
          return;
        }
        const current = get();
        if (!current.graph.nodes.some((node) => node.id === nodeId)) return;
        if (
          current.selectedNodeId === nodeId &&
          current.selectedNodeIds.includes(nodeId) &&
          current.selectedEdgeId === null
        ) {
          return;
        }
        set({
          selectedNodeId: nodeId,
          selectedNodeIds: current.selectedNodeIds.includes(nodeId)
            ? current.selectedNodeIds
            : [nodeId],
          selectedEdgeId: null,
        });
      },
      selectNodes(nodeIds) {
        const current = get();
        const selectedNodeIds = uniqueExistingIds(
          nodeIds,
          current.graph.nodes.map((node) => node.id),
        );
        const selectedNodeId =
          current.selectedNodeId &&
          selectedNodeIds.includes(current.selectedNodeId)
            ? current.selectedNodeId
            : (selectedNodeIds[0] ?? null);
        if (
          sameIds(current.selectedNodeIds, selectedNodeIds) &&
          current.selectedNodeId === selectedNodeId &&
          current.selectedEdgeId === null
        ) {
          return;
        }
        set({
          selectedNodeIds,
          selectedNodeId,
          selectedEdgeId: null,
        });
      },
      selectEdge(edgeId) {
        const current = get();
        if (
          current.selectedEdgeId === edgeId &&
          current.selectedNodeId === null &&
          current.selectedNodeIds.length === 0
        ) {
          return;
        }
        set({
          selectedEdgeId: edgeId,
          selectedNodeId: null,
          selectedNodeIds: [],
        });
      },
      beginNodeEdit(nodeId) {
        const current = get();
        if (
          current.editingNodeId === nodeId ||
          !current.graph.nodes.some((node) => node.id === nodeId)
        ) {
          return;
        }
        set({ editingNodeId: nodeId });
      },
      endNodeEdit(nodeId) {
        if (get().editingNodeId !== nodeId) return;
        set({ editingNodeId: null });
      },
      setToolMode(toolMode) {
        set({
          toolMode,
          connectionDraft: null,
          editingNodeId: toolMode === "select" ? get().editingNodeId : null,
        });
      },
      setConnectionDraft(connectionDraft) {
        set((current) => ({
          connectionDraft,
          editingNodeId: connectionDraft ? null : current.editingNodeId,
        }));
      },
      beginInteraction() {
        set((current) => ({
          activeInteractionCount: current.activeInteractionCount + 1,
        }));
      },
      endInteraction() {
        set((current) => ({
          activeInteractionCount: Math.max(
            0,
            current.activeInteractionCount - 1,
          ),
        }));
      },
      undo() {
        const current = get();
        const previous = current.history.at(-1);
        if (!previous) return;
        const operations = operationsBetween(current.graph, previous.graph);
        set({
          graph: cloneCanvasGraph(previous.graph),
          history: current.history.slice(0, -1),
          future: [
            { graph: cloneCanvasGraph(current.graph), label: previous.label },
            ...current.future,
          ].slice(0, HISTORY_LIMIT),
          pendingOperations: [...current.pendingOperations, ...operations],
          ...dirtySaveStatus(current),
          selectedNodeId: null,
          selectedNodeIds: [],
          selectedEdgeId: null,
          editingNodeId: null,
        });
      },
      redo() {
        const current = get();
        const nextEntry = current.future[0];
        if (!nextEntry) return;
        const operations = operationsBetween(current.graph, nextEntry.graph);
        set({
          graph: cloneCanvasGraph(nextEntry.graph),
          history: [
            ...current.history,
            { graph: cloneCanvasGraph(current.graph), label: nextEntry.label },
          ].slice(-HISTORY_LIMIT),
          future: current.future.slice(1),
          pendingOperations: [...current.pendingOperations, ...operations],
          ...dirtySaveStatus(current),
          selectedNodeId: null,
          selectedNodeIds: [],
          selectedEdgeId: null,
          editingNodeId: null,
        });
      },
      markSaving(count) {
        const current = get();
        if (current.saveState === "conflict") return;
        const pendingCount = current.pendingOperations.length;
        set({
          inFlightOperationCount: Math.min(
            Math.max(0, count ?? pendingCount),
            pendingCount,
          ),
          saveState: "saving",
          saveMessage: null,
        });
      },
      acknowledgeOperations(count, nextRevision) {
        const current = get();
        const acknowledgedCount = Math.min(
          Math.max(0, count),
          current.inFlightOperationCount,
        );
        if (acknowledgedCount === 0) return false;
        const pending = current.pendingOperations.slice(acknowledgedCount);
        set({
          pendingOperations: pending,
          inFlightOperationCount: 0,
          revision: nextRevision,
          saveState: pending.length > 0 ? "dirty" : "saved",
          saveMessage: null,
        });
        return true;
      },
      markSaveError(message) {
        set((current) =>
          current.saveState === "conflict"
            ? {}
            : {
                saveState: "error",
                saveMessage: message,
              },
        );
      },
      markConflict(message) {
        set({
          inFlightOperationCount: 0,
          saveState: "conflict",
          saveMessage: message,
        });
      },
      replaceFromRemote(nextGraph, nextRevision) {
        set({
          graph: cloneCanvasGraph(nextGraph),
          revision: nextRevision,
          selectedNodeId: null,
          selectedNodeIds: [],
          selectedEdgeId: null,
          editingNodeId: null,
          connectionDraft: null,
          activeInteractionCount: 0,
          history: [],
          future: [],
          pendingOperations: [],
          inFlightOperationCount: 0,
          saveState: "saved",
          saveMessage: null,
        });
      },
    };
  });
}

function dirtySaveStatus(
  state: Pick<CanvasEditorState, "saveState" | "saveMessage">,
): Pick<CanvasEditorState, "saveState" | "saveMessage"> {
  return state.saveState === "conflict"
    ? {
        saveState: "conflict",
        saveMessage: state.saveMessage,
      }
    : {
        saveState: "dirty",
        saveMessage: null,
      };
}

function canvasConfigEqual(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): boolean {
  return jsonValueEqual(left, right);
}

function jsonValueEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => jsonValueEqual(value, right[index]))
    );
  }
  if (
    !left ||
    !right ||
    typeof left !== "object" ||
    typeof right !== "object"
  ) {
    return false;
  }
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const leftKeys = Object.keys(leftRecord);
  const rightKeys = Object.keys(rightRecord);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every(
      (key) =>
        Object.prototype.hasOwnProperty.call(rightRecord, key) &&
        jsonValueEqual(leftRecord[key], rightRecord[key]),
    )
  );
}

function coalescePendingOperations(
  pending: CanvasOperation[],
  incoming: CanvasOperation[],
  protectedPrefixCount = 0,
): CanvasOperation[] {
  if (incoming.length !== 1 || incoming[0].op !== "update_node_config") {
    return [...pending, ...incoming];
  }
  const protectedCount = Math.min(
    Math.max(0, protectedPrefixCount),
    pending.length,
  );
  if (pending.length <= protectedCount) {
    return [...pending, incoming[0]];
  }
  const previous = pending.at(-1);
  if (
    previous?.op !== "update_node_config" ||
    previous.node_id !== incoming[0].node_id
  ) {
    return [...pending, incoming[0]];
  }
  return [...pending.slice(0, -1), incoming[0]];
}

export function operationsBetween(
  current: CanvasGraph,
  next: CanvasGraph,
): CanvasOperation[] {
  const currentNodes = new Map(current.nodes.map((node) => [node.id, node]));
  const nextNodes = new Map(next.nodes.map((node) => [node.id, node]));
  const currentEdges = new Map(current.edges.map((edge) => [edge.id, edge]));
  const nextEdgeIds = new Set(next.edges.map((edge) => edge.id));
  const removedNodeIds = current.nodes
    .filter((node) => !nextNodes.has(node.id))
    .map((node) => node.id);
  return [
    ...removedEntityOperations(current, nextEdgeIds, removedNodeIds),
    ...changedNodeOperations(currentNodes, next),
    ...changedEdgeOperations(currentEdges, next),
  ];
}

function removedEntityOperations(
  current: CanvasGraph,
  nextEdgeIds: ReadonlySet<string>,
  removedNodeIds: string[],
): CanvasOperation[] {
  const removedNodes = new Set(removedNodeIds);
  const associatedEdgeIds = current.edges
    .filter(
      (edge) =>
        removedNodes.has(edge.source_node_id) ||
        removedNodes.has(edge.target_node_id),
    )
    .map((edge) => edge.id);
  const associatedEdges = new Set(associatedEdgeIds);
  const independentlyRemovedEdgeIds = current.edges
    .filter(
      (edge) =>
        !associatedEdges.has(edge.id) &&
        !nextEdgeIds.has(edge.id),
    )
    .map((edge) => edge.id);
  const operations: CanvasOperation[] = [];
  if (removedNodeIds.length > 0) {
    operations.push({
      op: "remove_nodes",
      operation_schema_version: 1,
      node_ids: removedNodeIds,
      edge_ids: associatedEdgeIds,
    });
  }
  if (independentlyRemovedEdgeIds.length > 0) {
    operations.push({
      op: "remove_edges",
      operation_schema_version: 1,
      edge_ids: independentlyRemovedEdgeIds,
    });
  }
  return operations;
}

function changedNodeOperations(
  currentNodes: Map<string, CanvasGraph["nodes"][number]>,
  next: CanvasGraph,
): CanvasOperation[] {
  const operations: CanvasOperation[] = [];
  const movedItems: Extract<
    CanvasOperation,
    { op: "move_nodes" }
  >["items"] = [];
  for (const node of next.nodes) {
    const before = currentNodes.get(node.id);
    if (!before) {
      operations.push({ op: "add_node", operation_schema_version: 1, node });
      continue;
    }
    if (before.title !== node.title) {
      operations.push({
        op: "update_node_meta",
        operation_schema_version: 1,
        node_id: node.id,
        title: node.title,
      });
    }
    if (!canvasConfigEqual(before.config, node.config)) {
      operations.push({
        op: "update_node_config",
        operation_schema_version: 1,
        node_id: node.id,
        config: node.config,
      });
    }
    if (
      before.position.x !== node.position.x ||
      before.position.y !== node.position.y
    ) {
      movedItems.push({
        node_id: node.id,
        x: node.position.x,
        y: node.position.y,
      });
    }
  }
  if (movedItems.length > 0) {
    operations.push({
      op: "move_nodes",
      operation_schema_version: 1,
      items: movedItems,
    });
  }
  return operations;
}

function uniqueExistingIds(ids: string[], existingIds: string[]): string[] {
  const existing = new Set(existingIds);
  return [...new Set(ids)].filter((id) => existing.has(id));
}

function sameIds(left: string[], right: string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

function changedEdgeOperations(
  currentEdges: ReadonlyMap<string, CanvasGraph["edges"][number]>,
  next: CanvasGraph,
): CanvasOperation[] {
  const operations: CanvasOperation[] = [];
  for (const edge of next.edges) {
    const before = currentEdges.get(edge.id);
    if (!before) {
      operations.push({ op: "add_edge", operation_schema_version: 1, edge });
    } else if (
      before.binding_mode !== edge.binding_mode ||
      before.pinned_execution_id !== edge.pinned_execution_id ||
      before.pinned_output_index !== edge.pinned_output_index ||
      before.order !== edge.order
    ) {
      operations.push({
        ...edgeBindingOperation(edge),
        order: edge.order ?? null,
      });
    }
  }
  return operations;
}

function edgeBindingOperation(
  edge: CanvasGraph["edges"][number],
): Extract<CanvasOperation, { op: "update_edge" }> {
  return {
    op: "update_edge",
    operation_schema_version: 1,
    edge_id: edge.id,
    binding_mode: edge.binding_mode,
    pinned_execution_id: edge.pinned_execution_id ?? null,
    pinned_output_index: edge.pinned_output_index ?? null,
  };
}

function normalizeEdgeOrders(graph: CanvasGraph): CanvasGraph {
  const groups = new Map<string, CanvasGraph["edges"]>();
  for (const edge of graph.edges) {
    const key = `${edge.target_node_id}\0${edge.target_handle}`;
    groups.set(key, [...(groups.get(key) ?? []), edge]);
  }
  const normalized = new Map<string, number>();
  for (const edges of groups.values()) {
    edges
      .sort(
        (left, right) =>
          (left.order ?? 0) - (right.order ?? 0) ||
          left.id.localeCompare(right.id),
      )
      .forEach((edge, order) => normalized.set(edge.id, order));
  }
  return {
    ...graph,
    edges: graph.edges.map((edge) => ({
      ...edge,
      order: normalized.get(edge.id) ?? 0,
    })),
  };
}

function incompatibleVideoEdgeIds(
  graph: CanvasGraph,
  nodeId: string,
  mode: string,
): string[] {
  const blocked =
    mode === "t2v"
      ? new Set(["first_frame", "reference_images", "reference_videos"])
      : mode === "i2v"
        ? new Set(["reference_images", "reference_videos"])
        : new Set(["first_frame"]);
  return graph.edges
    .filter(
      (edge) =>
        edge.target_node_id === nodeId && blocked.has(edge.target_handle),
    )
    .map((edge) => edge.id);
}
