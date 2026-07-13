import { createStore, type StoreApi } from "zustand/vanilla";

import {
  addCanvasNode,
  cloneCanvasGraph,
  createCanvasEdge,
  removeCanvasNodes,
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

export interface CanvasEditorState {
  graph: CanvasGraph;
  revision: number;
  selectedNodeId: string | null;
  selectedEdgeId: string | null;
  toolMode: CanvasToolMode;
  connectionDraft: ConnectionDraft | null;
  pendingOperations: CanvasOperation[];
  history: CanvasHistoryEntry[];
  future: CanvasHistoryEntry[];
  saveState: CanvasSaveState;
  saveMessage: string | null;
  hydrate: (graph: CanvasGraph, revision: number) => void;
  addNode: (type: CanvasNodeType, position: { x: number; y: number }) => string;
  updateNodeConfig: (nodeId: string, config: Record<string, unknown>) => void;
  updateNodeTitle: (nodeId: string, title: string) => void;
  moveNode: (nodeId: string, position: { x: number; y: number }) => void;
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
  selectEdge: (edgeId: string | null) => void;
  setToolMode: (mode: CanvasToolMode) => void;
  setConnectionDraft: (draft: ConnectionDraft | null) => void;
  undo: () => void;
  redo: () => void;
  markSaving: () => void;
  acknowledgeOperations: (count: number, revision: number) => void;
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
      mergeHistory = false,
    ) => {
      const current = get();
      const history =
        mergeHistory && current.history.at(-1)?.label === label
          ? current.history
          : [
              ...current.history,
              { graph: cloneCanvasGraph(current.graph), label },
            ].slice(-HISTORY_LIMIT);
      set({
        graph: nextGraph,
        history,
        future: [],
        pendingOperations: coalescePendingOperations(
          current.pendingOperations,
          operations,
        ),
        saveState: "dirty",
        saveMessage: null,
      });
    };

    return {
      graph: cloneCanvasGraph(graph),
      revision,
      selectedNodeId: null,
      selectedEdgeId: null,
      toolMode: "select",
      connectionDraft: null,
      pendingOperations: [],
      history: [],
      future: [],
      saveState: "idle",
      saveMessage: null,
      hydrate(nextGraph, nextRevision) {
        set({
          graph: cloneCanvasGraph(nextGraph),
          revision: nextRevision,
          pendingOperations: [],
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
        set({ selectedNodeId: result.node.id, selectedEdgeId: null });
        return result.node.id;
      },
      updateNodeConfig(nodeId, config) {
        const current = get().graph;
        const node = current.nodes.find((item) => item.id === nodeId);
        if (!node || shallowEqual(node.config, config)) return;
        const removedEdgeIds =
          node.type === "video_generate"
            ? incompatibleVideoEdgeIds(
                current,
                nodeId,
                String(config.mode ?? "t2v"),
              )
            : [];
        const next = normalizeEdgeOrders({
          ...current,
          nodes: current.nodes.map((item) =>
            item.id === nodeId ? { ...item, config: { ...config } } : item,
          ),
          edges: current.edges.filter(
            (edge) => !removedEdgeIds.includes(edge.id),
          ),
        });
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
          removedEdgeIds.length === 0,
        );
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
        const current = get().graph;
        const node = current.nodes.find((item) => item.id === nodeId);
        if (
          !node ||
          (node.position.x === position.x && node.position.y === position.y)
        ) {
          return;
        }
        const next = {
          ...current,
          nodes: current.nodes.map((item) =>
            item.id === nodeId ? { ...item, position } : item,
          ),
        };
        commit(next, "移动节点", [
          {
            op: "move_nodes",
            operation_schema_version: 1,
            items: [{ node_id: nodeId, x: position.x, y: position.y }],
          },
        ]);
      },
      removeNodes(nodeIds) {
        if (nodeIds.length === 0) return;
        const current = get().graph;
        const existingIds = nodeIds.filter((id) =>
          current.nodes.some((node) => node.id === id),
        );
        if (existingIds.length === 0) return;
        const result = removeCanvasNodes(current, existingIds);
        commit(normalizeEdgeOrders(result.graph), "删除节点", [
          {
            op: "remove_nodes",
            operation_schema_version: 1,
            node_ids: existingIds,
            edge_ids: result.edgeIds,
          },
        ]);
        set({ selectedNodeId: null, selectedEdgeId: null });
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
        set({ selectedEdgeId: edge.id, selectedNodeId: null, connectionDraft: null });
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
        if (edgeIds.length === 0) return;
        const current = get().graph;
        const existing = edgeIds.filter((id) =>
          current.edges.some((edge) => edge.id === id),
        );
        if (existing.length === 0) return;
        commit(
          normalizeEdgeOrders({
            ...current,
            edges: current.edges.filter((edge) => !existing.includes(edge.id)),
          }),
          "删除连接",
          [{ op: "remove_edges", operation_schema_version: 1, edge_ids: existing }],
        );
        set({ selectedEdgeId: null });
      },
      selectNode(nodeId) {
        set({ selectedNodeId: nodeId, selectedEdgeId: null });
      },
      selectEdge(edgeId) {
        set({ selectedEdgeId: edgeId, selectedNodeId: null });
      },
      setToolMode(toolMode) {
        set({ toolMode, connectionDraft: null });
      },
      setConnectionDraft(connectionDraft) {
        set({ connectionDraft });
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
          saveState: "dirty",
          saveMessage: null,
          selectedNodeId: null,
          selectedEdgeId: null,
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
          saveState: "dirty",
          saveMessage: null,
          selectedNodeId: null,
          selectedEdgeId: null,
        });
      },
      markSaving() {
        set({ saveState: "saving", saveMessage: null });
      },
      acknowledgeOperations(count, nextRevision) {
        const pending = get().pendingOperations.slice(count);
        set({
          pendingOperations: pending,
          revision: nextRevision,
          saveState: pending.length > 0 ? "dirty" : "saved",
          saveMessage: null,
        });
      },
      markSaveError(message) {
        set({ saveState: "error", saveMessage: message });
      },
      markConflict(message) {
        set({ saveState: "conflict", saveMessage: message });
      },
      replaceFromRemote(nextGraph, nextRevision) {
        set({
          graph: cloneCanvasGraph(nextGraph),
          revision: nextRevision,
          history: [],
          future: [],
          pendingOperations: [],
          saveState: "saved",
          saveMessage: null,
          selectedNodeId: null,
          selectedEdgeId: null,
        });
      },
    };
  });
}

function shallowEqual(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): boolean {
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  return (
    leftKeys.length === rightKeys.length &&
    leftKeys.every((key) => Object.is(left[key], right[key]))
  );
}

function coalescePendingOperations(
  pending: CanvasOperation[],
  incoming: CanvasOperation[],
): CanvasOperation[] {
  if (incoming.length !== 1 || incoming[0].op !== "update_node_config") {
    return [...pending, ...incoming];
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
  const removedNodeIds = current.nodes
    .filter((node) => !nextNodes.has(node.id))
    .map((node) => node.id);
  return [
    ...removedEntityOperations(current, next, removedNodeIds),
    ...changedNodeOperations(currentNodes, next),
    ...changedEdgeOperations(current, next),
  ];
}

function removedEntityOperations(
  current: CanvasGraph,
  next: CanvasGraph,
  removedNodeIds: string[],
): CanvasOperation[] {
  const removedEdgeIds = current.edges
    .filter(
      (edge) =>
        !next.edges.some((candidate) => candidate.id === edge.id) ||
        removedNodeIds.includes(edge.source_node_id) ||
        removedNodeIds.includes(edge.target_node_id),
    )
    .map((edge) => edge.id);
  if (removedNodeIds.length > 0) {
    return [
      {
        op: "remove_nodes",
        operation_schema_version: 1,
        node_ids: removedNodeIds,
        edge_ids: removedEdgeIds,
      },
    ];
  }
  return removedEdgeIds.length > 0
    ? [
        {
          op: "remove_edges",
          operation_schema_version: 1,
          edge_ids: removedEdgeIds,
        },
      ]
    : [];
}

function changedNodeOperations(
  currentNodes: Map<string, CanvasGraph["nodes"][number]>,
  next: CanvasGraph,
): CanvasOperation[] {
  const operations: CanvasOperation[] = [];
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
    if (!shallowEqual(before.config, node.config)) {
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
      operations.push({
        op: "move_nodes",
        operation_schema_version: 1,
        items: [{ node_id: node.id, x: node.position.x, y: node.position.y }],
      });
    }
  }
  return operations;
}

function changedEdgeOperations(
  current: CanvasGraph,
  next: CanvasGraph,
): CanvasOperation[] {
  const operations: CanvasOperation[] = [];
  for (const edge of next.edges) {
    const before = current.edges.find((candidate) => candidate.id === edge.id);
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
